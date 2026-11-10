"""唤醒词的音频前处理。

唤醒链路是「设备录一小段 → 云端 ASR → 关键词匹配」。三字专名对 ASR 很脆弱，
而真正决定成败的是**送进去的是哪一段**：同一段真人录音，窗口差 0.2 秒，
结果就从「查维斯」变成「咋回事？」。

实测（16k 单声道真机录音，`portable_media` 里的原始 wav）：

* 整段（含前后 0.8s 房间底噪）        → 「嗯。」/「咋回事？」
* 首尾静音剪掉，保留 1.9s 跨度        → 「也為。」
* **只留最响的那一簇（±0.2/0.3s）**   → 「查维斯。」→ 命中唤醒

所以这里做的是「**取最响的连续簇**」而不是「剪首尾」：房间底噪、关门声、
远处的谈话都在别的位置，整段送进去等于把噪声一起喂给模型。

只处理 16bit PCM 的 WAV；格式不认识就返回 None，调用方退回原音频。
不依赖 numpy：单声道 16k 三秒也只有 4.8 万个采样点，纯 Python 二十来毫秒。
"""

from __future__ import annotations

import io
import struct
import wave
from typing import Optional, Tuple

# 25ms 一窗求峰值：单点毛刺不会左右门限。
WINDOW_SECONDS = 0.025
# 门限相对整段峰值的比例。太小会把底噪并进簇里，太大会把「贾」削掉。
CROP_RATIO = 0.35
# 门限的绝对下限，避免安静录音里把底噪当语音。
CROP_FLOOR = 200
# 簇内允许几个窗口低于门限（2 窗 = 50ms），把「贾-维-斯」之间的短暂停顿连起来。
CROP_GAP_WINDOWS = 2
# 簇两侧留的气口：前面多留给「贾」的爆破音，后面多留给尾音收口。
PAD_HEAD_SECONDS = 0.20
PAD_TAIL_SECONDS = 0.30
# 剪完短于这个长度就没有识别价值。
MIN_SEGMENT_SECONDS = 0.20
# 归一化目标峰值（占满量程的比例）。0.7 是 ASR 常见的最佳区间，
# 太低模型听不清，太高削波。
TARGET_PEAK_RATIO = 0.7


def _parse_wav(data: bytes) -> Optional[Tuple[int, int, int, int, bytes]]:
    """返回 (声道数, 采样率, 位宽字节, 帧数, PCM 数据)；不支持的格式返回 None。"""
    if not data or len(data) < 44:
        return None
    try:
        reader = wave.open(io.BytesIO(data), "rb")
    except (wave.Error, EOFError, ValueError):
        return None
    try:
        channels = reader.getnchannels()
        sample_rate = reader.getframerate()
        width = reader.getsampwidth()
        frames = reader.getnframes()
        pcm = reader.readframes(frames)
    except (wave.Error, EOFError, ValueError):
        return None
    finally:
        reader.close()
    if width != 2 or not pcm:
        return None
    return channels, sample_rate, width, frames, pcm


def _mono_samples(channels: int, width: int, pcm: bytes) -> Tuple[int, ...]:
    total = len(pcm) // (width * channels)
    if channels == 1:
        return struct.unpack_from("<%dh" % total, pcm, 0)
    return tuple(
        struct.unpack_from("<h", pcm, frame * width * channels)[0]
        for frame in range(total)
    )


def wav_peak(data: bytes) -> int:
    """整段 PCM 的绝对值峰值；解析不了返回 0。"""
    parsed = _parse_wav(data)
    if parsed is None:
        return 0
    channels, _, width, _, pcm = parsed
    samples = _mono_samples(channels, width, pcm)
    return max((abs(value) for value in samples), default=0)


def loudest_span(
    samples: Tuple[int, ...],
    sample_rate: int,
    crop_ratio: float = CROP_RATIO,
    crop_floor: int = CROP_FLOOR,
    gap_windows: int = CROP_GAP_WINDOWS,
    pad_head: float = PAD_HEAD_SECONDS,
    pad_tail: float = PAD_TAIL_SECONDS,
) -> Optional[Tuple[int, int]]:
    """定位「最响的那一簇」语音，返回 (起始帧, 结束帧)；没有可用簇时返回 None。

    从全局最响的 25ms 窗口出发向两侧扩张，遇到低于门限的窗口就停，
    但允许 `gap_windows` 个窗口的短暂掉线（字与字之间的停顿）。
    比「取第一个过线窗口到最后一个过线窗口」稳得多：后者会把整段房间
    噪声一起框进去。
    """
    if not samples or sample_rate <= 0:
        return None
    window = max(1, int(sample_rate * WINDOW_SECONDS))
    total = len(samples)
    peaks = [
        max((abs(value) for value in samples[start : start + window]), default=0)
        for start in range(0, total - window + 1, window)
    ]
    if len(peaks) < 2:
        return None
    loudest = max(peaks)
    # 整段都没过绝对下限，说明只有房间底噪，裁剪没有意义。
    if loudest < crop_floor:
        return None
    threshold = max(int(loudest * crop_ratio), crop_floor)

    peak_index = peaks.index(loudest)
    start_index = peak_index
    gap = 0
    while start_index > 0:
        if peaks[start_index - 1] >= threshold:
            start_index -= 1
            gap = 0
        elif gap < gap_windows:
            start_index -= 1
            gap += 1
        else:
            break
    end_index = peak_index
    gap = 0
    while end_index < len(peaks) - 1:
        if peaks[end_index + 1] >= threshold:
            end_index += 1
            gap = 0
        elif gap < gap_windows:
            end_index += 1
            gap += 1
        else:
            break

    start = max(0, start_index * window - int(pad_head * sample_rate))
    end = min(total, (end_index + 1) * window + int(pad_tail * sample_rate))
    if end - start < int(MIN_SEGMENT_SECONDS * sample_rate):
        return None
    return start, end


def prepare_wake_wav(
    data: bytes,
    crop_ratio: float = CROP_RATIO,
    crop_floor: int = CROP_FLOOR,
    pad_head: float = PAD_HEAD_SECONDS,
    pad_tail: float = PAD_TAIL_SECONDS,
    target_peak_ratio: float = TARGET_PEAK_RATIO,
) -> Optional[bytes]:
    """裁出最响的一簇语音并做峰值归一化，返回新的 WAV；不值得处理时返回 None。

    取**第一声道**做检测（设备上传的是单声道；万一以后是立体声，所有声道
    按同一增益缩放，不会破音）。
    """
    parsed = _parse_wav(data)
    if parsed is None:
        return None
    channels, sample_rate, width, frames, pcm = parsed
    usable = frames * width * channels
    pcm = pcm[:usable]
    if len(pcm) < sample_rate * width * channels // 2:  # 不足 0.5 秒，没有裁剪余量
        return None

    samples = _mono_samples(channels, width, pcm)
    span = loudest_span(
        samples,
        sample_rate,
        crop_ratio=crop_ratio,
        crop_floor=crop_floor,
        pad_head=pad_head,
        pad_tail=pad_tail,
    )
    if span is None:
        return None
    start, end = span

    peak = max((abs(value) for value in samples[start:end]), default=0)
    if peak <= 0:
        return None

    gain = (target_peak_ratio * 32767.0) / float(peak)
    if 0.98 <= gain <= 1.02:
        gain = 1.0
    gain = min(gain, 32767.0 / float(peak))

    chunk = pcm[start * width * channels : end * width * channels]
    if gain == 1.0:
        scaled = chunk
    else:
        values = struct.unpack("<%dh" % (len(chunk) // width), chunk)
        scaled = struct.pack(
            "<%dh" % len(values),
            *(max(-32768, min(32767, int(value * gain))) for value in values),
        )

    buffer = io.BytesIO()
    writer = wave.open(buffer, "wb")
    writer.setnchannels(channels)
    writer.setsampwidth(width)
    writer.setframerate(sample_rate)
    writer.writeframes(scaled)
    writer.close()
    return buffer.getvalue()
