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

import math
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

# 音量门控（2026-11-12 二版）：曾用 -33dBFS 挡远场噪声，实测把用户自己的
# 话也误杀了（座位距离说话 rms -30~-37，与远场重叠，绝对电平分不开）。
# 现降到 -45 只挡纯数字静音；远场乱码行靠清屏词收紧（≤12字）兜底。
_SPEAK_RMS_DBFS = -45.0     # 单块判定「有人说话」（partial 显示门控）
_MIN_UTTERANCE_RMS = -45.0  # 整句下限：只挡纯静音/全零块


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
            "beep_pending": False,  # 唤醒词命中 → 设备滴一声
            "beep_armed": True,     # 每句只响一次，endpoint 重新武装
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


def reset_session(device_id: str, reason: str = "") -> None:
    with _lock:
        print(
            f"[{time.strftime('%H:%M:%S')}] [LIVE] 会话重置 device={device_id} "
            f"reason={reason or '未标注'}",
            flush=True,
        )
        _sessions.pop(device_id, None)
        _new_stream(device_id)


def _contains_clear(text: str) -> bool:
    """清屏指令判定（收紧版）：只认 ≤12 字的短句——长句/乱码里混到清屏词
    一律不算，否则远场幻听能把整屏字幕误清掉。"""
    t = str(text or "").strip().strip("。.，,！!？? ")
    if not t or len(t) > 12:
        return False
    return any(word in t for word in _CLEAR_WORDS)


def _rms_dbfs(pcm_bytes: bytes) -> float:
    """整段 PCM 的 RMS 电平（dBFS）。空音频返回 -99。"""
    n = len(pcm_bytes) // 2
    if n == 0:
        return -99.0
    samples = struct.unpack(f"<{n}h", pcm_bytes[: n * 2])
    rms = math.sqrt(sum(s * s for s in samples) / n)
    return 20 * math.log10(rms / 32768) if rms > 0 else -99.0


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
        raw = struct.unpack(f"<{n}h", pcm_bytes[: n * 2])
        # 原始电平判定「近处有人说话」：必须在归一化之前算（归一化会抹掉音量差）
        raw_rms = math.sqrt(sum(s * s for s in raw) / n)
        raw_rms_db = 20 * math.log10(raw_rms / 32768) if raw_rms > 0 else -99.0
        if raw_rms_db >= _SPEAK_RMS_DBFS:
            session["last_speech_ts"] = time.time()
        samples = _normalize_samples(list(raw))
        stream = session["stream"]
        # 必须归一到 [-1, 1] 浮点：整型直喂振幅超万倍，识别直接乱码
        stream.accept_waveform(_PCM_RATE, [s / 32768.0 for s in samples])
        while recognizer.is_ready(stream):
            recognizer.decode_stream(stream)
        partial = str(recognizer.get_result(stream) or "").strip()
        # 诊断日志（2026-11-13）：只在识别文本变化时打——hub 识别器曾出现
        # 「进程活着但 partial 恒空」的卡死态，重启才愈。这条日志用于下次
        # 快速定位是「没收到音频」还是「收到但没解码」。
        if partial != session.get("_prev_partial"):
            session["_prev_partial"] = partial
            print(
                f"[{time.strftime('%H:%M:%S')}] [LIVE] decode dev={device_id} "
                f"rms={raw_rms_db:.1f} partial={partial!r}",
                flush=True,
            )
        session["partial"] = partial

        # 唤醒词命中探针（2026-11-12）：流式识别一命中「贾维斯」（含模糊
        # 变体）就让设备滴一声 0.5s——用户用「说 N 次数 N 声」测识别率。
        # 每句只响一次（endpoint 定稿后重新武装）+ 3 秒冷却：定稿后的尾部
        # 重喂会把上一句尾巴重新解码成唤醒词片段，没有冷却会连响两声。
        now_ts = time.time()
        if (
            session.get("beep_armed")
            and partial
            and now_ts - session.get("last_beep_ts", 0) >= 3.0
            and _wake_hit(partial)
        ):
            session["beep_pending"] = True
            session["beep_armed"] = False
            session["last_beep_ts"] = now_ts
            print(
                f"[{time.strftime('%H:%M:%S')}] [LIVE] 唤醒词命中（设备将滴声）: {partial!r}",
                flush=True,
            )

        # 单句音频累计（供千问精转）；超 20s 强制定稿防内存无限涨
        session["utterance_pcm"].extend(pcm_bytes)
        max_pcm = int(_MAX_UTTERANCE_SECONDS * _PCM_RATE) * 2
        endpoint = recognizer.is_endpoint(stream)
        if endpoint or len(session["utterance_pcm"]) >= max_pcm:
            try:
                _finalize_utterance(device_id, session, recognizer)
            except Exception as error:  # noqa: BLE001
                # finalize 崩了绝不能让字幕链路静默死亡（2026-11-13 教训：
                # 沙箱钩子炸掉 dump 轮转时，partial 被清、无任何日志、设备
                # 收 500，症状就是「说完屏幕没反应」）。打日志、保数据。
                print(
                    f"[{time.strftime('%H:%M:%S')}] [LIVE] finalize 异常: "
                    f"{type(error).__name__}: {error}",
                    flush=True,
                )
                try:
                    session["utterance_pcm"].extend(pcm)  # 丢了的这句留待下轮
                except Exception:  # noqa: BLE001
                    pass


# 同音字集（2026-11-12）：用户实测「贾维斯」被听成 价位/假卫诗/加微/家里斯/
# 较为/贾维师 等——逐个硬编码追不完，改用三个同音字集 + 邻接规则覆盖。
_JIA_CHARS = "贾家假加价甲架嘉佳驾稼叫较教交角"
_WEI_CHARS = "维威伟为卫微位围唯委韦未味喂魏纬"
# 与「维/威/伟」相邻的 sī/shī 字（为什么的「什」不在此列，防高频误触）
_SI_ADJ_CHARS = "斯师诗士司丝私"
# 「贾_ _斯」隔一字规则用的 sī/shī 字（距离=2，假释/加斯这类贴邻不触发）。
# 「司」不在此列：「上市公司」「开家公司」这类高频词会误触（11-13 实测）。
_SI_FAR_CHARS = "斯师士诗使史市石"

def _wake_hit(text: str) -> bool:
    """流式 partial / 定稿文本里是否出现唤醒词「贾维斯」。

    2026-11-13 收紧：不再套用 portable 的模糊匹配表——它把远场电视声
    「我刚睡觉也会改造家公司」都误判成命中（滴声误报）。只留两道严格
    规则：精确词 + 同音字集邻接规则（昨天 9 个实测变体全覆盖）。
    """
    text = str(text or "")
    if "贾维斯" in text:
        return True
    chars = [c for c in text if "\u4e00" <= c <= "\u9fff"]
    for i in range(len(chars) - 1):
        first, second = chars[i], chars[i + 1]
        if first in _JIA_CHARS and second in _WEI_CHARS:
            return True
        if first in _WEI_CHARS and second in _SI_ADJ_CHARS:
            return True
        if (
            i + 2 < len(chars)
            and first in _JIA_CHARS
            and chars[i + 2] in _SI_FAR_CHARS
        ):
            return True
    return False


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
    session["beep_armed"] = True  # 句边界：重新武装滴声探针

    # 远场噪声防线：整句能量低于 -33dBFS = 房间远处的说话/电视。增益放大
    # 后这种音频被流式引擎大量幻听（用户实测连不成句 + 乱码行），不上屏、
    # 不精转、不判清屏——只在日志里留痕。
    rms_db = _rms_dbfs(pcm)
    if rms_db < _MIN_UTTERANCE_RMS:
        print(
            f"[{time.strftime('%H:%M:%S')}] [LIVE] 远场噪声句丢弃 "
            f"rms={rms_db:.1f}dBFS partial={partial!r}",
            flush=True,
        )
        return

    # 每句音频落盘（保留最近 20 条）：用户报丢词时，直接听原始录音就能
    # 分清是「采集端丢了」还是「识别模型吞了」——定位不再靠猜。
    _dump_utterance_pcm(pcm)

    print(
        f"[{time.strftime('%H:%M:%S')}] [LIVE] 定稿 rms={rms_db:.1f}dBFS "
        f"text={partial!r}",
        flush=True,
    )

    if _contains_clear(partial):
        # 清屏重新开始：行槽、行位计数、句列表全部归零
        print(f"[{time.strftime('%H:%M:%S')}] [LIVE] 命中清屏指令", flush=True)
        session["utterances"] = []
        session["slots"] = []
        session["rows"] = [None] * 16
        session["next_row"] = 0
        return

    utterance_index = len(session["utterances"])
    if partial:
        session["utterances"].append(partial)
        # 行槽写入：这句钉在 next_row 起的固定行位上。
        # 说满一整页自动清屏（2026-11-12 用户要求）：句子写到底部还放不下
        # 时，清空整页从第 0 行重开——不再挤成一团乱麻。
        pieces = wrap_text(partial)
        start_row = session["next_row"]
        if start_row + len(pieces) > 16:
            session["rows"] = [None] * 16
            session["slots"] = []
            session["utterances"] = []
            session["next_row"] = 0
            utterance_index = 0
            start_row = 0
            print(f"[{time.strftime('%H:%M:%S')}] [LIVE] 屏幕已满，自动清屏重开", flush=True)
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

    # 定稿命中补判（2026-11-12）：流式 partial 常把「贾维斯」拆成碎片漏检，
    # SenseVoice 精转是全句最准的转写——这里用完整词匹配再判一次（不用碎片
    # 匹配：精转文本里偶发的「甲为」类碎片会误报）。
    # 3s 冷却与 partial 命中的那次滴声互斥，不会双响。
    now_ts = time.time()
    if refined and now_ts - session.get("last_beep_ts", 0) >= 3.0:
        hit = _wake_hit(refined)
        if hit:
            session["beep_pending"] = True
            session["beep_armed"] = False
            session["last_beep_ts"] = now_ts
            print(
                f"[{time.strftime('%H:%M:%S')}] [LIVE] 定稿命中唤醒词（设备将滴声）: {refined!r}",
                flush=True,
            )

    # 第二精转：千问（后台线程，网络调用；成功且像修正时再替换一次）
    _get_finalize_pool().submit(
        _finalize_with_qwen, device_id, utterance_index, refined, pcm
    )


_DUMP_DIR = Path(".data/live_dump")


_DUMP_SLOTS = 20
_dump_slot_counter = 0


def _dump_utterance_pcm(pcm: bytes) -> None:
    """把定稿句的原始音频存成 wav，最多保留 20 条（诊断丢词用）。

    2026-11-13 改为固定槽位轮转覆写（utt_00..utt_19.wav）：旧实现每句
    unlink 最旧的文件——WorkBuddy 沙箱的 safe-delete 钩子会拦截 hub 进程
    里的 unlink，把 finalize 炸掉（dump 写了、定稿 print/行写入/SenseVoice
    全没跑、设备收 500）。覆写不删除，钩子无从拦截。异常一律吞掉：落盘
    只是诊断工具，绝不能影响主链路。
    """
    global _dump_slot_counter
    try:
        out = _DUMP_DIR
        out.mkdir(parents=True, exist_ok=True)
        path = out / f"utt_{_dump_slot_counter % _DUMP_SLOTS:02d}.wav"
        _dump_slot_counter += 1
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
    except Exception:  # noqa: BLE001 - 落盘失败绝不能影响字幕主链路
        pass


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
        # partial 显示门控：近处 1.5s 内没出现过响亮人声，partial 多半是
        # 远场噪声幻听——不上屏（定稿行槽不受影响）。
        last_speech = session.get("last_speech_ts") or 0
        show_partial = (time.time() - last_speech) < 1.5

    highlight = None
    if partial and show_partial:
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
    """相对上一帧只回变化的行：[标志 1B][行数 1B] + N×(1B 行号 + 7200B 行数据)。

    flags bit0 = 唤醒词命中（设备滴一声 0.5s）。取走即清零，一句只响一次。
    整帧 115KB 走一遍要 0.3s+，而每轮变化通常只有 1~3 行——按行差量下发
    后拉帧从 ~0.3s 缩到 ~50ms，设备可以每轮都拉，字幕上屏节奏翻倍。
    full=True（设备拉帧失败后主动要求）忽略差量基线，整屏重发，防止设备
    错过一帧后画面永久错位。
    """
    frame = current_frame(device_id)
    with _lock:
        session = _sessions.get(device_id)
        beep_flag = 0
        if session is not None and session.get("beep_pending"):
            beep_flag = 1
            session["beep_pending"] = False  # 取走即清：一句只滴一声
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
        return bytes([beep_flag, 0])  # 无变化：设备只处理滴声标志
    return bytes([beep_flag, count & 0xFF]) + bytes(rows)


def session_note(device_id: str) -> str:
    """诊断用：当前 partial 文本。"""
    with _lock:
        session = _sessions.get(device_id)
        if session is None:
            return ""
        return str(session.get("partial") or "")
