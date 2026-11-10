"""本地神经 TTS：sherpa-onnx + Kokoro/Piper 中文模型，CPU 实时合成。

和云端 OmniVoice 的区别：

- 不联网、不排队，M4 上一条 20 字回复通常 200~400 ms 出音；
- 音色是神经网络生成的（VITS/Kokoro），不是 macOS `say` 那种拼接录音；
- 输出仍然统一成设备要的 16 kHz / 16 bit / 单声道 WAV。

模型不放仓库里（单个 60~150 MB），默认从 ``~/.cache/sherpa-tts/`` 找，
可以用 ``LOCAL_TTS_MODEL_DIR`` 指到别处。找不到模型时 ``available`` 为 False，
由调用方决定退回云端还是本机 say。
"""

from __future__ import annotations

import io
import os
import threading
import time
import wave
from pathlib import Path
from typing import Optional, Tuple

from agent_gateway.local_tts import normalize_wav

DEFAULT_MODEL_ROOT = Path("~/.cache/sherpa-tts")
# 按优先级找：Kokoro 多语言自然度最好，其次是两个轻量中文 VITS。
MODEL_CANDIDATES = (
    "kokoro-int8-multi-lang-v1_1",
    "kokoro-multi-lang-v1_1",
    "vits-piper-zh_CN-xiao_ya-medium",
    "vits-piper-zh_CN-huayan-medium",
)

# Kokoro 多语言模型里中文女声的说话人编号。取值在模型自带的 voices.bin 里，
# 实际编号靠 tools/check_local_tts.py 试听后锁定；其余模型忽略 sid。
DEFAULT_SID = 3


class LocalNeuralTtsError(RuntimeError):
    pass


def find_model_dir(explicit: Optional[str] = None) -> Optional[Path]:
    """定位模型目录：显式指定 > 环境变量 > 缓存目录里第一个存在的候选。"""
    if explicit:
        path = Path(explicit).expanduser()
        return path if path.is_dir() else None

    from_env = os.environ.get("LOCAL_TTS_MODEL_DIR")
    if from_env:
        path = Path(from_env).expanduser()
        return path if path.is_dir() else None

    root = Path(os.environ.get("LOCAL_TTS_MODEL_ROOT", DEFAULT_MODEL_ROOT)).expanduser()
    for name in MODEL_CANDIDATES:
        candidate = root / name
        # 目录存在不等于能用：下载中断会留下没有 onnx 的空壳目录，
        # 光看 is_dir() 会选中它，把后面真正下好的模型挡掉。
        if candidate.is_dir() and any(candidate.glob("*.onnx")):
            return candidate
    return None


class LocalNeuralSpeechProvider:
    """SpeechSynthesisProvider 的本地神经实现（接口与云端那套一致）。"""

    def __init__(
        self,
        model_dir: Optional[str] = None,
        sid: int = DEFAULT_SID,
        speed: float = 1.0,
        num_threads: int = 2,
    ) -> None:
        self.model_dir = find_model_dir(model_dir)
        self.sid = int(sid)
        self.speed = max(0.5, min(float(speed), 2.0))
        self.num_threads = max(1, int(num_threads))
        self._tts = None
        self._load_error: Optional[str] = None
        # sherpa-onnx 的 OfflineTts 实例不保证可重入，合成必须排他。
        self._lock = threading.Lock()

    @property
    def available(self) -> bool:
        if self.model_dir is None:
            return False
        try:
            self._ensure_loaded()
        except Exception as error:  # noqa: BLE001 - 不可用就走降级，不炸调用方
            self._load_error = str(error)
            return False
        return self._tts is not None

    @property
    def error(self) -> Optional[str]:
        return self._load_error

    def synthesize(self, text: str) -> Tuple[bytes, str, str]:
        normalized = str(text or "").strip()
        if not normalized:
            raise LocalNeuralTtsError("没有可合成的文字")
        if not self.available:
            detail = self._load_error or f"没找到模型（{self.model_dir}）"
            raise LocalNeuralTtsError(f"本地神经 TTS 不可用：{detail}")

        started = time.perf_counter()
        with self._lock:
            self.sid = self._resolve_sid()
            audio = self._tts.generate(normalized, sid=self.sid, speed=self.speed)
        samples = getattr(audio, "samples", None)
        sample_rate = int(getattr(audio, "sample_rate", 0) or 0)
        if samples is None or len(samples) == 0 or sample_rate <= 0:
            raise LocalNeuralTtsError("本地神经 TTS 没有生成音频")

        wav = _float_samples_to_wav(samples, sample_rate)
        # sample_rate 因模型而异（Kokoro 24 kHz、Piper 22.05 kHz），统一降到 16 kHz
        device_audio = normalize_wav(wav)
        elapsed = (time.perf_counter() - started) * 1000
        print(
            f"[TIMING] local_neural={elapsed:.0f}ms chars={len(normalized)} "
            f"bytes={len(device_audio)}",
            flush=True,
        )
        return device_audio, "wav", "audio/wav"

    # ---------------- 内部 ----------------

    def _ensure_loaded(self) -> None:
        if self._tts is not None:
            return
        import sherpa_onnx

        config = self._build_config(sherpa_onnx)
        self._tts = sherpa_onnx.OfflineTts(config)

    def _build_config(self, sherpa_onnx):
        assert self.model_dir is not None
        model_dir = self.model_dir

        # Kokoro 多语言：主模型 + voices + tokens + espeak 数据 + 中文分词词典
        kokoro_files = {
            "model": model_dir / "model.int8.onnx",
            "voices": model_dir / "voices.bin",
            "tokens": model_dir / "tokens.txt",
            "data_dir": model_dir / "espeak-ng-data",
            "dict_dir": model_dir / "dict",
            "lexicon": model_dir / "lexicon-us-en.txt",
        }
        if kokoro_files["model"].is_file() and kokoro_files["voices"].is_file():
            kokoro = sherpa_onnx.OfflineTtsKokoroModelConfig(
                model=str(kokoro_files["model"]),
                voices=str(kokoro_files["voices"]),
                tokens=str(kokoro_files["tokens"]),
                data_dir=str(kokoro_files["data_dir"]),
                dict_dir=str(kokoro_files["dict_dir"]),
                lexicon=str(kokoro_files["lexicon"]),
                length_scale=self.speed,
            )
            return sherpa_onnx.OfflineTtsConfig(
                model=sherpa_onnx.OfflineTtsModelConfig(
                    kokoro=kokoro,
                    num_threads=self.num_threads,
                    provider="cpu",
                ),
                max_num_sentences=1,
            )

        # 轻量 VITS（Piper 系列）：单个 onnx + tokens + 可选 lexicon / 数字正则化 fst
        vits_model = next(
            (
                candidate
                for candidate in (model_dir / "model.onnx", model_dir / "model.int8.onnx")
                if candidate.is_file()
            ),
            None,
        )
        if vits_model is None:
            # Piper 官方包按模型名命名 onnx（zh_CN-xiao_ya-medium.onnx），
            # 不是统一叫 model.onnx，退而取目录里体积最大的那个。
            candidates = sorted(
                model_dir.glob("*.onnx"), key=lambda p: p.stat().st_size, reverse=True
            )
            vits_model = candidates[0] if candidates else None
        if vits_model is None:
            raise LocalNeuralTtsError(f"{model_dir} 里没有可用的 onnx 模型")

        lexicon = model_dir / "lexicon.txt"
        vits = sherpa_onnx.OfflineTtsVitsModelConfig(
            model=str(vits_model),
            tokens=str(model_dir / "tokens.txt"),
            data_dir=str(model_dir / "espeak-ng-data"),
            lexicon=str(lexicon) if lexicon.is_file() else "",
            length_scale=1.0 / self.speed,
        )
        # date/number/phone.fst 负责把「18 度」读成「十八度」而不是「一八度」。
        rule_fsts = [
            path
            for path in (
                model_dir / "date.fst",
                model_dir / "number.fst",
                model_dir / "phone.fst",
            )
            if path.is_file()
        ]
        return sherpa_onnx.OfflineTtsConfig(
            model=sherpa_onnx.OfflineTtsModelConfig(
                vits=vits,
                num_threads=self.num_threads,
                provider="cpu",
            ),
            max_num_sentences=1,
            rule_fsts=",".join(str(path) for path in rule_fsts) if rule_fsts else "",
        )

    def _resolve_sid(self) -> int:
        """把说话人编号收敛到模型实际拥有的范围内。"""
        num_speakers = int(getattr(self._tts, "num_speakers", 0) or 0)
        if num_speakers <= 0:
            return 0
        return max(0, min(self.sid, num_speakers - 1))


def _float_samples_to_wav(samples, sample_rate: int) -> bytes:
    """把模型的 float32 波形打成 16 bit PCM WAV。"""
    import array

    clipped = array.array("h")
    for value in samples:
        scaled = int(max(-1.0, min(1.0, float(value))) * 32767)
        clipped.append(scaled)

    buffer = io.BytesIO()
    with wave.open(buffer, "wb") as target:
        target.setnchannels(1)
        target.setsampwidth(2)
        target.setframerate(sample_rate)
        target.writeframes(clipped.tobytes())
    return buffer.getvalue()
