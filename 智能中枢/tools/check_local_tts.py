"""试听 / 测速本地神经 TTS，用来挑音色和确认延迟。

    python3 tools/check_local_tts.py              # 列说话人 + 用默认 sid 试听
    python3 tools/check_local_tts.py --all        # 每个说话人都合成一条
    python3 tools/check_local_tts.py --sid 48     # 指定说话人
    python3 tools/check_local_tts.py --text "你好"

输出落在 /tmp/local-tts-preview/，文件名带 sid 和耗时，方便 A/B。
"""

from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from agent_gateway.local_neural_tts import (  # noqa: E402
    LocalNeuralSpeechProvider,
    find_model_dir,
)

PREVIEW_DIR = Path("/tmp/local-tts-preview")
DEFAULT_TEXT = "好的，音乐喷泉已启动，跟着节奏一起动起来吧。"


def main() -> int:
    parser = argparse.ArgumentParser(description="试听本地神经 TTS")
    parser.add_argument("--sid", type=int, default=None, help="说话人编号")
    parser.add_argument("--all", action="store_true", help="遍历所有说话人")
    parser.add_argument("--text", default=DEFAULT_TEXT)
    parser.add_argument("--model-dir", default=None)
    parser.add_argument("--speed", type=float, default=1.0)
    args = parser.parse_args()

    model_dir = find_model_dir(args.model_dir)
    if model_dir is None:
        print("没找到模型。先下载：")
        print("  HF_ENDPOINT=https://hf-mirror.com python3 -c \"from huggingface_hub import snapshot_download;"
              "snapshot_download('csukuangfj/kokoro-multi-lang-v1_1',"
              "local_dir='~/.cache/sherpa-tts/kokoro-multi-lang-v1_1')\"")
        return 1
    print(f"模型：{model_dir}")

    provider = LocalNeuralSpeechProvider(model_dir=str(model_dir), speed=args.speed)
    if not provider.available:
        print(f"加载失败：{provider.error}")
        return 1

    num_speakers = int(getattr(provider._tts, "num_speakers", 0) or 0)  # noqa: SLF001
    print(f"说话人数量：{num_speakers}")

    if args.all:
        sids = list(range(num_speakers))
    elif args.sid is not None:
        sids = [args.sid]
    else:
        sids = [provider.sid]

    PREVIEW_DIR.mkdir(parents=True, exist_ok=True)
    results = []
    for sid in sids:
        provider.sid = sid
        started = time.perf_counter()
        try:
            audio, _fmt, _mime = provider.synthesize(args.text)
        except Exception as error:  # noqa: BLE001 - 单个 sid 失败不该中断全量试听
            print(f"  sid={sid:3d} 失败：{error}")
            continue
        elapsed = (time.perf_counter() - started) * 1000
        path = PREVIEW_DIR / f"sid{sid:03d}-{elapsed:.0f}ms.wav"
        path.write_bytes(audio)
        results.append((sid, elapsed, len(audio), path))
        print(f"  sid={sid:3d}  {elapsed:6.0f}ms  {len(audio):7d} bytes  {path}")

    if results:
        fastest = min(results, key=lambda item: item[1])
        average = sum(item[1] for item in results) / len(results)
        print(f"\n平均 {average:.0f}ms，最快 sid={fastest[0]}（{fastest[1]:.0f}ms）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
