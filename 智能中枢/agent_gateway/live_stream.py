"""实时字幕流式服务（2026-11-12）：真流式语音转文字。

架构（用户要求：说话过程中屏幕就要出字，用已部署的千问 ASR 做精转）：
- 设备每 ~600ms POST 一段 16k 单声道 s16le PCM 到 /live/stream；
- 本模块把 PCM 喂给 sherpa-onnx 流式 zipformer（14M，增量解码），
  partial 文字随说话实时增长——这是屏幕上的「现场字幕」；
- 流式引擎检测到 1.2s 尾部停顿（endpoint）= 一句说完：partial 定稿成
  一行，同时把这句的完整音频丢给后台的千问 ASR（153 机器，Qwen3-1.7B
  @ RTX 3090，批处理接口）精转，完成后原地替换该行——粗字变准字；
- GET /live/frame 返回当前整屏 RGB565 帧（live_screen 渲染）；
- partial/定稿文本命中 退下/清屏 → 清空会话重新开始。
"""

import struct
import threading
import time
import wave
from pathlib import Path
from typing import Dict, List, Optional, Tuple

from agent_gateway import live_screen
from agent_gateway.live_screen import render_rows, wrap_text

_MODEL_DIR = Path.home() / ".cache/sherpa-asr/sherpa-onnx-streaming-zipformer-zh-14M-2023-02-23"
_MAX_UTTERANCE_SECONDS = 20.0  # 单句音频上限：超长强制定稿，防止内存无限涨
_PCM_RATE = 16000

_lock = threading.RLock()
_recognizer = None  # 进程内单例（模型加载 1~2s，只做一次）
_sessions: Dict[str, dict] = {}
_finalize_pool = None  # 千问精转的后台线程池（懒建）

_CLEAR_WORDS = ("退下", "清屏", "清空", "别说了", "停下吧")


def is_available() -> bool:
    directory = _MODEL_DIR
    return (
        (directory / "tokens.txt").is_file()
        and (directory / "encoder-epoch-99-avg-1.int8.onnx").is_file()
        and (directory / "decoder-epoch-99-avg-1.int8.onnx").is_file()
        and (directory / "joiner-epoch-99-avg-1.int8.onnx").is_file()
    )


def _get_recognizer():
    global _recognizer
    if _recognizer is not None:
        return _recognizer
    import sherpa_onnx

    _recognizer = sherpa_onnx.OnlineRecognizer.from_transducer(
        tokens=str(_MODEL_DIR / "tokens.txt"),
        encoder=str(_MODEL_DIR / "encoder-epoch-99-avg-1.int8.onnx"),
        decoder=str(_MODEL_DIR / "decoder-epoch-99-avg-1.int8.onnx"),
        joiner=str(_MODEL_DIR / "joiner-epoch-99-avg-1.int8.onnx"),
        num_threads=2,
        enable_endpoint_detection=True,
        rule1_min_trailing_silence=2.0,
        # 1.2→1.0s：endpoint 越早触发，重置越早完成，用户在重置窗口里
        # 开口的竞态概率越小（实测句首丢字集中发生在 1.2~1.5s 停顿后）
        rule2_min_trailing_silence=1.0,
        rule3_min_utterance_length=20.0,
    )
    return _recognizer


def _get_finalize_pool():
    global _finalize_pool
    if _finalize_pool is None:
        from concurrent.futures import ThreadPoolExecutor

        _finalize_pool = ThreadPoolExecutor(
            max_workers=1, thread_name_prefix="live-finalize"
        )
    return _finalize_pool


def _new_stream(device_id: str) -> None:
    session = _sessions.setdefault(
        device_id,
        {
            "stream": None,
            "utterances": [],  # 每句粗字文本（千问/SenseVoice 精转后原地替换）
            "slots": [],       # 与 utterances 对齐：{"start": 起始行, "count": 行数}
            # 固定行槽（2026-11-12）：16 行各自钉住内容，句子定稿/精转只原地
            # 改写自己的行，不做全局重排——修「每说完一句屏幕就清空重排」。
            "rows": [None] * 16,
            "next_row": 0,
            "partial": "",
            "utterance_pcm": bytearray(),
            "last_active": time.time(),
        },
    )
    recognizer = _get_recognizer()
    session["stream"] = recognizer.create_stream()


def _write_rows(session: dict, start_row: int, pieces: List[str]) -> int:
    """把折行段写进行槽（回卷覆盖旧行），返回占用的行数。"""
    rows: List[Optional[str]] = session["rows"]
    for offset, piece in enumerate(pieces):
        rows[(start_row + offset) % 16] = piece
    return len(pieces)


def reset_session(device_id: str) -> None:
    with _lock:
        _sessions.pop(device_id, None)
        _new_stream(device_id)


def _contains_clear(text: str) -> bool:
    return any(word in text for word in _CLEAR_WORDS)


def _normalize_samples(samples: List[int]) -> List[int]:
    """每段峰值归一化到 -6dBFS（上限 ×20）。

    设备推的是原始电平，用户小声说话只有 -32~-36dBFS，流式引擎对这种
    弱信号直接「听不到」。纯底噪（峰值 <200）不放大，避免抬成幻觉语音。
    """
    peak = max(abs(s) for s in samples) if samples else 0
    if 200 <= peak < 16000:
        gain = min(16000.0 / peak, 20.0)
        return [max(-32768, min(32767, int(s * gain))) for s in samples]
    return samples


def feed_pcm(device_id: str, pcm_bytes: bytes) -> None:
    """设备推来的 16k 单声道 s16le PCM → 喂流式引擎并增量解码。

    每段先做峰值归一化（2026-11-12）：设备推的是原始电平，用户小声说话
    时只有 -32~-36dBFS，流式引擎对这种弱信号直接「听不到」。把峰值拉到
    0.5（-6dBFS），上限 ×20——纯底噪（峰值 <200）不放大，避免把噪声抬成
    幻觉语音。
    """
    recognizer = _get_recognizer()
    with _lock:
        session = _sessions.get(device_id)
        if session is None or session.get("stream") is None:
            reset_session(device_id)
            session = _sessions[device_id]
        session["last_active"] = time.time()

        n = len(pcm_bytes) // 2
        if n == 0:
            return
        samples = list(struct.unpack(f"<{n}h", pcm_bytes[: n * 2]))
        samples = _normalize_samples(samples)
        stream = session["stream"]
        # 必须归一到 [-1, 1] 浮点：整型直喂振幅超万倍，识别直接乱码
        stream.accept_waveform(_PCM_RATE, [s / 32768.0 for s in samples])
        while recognizer.is_ready(stream):
            recognizer.decode_stream(stream)
        partial = str(recognizer.get_result(stream) or "").strip()
        session["partial"] = partial

        # 单句音频累计（供千问精转）；超 20s 强制定稿防内存无限涨
        session["utterance_pcm"].extend(pcm_bytes)
        max_pcm = int(_MAX_UTTERANCE_SECONDS * _PCM_RATE) * 2
        endpoint = recognizer.is_endpoint(stream)
        if endpoint or len(session["utterance_pcm"]) >= max_pcm:
            _finalize_utterance(device_id, session, recognizer)


def _finalize_utterance(
    device_id: str, session: dict, recognizer
) -> None:
    """停顿/超长：partial 定稿一行，缓冲音频交千问精转后原地替换。

    句首丢字修复（2026-11-12）：endpoint 重置时，触发块里可能混着用户
    刚开口的下一句开头（transducer 出 token 滞后于音频几百毫秒，重置会
    把这些未解码帧扔掉——实测「前面的字就大概率掉」只剩后半）。
    对策：重置后把最后 1.2s 音频重新喂进新流——按 endpoint 定义这段是
    尾部静音（重喂产出零 token，无害）；若里面混着下一句开头，字就活了。
    """
    partial = session.get("partial") or ""
    pcm = bytes(session["utterance_pcm"])
    if recognizer.is_endpoint(session["stream"]):
        recognizer.reset(session["stream"])
    session["partial"] = ""
    session["utterance_pcm"] = bytearray()

    # 每句音频落盘（保留最近 20 条）：用户报丢词时，直接听原始录音就能
    # 分清是「采集端丢了」还是「识别模型吞了」——定位不再靠猜。
    _dump_utterance_pcm(pcm)

    if _contains_clear(partial):
        # 清屏重新开始：行槽、行位计数、句列表全部归零
        session["utterances"] = []
        session["slots"] = []
        session["rows"] = [None] * 16
        session["next_row"] = 0
        return

    utterance_index = len(session["utterances"])
    if partial:
        session["utterances"].append(partial)
        # 行槽写入：这句钉在 next_row 起的固定行位上（回卷覆盖最旧的行）
        pieces = wrap_text(partial)
        start_row = session["next_row"]
        count = _write_rows(session, start_row, pieces)
        session["slots"].append({"start": start_row, "count": count})
        session["next_row"] = (start_row + count) % 16

    # 尾部 1.2s 重喂（见上注释）：救回卡在重置窗口里的下一句开头
    tail = pcm[-int(_PCM_RATE * 1.2) * 2:]
    if tail:
        n = len(tail) // 2
        tail_samples = _normalize_samples(
            list(struct.unpack(f"<{n}h", tail))
        )
        stream = session["stream"]
        stream.accept_waveform(
            _PCM_RATE, [s / 32768.0 for s in tail_samples]
        )
        while recognizer.is_ready(stream):
            recognizer.decode_stream(stream)
        session["partial"] = str(recognizer.get_result(stream) or "").strip()
        session["utterance_pcm"] = bytearray(tail)

    # 音频太短（<0.4s）多半是噪声碎片，不值得花一次精转
    if len(pcm) < _PCM_RATE * 2 * 0.4 or not pcm:
        return

    # 第一精转：本地 SenseVoice（2026-11-12）。0.1s 级、离线、必成功——
    # zipformer 14M 在真实噪声下爱吞词，停顿时用 SenseVoice 整句重转一遍，
    # 丢掉的词当场补回来。千问（第二精转，后台）可用时再升一次精度。
    refined = partial
    try:
        from agent_gateway import local_asr

        if local_asr.is_available():
            sense_text = local_asr.transcribe_pcm(pcm).strip()
            if (
                sense_text
                and not _contains_clear(sense_text)
                and _looks_like_refinement(partial, sense_text)
            ):
                session["utterances"][utterance_index] = sense_text
                _rewrite_rows(session, utterance_index, sense_text)
                refined = sense_text
                print(
                    f"[{time.strftime('%H:%M:%S')}] [LIVE] SenseVoice 精转 "
                    f"#{utterance_index}: {partial!r} -> {sense_text!r}",
                    flush=True,
                )
    except Exception as error:  # noqa: BLE001 - 精转失败保留粗字
        print(f"[LIVE] SenseVoice 精转失败：{error}", flush=True)

    # 第二精转：千问（后台线程，网络调用；成功且像修正时再替换一次）
    _get_finalize_pool().submit(
        _finalize_with_qwen, device_id, utterance_index, refined, pcm
    )


_DUMP_DIR = Path(".data/live_dump")


def _dump_utterance_pcm(pcm: bytes) -> None:
    """把定稿句的原始音频存成 wav，最多保留 20 条（诊断丢词用）。"""
    try:
        out = _DUMP_DIR
        out.mkdir(parents=True, exist_ok=True)
        stamp = time.strftime("%H%M%S")
        path = out / f"utt_{stamp}_{len(pcm) // 32000}.wav"
        with open(path, "wb") as handle:
            handle.write(
                b"RIFF"
                + (len(pcm) + 36).to_bytes(4, "little")
                + b"WAVEfmt "
                + (16).to_bytes(4, "little")
                + (1).to_bytes(2, "little")
                + (1).to_bytes(2, "little")
                + (_PCM_RATE).to_bytes(4, "little")
                + (_PCM_RATE * 2).to_bytes(4, "little")
                + (2).to_bytes(2, "little")
                + (16).to_bytes(2, "little")
                + b"data"
                + len(pcm).to_bytes(4, "little")
            )
            handle.write(pcm)
        # 只留最近 20 条
        dumps = sorted(out.glob("utt_*.wav"))
        for old in dumps[:-20]:
            old.unlink(missing_ok=True)
    except OSError:
        pass  # 诊断落盘失败不影响主链路


def _rewrite_rows(session: dict, index: int, new_text: str) -> None:
    """精转后原地改写某句占用的行槽（行位不变，不做全局重排）。

    新文本行数变少时多余行清空；变多时向后覆盖（牺牲的是更旧的行）。
    """
    slots = session["slots"]
    if index >= len(slots):
        return
    slot = slots[index]
    rows: List[Optional[str]] = session["rows"]
    for offset in range(slot["count"]):
        rows[(slot["start"] + offset) % 16] = None
    slot["count"] = _write_rows(session, slot["start"], wrap_text(new_text))


def _finalize_with_qwen(
    device_id: str, utterance_index: int, rough_text: str, pcm: bytes
) -> None:
    """后台：把这句的完整音频交千问 ASR 精转，完成后原地替换该行。"""
    try:
        from agent_gateway.qwen_asr import QwenAsrClient

        header = (
            b"RIFF"
            + (len(pcm) + 36).to_bytes(4, "little")
            + b"WAVEfmt "
            + (16).to_bytes(4, "little")
            + (1).to_bytes(2, "little")
            + (1).to_bytes(2, "little")
            + (_PCM_RATE).to_bytes(4, "little")
            + (_PCM_RATE * 2).to_bytes(4, "little")
            + (2).to_bytes(2, "little")
            + (16).to_bytes(2, "little")
            + b"data"
            + len(pcm).to_bytes(4, "little")
        )
        import base64

        data_url = "data:audio/wav;base64," + base64.b64encode(
            header + pcm
        ).decode()
        response = QwenAsrClient().transcribe(data_url)
        text = str(
            (response or {}).get("text")
            or (response or {}).get("result")
            or ""
        ).strip()
        if not text or _contains_clear(text):
            return
        # 防幻觉护栏（2026-11-12）：信噪比太低时千问会自由发挥（实测噪声段
        # 「哪个人点火二」被精转成「那你们每个人多少活数？」）。精转文本与
        # 粗字几乎没有公共二元组 = 千问没在「修正」而是在「重写」，保留粗字。
        if rough_text and not _looks_like_refinement(rough_text, text):
            print(
                f"[LIVE] 千问结果与粗字无关（疑似幻觉），保留粗字：{text!r}",
                flush=True,
            )
            return
        with _lock:
            session = _sessions.get(device_id)
            if session is None or utterance_index >= len(session["utterances"]):
                return
            if session["utterances"][utterance_index] == rough_text:
                session["utterances"][utterance_index] = text
                _rewrite_rows(session, utterance_index, text)
                print(
                    f"[{time.strftime('%H:%M:%S')}] [LIVE] 千问精转 #{utterance_index}: "
                    f"{rough_text!r} -> {text!r}",
                    flush=True,
                )
    except Exception as error:  # noqa: BLE001 - 精转失败保留粗字，不影响链路
        print(f"[LIVE] 千问精转失败（保留粗字）：{error}", flush=True)


def _looks_like_refinement(rough: str, refined: str) -> bool:
    """精转文本是否像「对粗字的修正」：至少 1/3 的粗字二元组还在。"""
    if not refined or not rough:
        return False
    bigrams = [rough[i : i + 2] for i in range(len(rough) - 1)]
    if not bigrams:
        return rough[0] in refined
    hits = sum(1 for gram in bigrams if gram in refined)
    return hits >= max(1, len(bigrams) // 3)


def current_frame(device_id: str) -> bytes:
    """当前整屏帧：16 个固定行槽 + 正在说的 partial（追加在下一空行）。"""
    with _lock:
        session = _sessions.get(device_id)
        if session is None:
            return render_rows([None] * 16, note="说话吧…")
        session["last_active"] = time.time()
        rows: List[Optional[str]] = list(session["rows"])
        next_row: int = session["next_row"]
        partial: str = session.get("partial") or ""

    highlight = None
    if partial:
        # 正在说的话写到下一空行起（只影响渲染，不动行槽；定稿时才落槽）
        pieces = wrap_text(partial)
        for offset, piece in enumerate(pieces):
            row = (next_row + offset) % 16
            rows[row] = piece
            highlight = row
    else:
        occupied = [r for r, text in enumerate(rows) if text]
        if occupied:
            highlight = occupied[-1]

    if any(text for text in rows):
        return render_rows(rows, highlight_row=highlight)
    return render_rows([None] * 16, note="说话吧…")


_ROW_HEIGHT = live_screen._LINE_HEIGHT  # 15px
_ROW_BYTES = live_screen.FRAME_WIDTH * _ROW_HEIGHT * 2  # 7200B
_FRAME_ROWS = live_screen.FRAME_HEIGHT // _ROW_HEIGHT  # 16


def frame_diff(device_id: str, full: bool = False) -> bytes:
    """相对上一帧只回变化的行：1 字节行数 + N×(1 字节行号 + 7200B 行数据)。

    整帧 115KB 走一遍要 0.3s+，而每轮变化通常只有 1~3 行——按行差量下发
    后拉帧从 ~0.3s 缩到 ~50ms，设备可以每轮都拉，字幕上屏节奏翻倍。
    full=True（设备拉帧失败后主动要求）忽略差量基线，整屏重发，防止设备
    错过一帧后画面永久错位。
    """
    frame = current_frame(device_id)
    with _lock:
        session = _sessions.get(device_id)
        last = None if full else (session.get("last_frame") if session else None)
        if session is not None:
            session["last_frame"] = frame
    rows = bytearray()
    count = 0
    for row in range(_FRAME_ROWS):
        chunk = frame[row * _ROW_BYTES : (row + 1) * _ROW_BYTES]
        if last is None or last[row * _ROW_BYTES : (row + 1) * _ROW_BYTES] != chunk:
            rows.append(row)
            rows.extend(chunk)
            count += 1
    if last is not None and count == 0:
        return b"\x00\x00"  # 无变化：设备什么都不画
    return bytes([count & 0xFF]) + bytes(rows)


def session_note(device_id: str) -> str:
    """诊断用：当前 partial 文本。"""
    with _lock:
        session = _sessions.get(device_id)
        if session is None:
            return ""
        return str(session.get("partial") or "")
