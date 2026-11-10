"""AMD Radeon Cloud 的 OmniVoice 云端语音合成。

平台是 developer.amd.com.cn 的 Radeon AI 模型网关（OpenAI 风格的 `/v1` 接口），
OmniVoice 是其中唯一一个输出 audio 的模型。接口形态跟 OpenAI 的
`/audio/speech` 基本一致，所以这里当成「一家云端 TTS 厂商」接进来，
实现与 ``MacSaySpeechProvider`` / ``RK3588SpeechProvider`` 相同的协议：

    synthesize(text) -> (audio_bytes, "wav", "audio/wav")

和另外两家不一样的地方只有两处，都已在本模块内消化：

1. 云端只认自己的音色名（female / male / female-british …），传系统语音名
   （Tingting、zh-CN-XiaoxiaoNeural）会直接 400，所以这里做白名单校验；
2. 云端的 WAV 是 24 kHz 单声道，而录音豆/桌宠的播放链路要 16 kHz，
   所以拿到音频后统一走 ``normalize_wav`` 重采样 + 重封装。

密钥走环境变量，不写进代码：``AMD_RADEON_API_KEY``（也认 ``OMNIVOICE_API_KEY``）。
"""

from __future__ import annotations

import json
import os
import socket
import threading
import time
import urllib.error
import urllib.request
from typing import Any, Dict, Iterable, Optional, Tuple

from agent_gateway.local_tts import normalize_wav

DEFAULT_BASE_URL = "https://developer.amd.com.cn/radeon/api/v1"
DEFAULT_MODEL = "OmniVoice"
DEFAULT_VOICE = "female"
# 实测单条 1.3~4.8 秒，8 秒足够；卡住时宁可早点放弃出声，也别让设备干等 15 秒。
DEFAULT_TIMEOUT_SECONDS = 8.0

# 平台是串行排队的：3 路并发请求实测全部变成 26 秒。所以请求必须排队发，
# 一个进程内只允许一条在途，否则设备连说两句话就会互相拖死。
SERIALIZE_REQUESTS = True

# 缓存上限。设备交互的话术高度重复（我在/好的/已关闭…），命中等价于 0 延迟。
CACHE_MAX_ENTRIES = 32

# 平台实际接受的音色（unknown 名字会被 400 拒绝）。auto 交给服务端自选，
# 但中文场景下 auto 偶尔会挑到英文口音，所以默认写死 female。
SUPPORTED_VOICES = (
    "female",
    "male",
    "female-low",
    "male-low",
    "female-british",
    "male-british",
    "female-american",
    "male-american",
    "auto",
)

# 云端 1.4~1.7 秒出一条 20 字左右的回复，5xx / 连接中断重试一次就够；
# 再多等不如直接退回设备自带的提示音。
RETRYABLE_STATUS = {408, 429, 500, 502, 503, 504}


class OmniVoiceError(RuntimeError):
    pass


class OmniVoiceSpeechProvider:
    """OmniVoice 的 SpeechSynthesisProvider 实现。"""

    def __init__(
        self,
        api_key: Optional[str] = None,
        base_url: Optional[str] = None,
        model: Optional[str] = None,
        voice: Optional[str] = None,
        speed: Optional[Any] = None,
        timeout_seconds: Optional[float] = None,
        max_retries: int = 1,
    ) -> None:
        self.api_key = _first_env(api_key, ("AMD_RADEON_API_KEY", "OMNIVOICE_API_KEY"))
        self.base_url = (
            base_url
            or os.environ.get("AMD_RADEON_BASE_URL")
            or DEFAULT_BASE_URL
        ).rstrip("/")
        self.model = model or os.environ.get("OMNIVOICE_MODEL") or DEFAULT_MODEL
        self.voice = normalize_voice(voice or os.environ.get("OMNIVOICE_VOICE"))
        self.speed = normalize_speed(
            speed if speed is not None else os.environ.get("OMNIVOICE_SPEED")
        )
        if timeout_seconds is None:
            timeout_seconds = _float_env("OMNIVOICE_TIMEOUT", DEFAULT_TIMEOUT_SECONDS)
        self.timeout_seconds = max(1.0, float(timeout_seconds))
        self.max_retries = max(0, int(max_retries))
        self._cache: Dict[str, Tuple[bytes, str, str]] = {}
        self._cache_lock = threading.Lock()
        # 只在真的需要时才让请求排队：缓存命中不进锁，所以预热过的话术不互相阻塞。
        self._request_lock = threading.Lock() if SERIALIZE_REQUESTS else None

    @property
    def available(self) -> bool:
        return bool(self.api_key)

    @property
    def endpoint(self) -> str:
        return f"{self.base_url}/audio/speech"

    def synthesize(self, text: str) -> Tuple[bytes, str, str]:
        normalized = str(text or "").strip()
        if not normalized:
            raise OmniVoiceError("没有可合成的文字")
        if not self.api_key:
            raise OmniVoiceError("缺少 AMD_RADEON_API_KEY，无法调用 OmniVoice")

        cached = self._cache_get(normalized)
        if cached is not None:
            print(f"[TIMING] omnivoice=0ms (cached) chars={len(normalized)}", flush=True)
            return cached

        if self._request_lock is not None:
            with self._request_lock:
                # 排队等锁期间可能已经被别的线程合成了，进锁后再查一次
                cached = self._cache_get(normalized)
                if cached is not None:
                    return cached
                result = self._synthesize_uncached(normalized)
        else:
            result = self._synthesize_uncached(normalized)

        self._cache_put(normalized, result)
        return result

    def warmup(self, texts: Iterable[str]) -> int:
        """串行预热常用话术，返回成功条数。

        唤醒应答（「我在」）是最高频的一句话，云端首次合成要 1.5~2 秒；
        提前合成进缓存后，设备喊醒就能立刻出声。失败一律吞掉 ——
        预热只是优化，绝不能拖累启动。
        """
        ok = 0
        for text in texts:
            normalized = str(text or "").strip()
            if not normalized or self._cache_get(normalized) is not None:
                continue
            try:
                self._cache_put(normalized, self._synthesize_uncached(normalized))
                ok += 1
            except Exception as error:  # noqa: BLE001 - 预热失败不影响主链路
                print(f"[WARN] OmniVoice 预热失败：{error}", flush=True)
        return ok

    def _synthesize_uncached(self, normalized: str) -> Tuple[bytes, str, str]:
        started = time.perf_counter()
        audio = self._request_audio(normalized)
        # 24 kHz -> 16 kHz、去 JUNK chunk、单声道重封装，交给设备前必须做
        device_audio = normalize_wav(audio)
        print(
            f"[TIMING] omnivoice={(time.perf_counter() - started) * 1000:.0f}ms "
            f"chars={len(normalized)} bytes={len(device_audio)}",
            flush=True,
        )
        if not device_audio:
            raise OmniVoiceError("OmniVoice 返回了空音频")
        return device_audio, "wav", "audio/wav"

    def _cache_get(self, key: str) -> Optional[Tuple[bytes, str, str]]:
        with self._cache_lock:
            return self._cache.get(key)

    def _cache_put(self, key: str, value: Tuple[bytes, str, str]) -> None:
        with self._cache_lock:
            if len(self._cache) >= CACHE_MAX_ENTRIES and key not in self._cache:
                self._cache.pop(next(iter(self._cache)), None)
            self._cache[key] = value

    # ---------------- 内部 ----------------

    def _payload(self, text: str) -> Dict[str, Any]:
        payload: Dict[str, Any] = {
            "model": self.model,
            "input": text,
            "voice": self.voice,
            # 直接要 wav：省掉一次 mp3 解码，且 normalize_wav 只处理 PCM WAV
            "response_format": "wav",
        }
        if self.speed is not None:
            payload["speed"] = self.speed
        return payload

    def _request_audio(self, text: str) -> bytes:
        body = json.dumps(self._payload(text), ensure_ascii=False).encode("utf-8")
        request = urllib.request.Request(
            self.endpoint,
            data=body,
            method="POST",
            headers={
                "Authorization": f"Bearer {self.api_key}",
                "Content-Type": "application/json",
                "Accept": "audio/wav,audio/mpeg,application/octet-stream",
            },
        )

        last_error: Optional[Exception] = None
        for attempt in range(self.max_retries + 1):
            try:
                with urllib.request.urlopen(request, timeout=self.timeout_seconds) as resp:
                    audio = resp.read()
                    content_type = (resp.headers.get("Content-Type") or "").lower()
                if content_type.startswith("application/json"):
                    # 200 也可能带 JSON 错误体，别把它当音频写进播放缓冲
                    raise OmniVoiceError(_json_error(audio) or "OmniVoice 返回了 JSON 而不是音频")
                if not audio:
                    raise OmniVoiceError("OmniVoice 返回了空音频")
                return audio
            except urllib.error.HTTPError as error:
                detail = _json_error(error.read()) or f"HTTP {error.code}"
                last_error = OmniVoiceError(f"OmniVoice 合成失败：{detail}")
                if error.code not in RETRYABLE_STATUS or attempt >= self.max_retries:
                    raise last_error from error
            except (urllib.error.URLError, socket.timeout, TimeoutError, ConnectionError) as error:
                last_error = OmniVoiceError(f"OmniVoice 连接失败：{error}")
                if attempt >= self.max_retries:
                    raise last_error from error
            if attempt < self.max_retries:
                time.sleep(0.3)

        raise last_error or OmniVoiceError("OmniVoice 合成失败")


class FallbackSpeechProvider:
    """云端失败时退回本地合成。

    宁可音色变一下，也别让设备喊了没反应 —— 「喊了不理」是最差的体验，
    项目里本来就有本机 say 兜底，别让它白放着。回退只在云端真的抛错时发生
    （超时、5xx、网络断），正常情况音色始终是 OmniVoice。
    """

    def __init__(self, primary: Any, fallback: Optional[Any] = None) -> None:
        self.primary = primary
        self.fallback = fallback
        self.fallback_count = 0

    def synthesize(self, text: str) -> Tuple[bytes, str, str]:
        try:
            return self.primary.synthesize(text)
        except Exception as error:  # noqa: BLE001 - 云端任何失败都要兜住
            if self.fallback is None:
                raise
            self.fallback_count += 1
            print(f"[WARN] 云端语音合成失败，回退本地：{error}", flush=True)
            return self.fallback.synthesize(text)


def normalize_voice(value: Optional[str]) -> str:
    """把外部传入的音色收敛到平台白名单，不合法就退回 female。

    调用方常把本地语音名（Tingting）或 OpenAI 音色名（alloy）一路传下来，
    直接透传会让云端 400，所以这里必须兜一层。
    """
    candidate = str(value or "").strip().lower()
    if candidate in SUPPORTED_VOICES:
        return candidate
    return DEFAULT_VOICE


def normalize_speed(value: Any) -> Optional[float]:
    """语速做范围裁剪；无法解析就返回 None（不带这个字段，用服务端默认）。"""
    if value is None or value == "":
        return None
    try:
        speed = float(value)
    except (TypeError, ValueError):
        return None
    return max(0.5, min(speed, 2.0))


def _first_env(explicit: Optional[str], names: Tuple[str, ...]) -> str:
    if explicit:
        return str(explicit).strip()
    for name in names:
        value = os.environ.get(name)
        if value and value.strip():
            return value.strip()
    return ""


def _float_env(name: str, default: float) -> float:
    raw = os.environ.get(name)
    if not raw:
        return default
    try:
        return float(raw)
    except ValueError:
        return default


def _json_error(raw: bytes) -> str:
    """从错误体里抠出可读信息，抠不出来就原样截断。"""
    if not raw:
        return ""
    text = raw.decode("utf-8", "replace").strip()
    try:
        data = json.loads(text)
    except (json.JSONDecodeError, ValueError):
        return text[:300]
    if isinstance(data, dict):
        error = data.get("error")
        if isinstance(error, dict):
            message = error.get("message") or error.get("code")
            if message:
                return str(message)[:300]
        if isinstance(error, str) and error:
            return error[:300]
        message = data.get("message")
        if message:
            return str(message)[:300]
    return text[:300]
