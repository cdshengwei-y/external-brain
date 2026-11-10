"""本机语音合成：把回复文本变成 16 kHz 单声道 WAV。

用途：RK3588 的 TTS 不在线时，宿主这一端仍然能把「文字回复」变成「能听见的声音」，
让「说话 -> 识别 -> 回复 -> 播放」在单机上先闭环，不必等设备侧就绪。

实现用 macOS 自带的 `say`（无需联网、无需密钥）。没有 `say` 的平台返回不可用，
调用方应降级成「只回文本」。
"""

from __future__ import annotations

import io
import shutil
import subprocess
import tempfile
import wave
from pathlib import Path
from typing import Optional, Tuple


class LocalTtsError(RuntimeError):
    pass


# 常见的中文语音，按偏好排序；缺失时退回系统第一个 zh_CN 语音。
# 前面的都是女声：默认音色必须是女声，掉到 Eddy/Reed 这类男声听感很怪。
PREFERRED_VOICES = ("Tingting", "Meijia", "Sandy", "Shelley", "Flo", "Sinji")


def say_available() -> bool:
    return shutil.which("say") is not None


def list_chinese_voices() -> list[str]:
    """列出系统里可用的中文语音名。

    `say -v ?` 输出的是 `Eddy (中文（中国大陆）)  zh_CN  # ...`，
    名字后面带语言后缀。这里必须只取括号前的名字，否则
    `PREFERRED_VOICES` 永远匹配不上，最后退回列表第一个（正好是男声 Eddy）。
    """
    if not say_available():
        return []
    try:
        output = subprocess.run(
            ["say", "-v", "?"],
            capture_output=True,
            text=True,
            timeout=10,
        ).stdout
    except (OSError, subprocess.SubprocessError):
        return []
    voices: list[str] = []
    for line in output.splitlines():
        if "zh_CN" not in line:
            continue
        name = line.split("  ", 1)[0].strip()
        base = name.split(" (", 1)[0].strip()
        if base and base not in voices:
            voices.append(base)
    return voices


def pick_voice(preferred: Optional[str] = None) -> Optional[str]:
    available = list_chinese_voices()
    if preferred:
        return preferred
    for name in PREFERRED_VOICES:
        if name in available:
            return name
    return available[0] if available else None


class MacSaySpeechProvider:
    """SpeechSynthesisProvider 的本地实现（接口与 RK3588SpeechProvider 一致）。"""

    def __init__(
        self,
        voice: Optional[str] = None,
        rate: int = 190,
        timeout_seconds: float = 30.0,
    ) -> None:
        self.voice = pick_voice(voice)
        self.rate = max(80, min(int(rate), 320))
        self.timeout_seconds = timeout_seconds

    @property
    def available(self) -> bool:
        return say_available() and self.voice is not None

    def synthesize(self, text: str) -> Tuple[bytes, str, str]:
        normalized = str(text or "").strip()
        if not normalized:
            raise LocalTtsError("没有可合成的文字")
        if not say_available():
            raise LocalTtsError("当前平台没有 say 命令，无法本地合成语音")
        if self.voice is None:
            raise LocalTtsError("系统里没有可用的中文语音")

        with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as handle:
            output_path = Path(handle.name)
        try:
            command = [
                "say",
                "-v",
                self.voice,
                "-r",
                str(self.rate),
                "-o",
                str(output_path),
                "--data-format=LEI16@16000",
                normalized,
            ]
            try:
                completed = subprocess.run(
                    command,
                    capture_output=True,
                    timeout=self.timeout_seconds,
                )
            except subprocess.TimeoutExpired as error:
                raise LocalTtsError("本地语音合成超时") from error
            except OSError as error:
                raise LocalTtsError(f"无法启动 say：{error}") from error
            if completed.returncode != 0:
                detail = completed.stderr.decode("utf-8", "replace").strip()
                raise LocalTtsError(f"say 合成失败：{detail or completed.returncode}")
            audio = output_path.read_bytes()
        finally:
            try:
                output_path.unlink()
            except OSError:
                pass

        if not audio:
            raise LocalTtsError("本地语音合成为空音频")
        return normalize_wav(audio), "wav", "audio/wav"


def normalize_wav(audio: bytes) -> bytes:
    """统一成 16 kHz / 16 bit / 单声道，和录音豆、桌宠的上行格式对齐。"""
    try:
        with wave.open(io.BytesIO(audio), "rb") as source:
            channels = source.getnchannels()
            width = source.getsampwidth()
            rate = source.getframerate()
            frames = source.readframes(source.getnframes())
    except (wave.Error, EOFError):
        return audio

    if width != 2:
        return audio

    if channels == 2:
        samples = memoryview(frames).cast("h")
        mono = bytearray()
        for index in range(0, len(samples) - 1, 2):
            mono += int((samples[index] + samples[index + 1]) / 2).to_bytes(
                2, "little", signed=True
            )
        frames = bytes(mono)
        channels = 1

    if rate != 16000 and channels == 1:
        frames = _resample_pcm16(frames, rate, 16000)

    # 即使已经是 16k/16bit/单声道也要重封装：macOS say 写出来的 WAV 会夹一个
    # `JUNK` chunk，设备端按 44 字节固定偏移找 `data` 会失败（实测播不出声）。
    buffer = io.BytesIO()
    with wave.open(buffer, "wb") as target:
        target.setnchannels(1)
        target.setsampwidth(2)
        target.setframerate(16000)
        target.writeframes(frames)
    return buffer.getvalue()


def _resample_pcm16(frames: bytes, source_rate: int, target_rate: int) -> bytes:
    """线性插值降采样，够用就好，不追求音质。"""
    if source_rate <= 0 or target_rate <= 0 or source_rate == target_rate:
        return frames
    samples = memoryview(frames).cast("h")
    total = len(samples)
    if total == 0:
        return frames
    ratio = source_rate / target_rate
    out_len = int(total / ratio)
    out = bytearray()
    for index in range(out_len):
        position = index * ratio
        left = int(position)
        right = min(left + 1, total - 1)
        weight = position - left
        value = samples[left] * (1 - weight) + samples[right] * weight
        out += int(value).to_bytes(2, "little", signed=True)
    return bytes(out)
