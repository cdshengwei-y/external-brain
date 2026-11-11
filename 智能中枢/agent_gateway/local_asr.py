"""本机离线语音识别：不依赖 153 的远程 ASR 服务。

为什么要有这个模块
------------------
2026-11-11 现场：153 的千问 ASR 服务解码后端挂了（NoBackendError，对
已知清晰样本稳定返回 500/幻觉文本），用户喊什么都识别成「嗯」，而那台
机器用户够不到。这台 Mac 上本来就装着 sherpa_onnx（本地 TTS 同源），
同样能跑离线识别 —— 把「听」搬回本机，语音链路从 mic 到 TTS 全程本地化，
153 从此只是备用而不是单点。

模型：sherpa-onnx-paraformer-zh-small-2024-03-09（74MB int8，中文，
在 ~/.cache/sherpa-asr/），下载解压一次即可：
    curl -L -o paraformer-small.tar.bz2 \
      https://github.com/k2-fsa/sherpa-onnx/releases/download/asr-models/sherpa-onnx-paraformer-zh-small-2024-03-09.tar.bz2
    tar xjf paraformer-small.tar.bz2 -C ~/.cache/sherpa-asr/

运行环境：必须用带 sherpa_onnx 的解释器（中枢的 3.13），不是系统 3.9。
"""

from __future__ import annotations

import struct
import wave
from pathlib import Path
from typing import Optional

# 模型目录：默认 ~/.cache/sherpa-asr/<模型名>，可用环境变量覆盖。
# 优先 SenseVoice（对中文短句/噪声更鲁棒），缺文件时退回 paraformer-small。
_SENSE_VOICE_DIR = Path.home() / ".cache/sherpa-asr/sherpa-onnx-sense-voice-zh-en-ja-ko-yue-int8-2024-07-17"
DEFAULT_MODEL_DIR = Path.home() / ".cache/sherpa-asr/sherpa-onnx-paraformer-zh-small-2024-03-09"
MODEL_DIR_ENV = "LOCAL_ASR_MODEL_DIR"

_recognizer = None  # 进程内单例：模型加载约 1~2s，只做一次
_loaded_kind: Optional[str] = None  # "sense-voice" / "paraformer"


def model_dir() -> Optional[Path]:
    import os

    explicit = os.environ.get(MODEL_DIR_ENV)
    if explicit and Path(explicit).is_dir():
        return Path(explicit)
    if (_SENSE_VOICE_DIR / "model.int8.onnx").is_file():
        return _SENSE_VOICE_DIR
    if (DEFAULT_MODEL_DIR / "model.int8.onnx").is_file():
        return DEFAULT_MODEL_DIR
    return None


def model_kind() -> Optional[str]:
    directory = model_dir()
    if directory is None:
        return None
    return "sense-voice" if directory == _SENSE_VOICE_DIR else "paraformer"


def is_available() -> bool:
    """模型文件在不在。sherpa_onnx 缺失也算不可用（调用方回退远程）。"""
    try:
        import sherpa_onnx  # noqa: F401
    except ImportError:
        return False
    directory = model_dir()
    return directory is not None and (directory / "model.int8.onnx").is_file()


def _get_recognizer():
    global _recognizer, _loaded_kind
    if _recognizer is not None:
        return _recognizer
    import sherpa_onnx

    directory = model_dir()
    if directory is None:
        raise RuntimeError("本地 ASR 模型目录不存在")
    # sherpa_onnx 2.0.1：构造器改为按模型的 from_xxx 工厂方法
    if directory == _SENSE_VOICE_DIR:
        _recognizer = sherpa_onnx.OfflineRecognizer.from_sense_voice(
            model=str(directory / "model.int8.onnx"),
            tokens=str(directory / "tokens.txt"),
            num_threads=2,
            use_itn=True,  # 数字直接出阿拉伯数字，「现在几点」回复更好念
        )
        _loaded_kind = "sense-voice"
    else:
        _recognizer = sherpa_onnx.OfflineRecognizer.from_paraformer(
            paraformer=str(directory / "model.int8.onnx"),
            tokens=str(directory / "tokens.txt"),
            num_threads=2,
        )
        _loaded_kind = "paraformer"
    return _recognizer
    return _recognizer


def transcribe_pcm(pcm_bytes: bytes, sample_rate: int = 16000) -> str:
    """裸 s16le 单声道 PCM → 文本。空音频返回空串。"""
    recognizer = _get_recognizer()
    n = len(pcm_bytes) // 2
    if n == 0:
        return ""
    samples = struct.unpack(f"<{n}h", pcm_bytes[: n * 2])
    stream = recognizer.create_stream()
    stream.accept_waveform(sample_rate, [s / 32768.0 for s in samples])
    recognizer.decode_stream(stream)
    return str(stream.result.text or "").strip()


def transcribe_wav(wav_bytes: bytes) -> str:
    """WAV（设备上传的原始格式）→ 文本。带头/不带头都兼容。"""
    import io

    try:
        with wave.open(io.BytesIO(wav_bytes)) as handle:
            rate = handle.getframerate()
            pcm = handle.readframes(handle.getnframes())
        return transcribe_pcm(pcm, rate)
    except Exception:
        return transcribe_pcm(wav_bytes)
