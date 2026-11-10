"""批量生成桌宠的随机招呼语 / 结束语，打进固件头文件。

唤醒应答不再永远是「我在」，熄屏前也不再说「嗯」都没有 —— 各备 3 条，
设备端用 esp_random() 随机抽取（2026-11-10 用户要求「多样」）。

音色/电平规范与 make_wake_clip.py 完全一致（同一个本地神经 TTS、
rms -20.4dBFS 对齐中枢回复、首 60ms 尾 200ms 留白）。

    python3 tools/make_pet_phrases.py            # 生成 include/wake_sleep_phrases.h
    python3 tools/make_pet_phrases.py --dry-run  # 只打印统计不写文件

产出 wake_sleep_phrases.h 后重新烧录 pet_voice 生效。
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from make_wake_clip import shape, synth  # noqa: E402  复用同一套合成与电平整形

HEADER_PATH = ROOT / "firmware/sparkbot_wearable/include/wake_sleep_phrases.h"

# 招呼语（唤醒应答）与结束语（熄屏前），语速沿用「我在」的 0.78（不抢答）
WAKE_PHRASES = ["我在", "我在呢", "来了来了"]
SLEEP_PHRASES = ["那我先睡了", "我去睡了", "那我歇着了"]
SPEED = 0.78


def symbol(index: int, kind: str) -> str:
    return f"_pet_{kind}_{index}_pcm"


def render_clip(index: int, kind: str, pcm: bytes, text: str) -> str:
    sym = symbol(index, kind)
    rows = []
    for offset in range(0, len(pcm), 12):
        chunk = pcm[offset : offset + 12]
        rows.append("  " + ", ".join(f"0x{byte:02x}" for byte in chunk) + ",")
    body = "\n".join(rows).rstrip(",")
    return f"// 「{text}」\nconst unsigned char {sym}[] = {{\n{body}\n}};\nconst unsigned int {sym}_len = {len(pcm)};\n"


def main() -> int:
    parser = argparse.ArgumentParser(description="批量生成招呼语/结束语")
    parser.add_argument("--model-dir", default=None)
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    sections: list[str] = []
    stats: list[str] = []
    tables: dict[str, list[str]] = {"wake": [], "sleep": []}
    counts: dict[str, int] = {}

    for kind, phrases in (("wake", WAKE_PHRASES), ("sleep", SLEEP_PHRASES)):
        clips = []
        for index, text in enumerate(phrases):
            samples, rate = synth(text, args.model_dir, SPEED)
            pcm = shape(samples, rate)
            seconds = len(pcm) // 2 / rate
            stats.append(f"  [{kind}#{index}] 「{text}」 {seconds:.2f}s {len(pcm)}B")
            sections.append(render_clip(index, kind, pcm, text))
            clips.append(
                f"  {{{symbol(index, kind)}, {symbol(index, kind)}_len}},"
            )
        tables[kind] = clips
        counts[kind] = len(phrases)

    header = (
        "// 桌宠随机招呼语 / 结束语（由 tools/make_pet_phrases.py 生成，勿手改）\n"
        "// 音色 vits-piper-zh_CN-xiao_ya-medium，与中枢回复同源；16kHz/单声道/s16le 裸 PCM\n"
        "#pragma once\n\n"
        + "\n".join(sections)
        + f"""
struct PetPhraseClip {{
  const unsigned char* data;
  unsigned int len;
}};

#define PET_WAKE_PHRASE_COUNT {counts['wake']}
#define PET_SLEEP_PHRASE_COUNT {counts['sleep']}

const PetPhraseClip PET_WAKE_PHRASES[] = {{
{chr(10).join(tables['wake'])}
}};

const PetPhraseClip PET_SLEEP_PHRASES[] = {{
{chr(10).join(tables['sleep'])}
}};
"""
    )

    for line in stats:
        print(line)
    total = sum(int(s.rsplit(" ", 1)[1][:-1]) for s in stats)
    print(f"合计 {total/1024:.0f}KB")

    if args.dry_run:
        Path("/tmp/wake_sleep_phrases_preview.h").write_text(header)
        print("dry-run：已写到 /tmp/wake_sleep_phrases_preview.h")
        return 0
    HEADER_PATH.write_text(header)
    print(f"已写入 {HEADER_PATH}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
