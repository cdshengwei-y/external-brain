"""本地关键词唤醒检测：sherpa-onnx KWS（声学级），模糊文本匹配的兜底。

为什么要有这个模块
------------------
2026-11-11 现场：用户在正常距离说话，设备端音频电平弱（rms -32~-36dBFS），
SenseVoice 整句转写被切得粉碎（「找好维斯」「贾回事」「我个没关」），
文本模糊匹配只能碰运气。KWS 模型是**声学级关键词检测**（3.3M zipformer，
wenetspeech 训练），对含糊发音的容错和整句 ASR 完全不是一个量级。

模型：sherpa-onnx-kws-zipformer-wenetspeech-3.3M-2024-01-01（int8，
~/.cache/sherpa-asr/），关键词文件 keywords_jiaweisi.txt（拼音 token 格式）：
    j iǎ w éi s ī @贾维斯

用法：``detect_wake_keyword(wav_bytes)`` → True/False。单条 1.2s 探针
约 30~80ms（int8 CPU）。
"""

from __future__ import annotations

from pathlib import Path
from typing import Optional

# 模型目录
_KWS_DIR = Path.home() / ".cache/sherpa-asr/sherpa-onnx-kws-zipformer-wenetspeech-3.3M-2024-01-01"

_spotter = None  # 进程内单例


def is_available() -> bool:
    """模型文件齐全才算可用；LOCAL_ASR_DISABLED=1 一并关闭（测试用）。"""
    import os

    if os.environ.get("LOCAL_ASR_DISABLED"):
        return False
    try:
        import sherpa_onnx  # noqa: F401
    except ImportError:
        return False
    needed = (
        _KWS_DIR / "encoder-epoch-99-avg-1-chunk-16-left-64.int8.onnx",
        _KWS_DIR / "decoder-epoch-99-avg-1-chunk-16-left-64.int8.onnx",
        _KWS_DIR / "joiner-epoch-99-avg-1-chunk-16-left-64.int8.onnx",
        _KWS_DIR / "tokens.txt",
        _KWS_DIR / "keywords_jiaweisi.txt",
    )
    return all(path.is_file() for path in needed)


def _get_spotter():
    global _spotter
    if _spotter is not None:
        return _spotter
    import sherpa_onnx

    _spotter = sherpa_onnx.KeywordSpotter(
        tokens=str(_KWS_DIR / "tokens.txt"),
        encoder=str(_KWS_DIR / "encoder-epoch-99-avg-1-chunk-16-left-64.int8.onnx"),
        decoder=str(_KWS_DIR / "decoder-epoch-99-avg-1-chunk-16-left-64.int8.onnx"),
        joiner=str(_KWS_DIR / "joiner-epoch-99-avg-1-chunk-16-left-64.int8.onnx"),
        keywords_file=str(_KWS_DIR / "keywords_jiaweisi.txt"),
        num_threads=2,
        sample_rate=16000,
        feature_dim=80,
    )
    return _spotter


def detect_wake_keyword(wav_bytes: bytes) -> bool:
    """一段录音里有没有「贾维斯」。解析不了返回 False。"""
    import io
    import struct
    import wave

    try:
        with wave.open(io.BytesIO(wav_bytes)) as handle:
            rate = handle.getframerate()
            frames = handle.readframes(handle.getnframes())
    except (wave.Error, EOFError):
        return False
    n = len(frames) // 2
    if n == 0:
        return False
    samples = struct.unpack(f"<{n}h", frames[: n * 2])
    # 峰值归一化到 -6dBFS，与 ASR 路径一致（KWS 也在弱电平上打折）
    peak = max(abs(s) for s in samples) if samples else 0
    if 0 < peak < 16000:
        gain = min(16000.0 / peak, 20.0)
        samples = [max(-32768, min(32767, int(s * gain))) for s in samples]

    spotter = _get_spotter()
    stream = spotter.create_stream()
    stream.accept_waveform(rate, [s / 32768.0 for s in samples])
    # 喂 0.3s 尾部静音让 transducer 收尾
    stream.accept_waveform(rate, [0.0] * int(rate * 0.3))
    stream.input_finished()
    while spotter.is_ready(stream):
        spotter.decode_stream(stream)
    # sherpa_onnx 2.0.1：结果经 spotter.get_result(stream) 取（stream 上没有
    # is_detected 属性，旧 API 在这个版本永远返回 False）
    return str(spotter.get_result(stream) or "").strip() != ""
