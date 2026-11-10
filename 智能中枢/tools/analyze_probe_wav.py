#!/usr/bin/env python3
"""量化分析探针录制的 WAV，判断它到底是不是可用语音。

背景：桌宠音频调试长期靠"听感"判断，导致结论反复。这个脚本把每次录音转成
可比较的硬指标，重点是三个此前被忽略的量：

1. 静音底噪：整段录音里最安静的 100ms 有多响。真实语音一定有明显停顿，
   60 秒录音的最低电平通常低于 -45 dBFS；如果最低只有 -18 dBFS，说明这段
   录音里根本没有"安静"，要么麦克风没信号只剩噪声，要么增益把底噪顶满。
2. 包络起伏：语音是音节级的，20ms 帧间电平变化平均 3~8 dB；恒定噪声
   只有 0.1 dB 以下。这个值超过 2 dB 才可能有可辨识语音。
3. 相邻采样重复率：<5% 才正常；60% 以上说明 I2S/编解码器时钟比例不匹配。

用法：
    python3 tools/analyze_probe_wav.py /Users/jh-05/esp/esp_sr_probe/*.wav
    python3 tools/analyze_probe_wav.py xxx.wav --verbose
"""

from __future__ import annotations

import argparse
import math
import struct
import sys
import wave
from dataclasses import dataclass
from typing import List, Optional, Tuple

SPEECH_BANDS = (
    (0, 100, "低频隆隆"),
    (100, 300, "基频"),
    (300, 1000, "语音主体"),
    (1000, 3000, "辅音/清晰度"),
    (3000, 6000, "齿音"),
    (6000, 8000, "高频噪声"),
)


@dataclass
class Report:
    path: str
    seconds: float
    rms_dbfs: float
    peak_dbfs: float
    clip_percent: float
    repeat_percent: float
    floor_100ms: float
    dynamic_range: float
    envelope_delta: float
    band_share: List[Tuple[int, int, str, float]]


def load_mono(path: str) -> Tuple[List[int], int]:
    with wave.open(path, "rb") as handle:
        channels = handle.getnchannels()
        width = handle.getsampwidth()
        rate = handle.getframerate()
        frames = handle.getnframes()
        raw = handle.readframes(frames)
    if width != 2:
        raise ValueError(f"{path}: 只支持 16 bit WAV，实际 {width * 8} bit")
    samples = list(struct.unpack("<%dh" % (len(raw) // 2), raw))
    if channels > 1:
        samples = samples[0::channels]
    return samples, rate


def dbfs(value: float) -> float:
    return 20.0 * math.log10(max(value, 1e-9) / 32768.0)


def frame_levels(samples: List[int], frame: int) -> List[float]:
    levels = []
    for start in range(0, len(samples) - frame + 1, frame):
        block = samples[start:start + frame]
        power = sum(x * x for x in block) / frame
        levels.append(dbfs(math.sqrt(power)))
    return levels


def fft(values: List[complex]) -> List[complex]:
    data = values[:]
    n = len(data)
    j = 0
    for i in range(1, n):
        bit = n >> 1
        while j & bit:
            j ^= bit
            bit >>= 1
        j |= bit
        if i < j:
            data[i], data[j] = data[j], data[i]
    length = 2
    while length <= n:
        step = complex(math.cos(-2 * math.pi / length), math.sin(-2 * math.pi / length))
        for start in range(0, n, length):
            w = 1 + 0j
            for k in range(start, start + length // 2):
                u = data[k]
                v = data[k + length // 2] * w
                data[k] = u + v
                data[k + length // 2] = u - v
                w *= step
        length <<= 1
    return data


def band_shares(samples: List[int], rate: int) -> List[Tuple[int, int, str, float]]:
    size = 8192
    if len(samples) < size:
        return []
    # 取信号最响的一段做频谱，避免落在静音区
    levels = frame_levels(samples, size // 4)
    if not levels:
        return []
    best = max(range(len(levels)), key=lambda i: levels[i])
    start = min(best * (size // 4), len(samples) - size)
    segment = samples[start:start + size]
    mean = sum(segment) / size
    windowed = [
        complex((x - mean) * (0.5 - 0.5 * math.cos(2 * math.pi * i / (size - 1))))
        for i, x in enumerate(segment)
    ]
    spectrum = fft(windowed)[: size // 2]
    magnitude = [abs(x) for x in spectrum]
    total = sum(m * m for m in magnitude[1:])
    if total <= 0:
        return []
    shares = []
    for low, high, label in SPEECH_BANDS:
        lo = max(1, int(low * size / rate))
        hi = min(size // 2, int(high * size / rate))
        energy = sum(magnitude[i] ** 2 for i in range(lo, hi))
        shares.append((low, high, label, energy / total * 100.0))
    return shares


def analyze(path: str) -> Optional[Report]:
    try:
        samples, rate = load_mono(path)
    except Exception as error:  # noqa: BLE001 - 逐文件报告，不中断整批
        print(f"{path}: 读取失败 {error}", file=sys.stderr)
        return None
    if not samples:
        print(f"{path}: 空音频", file=sys.stderr)
        return None

    peak = max(abs(x) for x in samples)
    rms = math.sqrt(sum(x * x for x in samples) / len(samples))
    clipped = sum(1 for x in samples if abs(x) >= 32700) / len(samples) * 100.0
    repeats = sum(1 for i in range(1, len(samples)) if samples[i] == samples[i - 1])
    repeat_percent = repeats / max(len(samples) - 1, 1) * 100.0

    levels = frame_levels(samples, max(1, rate // 10))
    ordered = sorted(levels)
    floor = ordered[int(len(ordered) * 0.05)] if ordered else -120.0
    loud = ordered[int(len(ordered) * 0.95)] if ordered else -120.0

    fine = frame_levels(samples, max(1, rate // 50))
    deltas = [abs(fine[i] - fine[i - 1]) for i in range(1, len(fine))]
    envelope = sum(deltas) / len(deltas) if deltas else 0.0

    return Report(
        path=path,
        seconds=len(samples) / rate,
        rms_dbfs=dbfs(rms),
        peak_dbfs=dbfs(peak),
        clip_percent=clipped,
        repeat_percent=repeat_percent,
        floor_100ms=floor,
        dynamic_range=loud - floor,
        envelope_delta=envelope,
        band_share=band_shares(samples, rate),
    )


def verdict(report: Report) -> str:
    if report.repeat_percent > 20:
        return "时钟不匹配（采样重复）"
    if report.floor_100ms > -35:
        return "无静音段，疑似底噪/无麦克风信号"
    if report.envelope_delta < 2.0:
        return "包络过平，无可辨识语音"
    if report.clip_percent > 0.05:
        return "含语音但削波"
    return "含可用语音"


def main() -> int:
    parser = argparse.ArgumentParser(description="量化分析探针录音是否可用")
    parser.add_argument("wav", nargs="+", help="一个或多个 16 bit WAV")
    parser.add_argument("--verbose", action="store_true", help="额外输出频段能量分布")
    args = parser.parse_args()

    reports = [r for r in (analyze(path) for path in args.wav) if r]
    if not reports:
        return 1

    header = (
        f"{'文件':44s} {'时长':>6s} {'RMS':>7s} {'峰值':>7s} {'削波%':>7s} "
        f"{'重复%':>7s} {'底噪':>7s} {'动态':>6s} {'包络':>6s}  判定"
    )
    print(header)
    print("-" * len(header) * 2)
    for r in reports:
        name = r.path.rsplit("/", 1)[-1]
        print(
            f"{name:44s} {r.seconds:6.1f} {r.rms_dbfs:7.1f} {r.peak_dbfs:7.1f} "
            f"{r.clip_percent:7.3f} {r.repeat_percent:7.1f} {r.floor_100ms:7.1f} "
            f"{r.dynamic_range:6.1f} {r.envelope_delta:6.2f}  {verdict(r)}"
        )

    print()
    print("判定依据：重复% >20 → I2S/MCLK 比例错；底噪 >-35 dBFS → 全程没有真正安静；")
    print("          包络 <2 dB → 信号恒定，不像说话；削波 >0.05% → 增益过高。")

    if args.verbose:
        for r in reports:
            print()
            print(f"{r.path}:")
            for low, high, label, share in r.band_share:
                bar = "#" * int(share / 2)
                print(f"  {low:5d}-{high:5d} Hz {label:8s} {share:5.1f}% {bar}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
