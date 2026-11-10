"""重新生成固件内置的「我在」唤醒应答音频。

设备收到 `session_action=awake` 时播的是固件里内嵌的一段 PCM（喊完立刻应声，
不走网络）。这段音频必须是**和中枢回复同一个 TTS 音色**，否则用户会听出
「我在」和后面那句不是同一个人在说话。

    python3 tools/make_wake_clip.py                 # 用默认文本「我在」
    python3 tools/make_wake_clip.py --text "我在呢"  # 换文本
    python3 tools/make_wake_clip.py --speed 0.7     # 改语速（<1 更慢）
    python3 tools/make_wake_clip.py --dry-run       # 只合成到 /tmp 不写固件

产出直接覆盖 firmware/sparkbot_wearable/include/wozai_audio.h，
之后重新 `pio run -e pet_voice -t upload` 烧录才会生效。
"""

from __future__ import annotations

import argparse
import sys
import wave
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from agent_gateway.local_neural_tts import (  # noqa: E402
    LocalNeuralSpeechProvider,
    find_model_dir,
)

HEADER_PATH = ROOT / "firmware/sparkbot_wearable/include/wozai_audio.h"
SYMBOL = "_tmp_sparkbot_wozai_20261106_pcm"
TARGET_RMS_DBFS = -20.4  # 与中枢回复音频一致（实测 reply rms）
PEAK_CEILING = 0.7       # 峰值上限，和录音链路的归一化一致
# 「我在」只有两个字，speed=1.0 合成出来约 0.54s，听感是「抢答」。
# 0.78 拉到 0.7s 左右，接近人应声的节奏；再慢就会显得迟钝。
DEFAULT_SPEED = 0.78
HEAD_SILENCE = 0.060     # 起播先留白，避免第一个字被功放上电瞬间吃掉、听起来更"冲"
TAIL_SILENCE = 0.200     # 尾部留白，避免最后一个字被 I2S 关断切掉


def synth(text: str, model_dir: str | None, speed: float) -> tuple[np.ndarray, int]:
    resolved = find_model_dir(model_dir)
    if resolved is None:
        raise SystemExit(
            "没找到本地神经 TTS 模型。先下载 vits-piper-zh_CN-xiao_ya-medium，"
            "或用 --model-dir 指定。"
        )
    provider = LocalNeuralSpeechProvider(model_dir=str(resolved), speed=speed)
    if not provider.available:
        raise SystemExit(f"模型加载失败：{provider.error}")

    audio_bytes, _, _ = provider.synthesize(text)
    tmp = Path("/tmp/wozai_clip.wav")
    tmp.write_bytes(audio_bytes)

    with wave.open(str(tmp)) as handle:
        frames = handle.getnframes()
        rate = handle.getframerate()
        channels = handle.getnchannels()
        width = handle.getsampwidth()
        raw = handle.readframes(frames)

    if channels != 1 or width != 2:
        raise SystemExit(f"期望 16bit 单声道，实际 {width*8}bit/{channels}ch")
    if rate != 16000:
        raise SystemExit(f"期望 16kHz，实际 {rate}Hz（固件 I2S 按 16k 播放）")
    return np.frombuffer(raw, dtype=np.int16).astype(np.float64), rate


def shape(samples: np.ndarray, rate: int) -> bytes:
    """裁首尾静音 + 电平对齐 + 补尾部留白，返回 s16le 裸 PCM。"""
    threshold = np.abs(samples).max() * 0.02
    voiced = np.where(np.abs(samples) > threshold)[0]
    if len(voiced) == 0:
        raise SystemExit("合成结果是静音，放弃")
    start = voiced[0]
    end = min(len(samples), voiced[-1] + 1)
    samples = samples[start:end]

    # 增益只按有声段算：首尾补的静音不能把 rms 拉低，否则整句听起来比中枢回复小。
    current_rms = np.sqrt((samples**2).mean())
    target_rms = 32768 * 10 ** (TARGET_RMS_DBFS / 20)
    samples = samples * (target_rms / max(current_rms, 1.0))
    peak = np.abs(samples).max()
    if peak > PEAK_CEILING * 32768:
        samples = samples * (PEAK_CEILING * 32768 / peak)

    samples = np.concatenate(
        [
            np.zeros(int(HEAD_SILENCE * rate)),
            samples,
            np.zeros(int(TAIL_SILENCE * rate)),
        ]
    )
    return np.clip(np.round(samples), -32768, 32767).astype(np.int16).tobytes()


def render_header(pcm: bytes, text: str, speed: float) -> str:
    rows = []
    for offset in range(0, len(pcm), 12):
        chunk = pcm[offset : offset + 12]
        rows.append("  " + ", ".join(f"0x{byte:02x}" for byte in chunk) + ",")
    body = "\n".join(rows).rstrip(",")
    return f"""// 「我在」唤醒应答（设备本地播放，不经过中枢，做到喊完立刻应声）
//
// 由 tools/make_wake_clip.py 生成，文本：「{text}」，speed={speed:.2f}
// 音色来自本地神经 TTS（vits-piper-zh_CN-xiao_ya-medium），与中枢回复同源，
// 避免「我在」和后面那句听起来不是同一个人在说话。
// 格式：16kHz / 单声道 / s16le 裸 PCM（无 wav 头，直接 i2s_write）
// 电平：rms {TARGET_RMS_DBFS}dBFS（有声段），与中枢回复对齐；首 {int(HEAD_SILENCE*1000)}ms / 尾 {int(TAIL_SILENCE*1000)}ms 静音
const unsigned char {SYMBOL}[] = {{
{body}
}};
const unsigned int {SYMBOL}_len = {len(pcm)};
"""


def main() -> int:
    parser = argparse.ArgumentParser(description="生成固件内置的「我在」音频")
    parser.add_argument("--text", default="我在")
    parser.add_argument("--model-dir", default=None)
    parser.add_argument(
        "--speed",
        type=float,
        default=DEFAULT_SPEED,
        help=f"语速，1.0 为模型原速，<1 更慢（默认 {DEFAULT_SPEED}）",
    )
    parser.add_argument("--dry-run", action="store_true", help="只写到 /tmp")
    args = parser.parse_args()

    speed = max(0.5, min(float(args.speed), 2.0))
    samples, rate = synth(args.text, args.model_dir, speed)
    pcm = shape(samples, rate)

    peak = int(np.abs(np.frombuffer(pcm, dtype=np.int16)).max())
    rms = 20 * np.log10(
        np.sqrt((np.frombuffer(pcm, dtype=np.int16).astype(np.float64) ** 2).mean())
        / 32768
    )
    print(
        f"时长 {len(pcm)//2/rate:.2f}s  峰值 {peak} ({20*np.log10(peak/32768):.1f}dBFS)  "
        f"rms {rms:.1f}dBFS  字节 {len(pcm)}"
    )

    header = render_header(pcm, args.text, speed)
    if args.dry_run:
        preview = Path("/tmp/wozai_audio_preview.h")
        preview.write_text(header)
        print(f"dry-run：已写到 {preview}")
        return 0

    HEADER_PATH.write_text(header)
    print(f"已写入 {HEADER_PATH}")
    print("下一步：pio run -e pet_voice -t upload --upload-port <port>")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
