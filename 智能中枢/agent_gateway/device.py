"""设备影子（device shadow）：中枢对桌宠硬件状态的唯一可信副本。

在此之前中枢对音箱一无所知 —— 用户问「现在音量多少」，模型只能现编，
于是出现「不知道音量，但可以说调大调小」这种回答。这里把设备状态做成
中枢能直接读的一等对象：

* **中枢自己控制的量**（说话音量）由中枢写，是权威值；
* **设备自己知道的量**（音乐档位、屏幕、喷泉、音乐播放态）由设备下次
  请求时通过 ``device_state`` 上报，中枢只做记录，不臆造；
* bot 的提示词里注入一行实时状态，回答「多少」时不必再猜；
* 音量真正生效点是 ``PortableRequestService.get_audio``：下发前按档位
  缩放 PCM，改完下一句就听得出来，不依赖固件改动。

注意「说话音量」和「音乐音量」是两套：

* 说话音量 = TTS 回复的电平，中枢按档位缩放，5 档；
* 音乐音量 = 设备音乐播放器自己的 4 档（``kVolumeDbList``），只能由
  设备改，中枢只记录它上报的值。
"""

from __future__ import annotations

import array
import json
import os
import threading
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Dict, Optional

DEFAULT_STATE_FILE = Path(
    os.environ.get("PET_DEVICE_STATE_FILE") or (Path.home() / ".sparkbot" / "devices.json")
)

# 5 档。0.35 在 1 米外勉强听清，1.0 是 TTS 原始电平（不再放大，避免削波）。
VOLUME_GAINS = (0.35, 0.5, 0.65, 0.8, 1.0)
VOLUME_LABELS = ("很小", "偏小", "正常", "偏大", "最大")


def _default_step_from_env(default: int = 3) -> int:
    """出厂默认档位。``PET_DEFAULT_VOLUME=2``（用户视角 1-5）让新设备直接落 2 档。

    只在「影子还没这台设备」时才用到，已保存过的设备仍以 devices.json 为准。
    """
    raw = (os.environ.get("PET_DEFAULT_VOLUME") or "").strip()
    try:
        value = int(raw)
    except (TypeError, ValueError):
        return default
    if 1 <= value <= len(VOLUME_GAINS):
        return value - 1
    return default


DEFAULT_VOLUME_STEP = _default_step_from_env()

# 设备音乐播放器的 4 档，和固件 music_player.cpp 的 kVolumeDbList 对应
MUSIC_DB = (-32, -18, -10, -4)

_MUSIC_STATE_TEXT = {
    "idle": "没在放",
    "playing": "正在放",
    "paused": "暂停中",
}


def clamp_volume_step(value: Any, default: int = DEFAULT_VOLUME_STEP) -> int:
    """把各种写法收敛到 0..4。设备上报、bot slots、用户口语都可能乱。"""
    try:
        step = int(round(float(value)))
    except (TypeError, ValueError):
        return default
    return max(0, min(len(VOLUME_GAINS) - 1, step))


@dataclass
class DeviceProfile:
    """一台设备的已知状态。字段缺失表示「还不知道」，不要当成 0。"""

    device_id: str
    volume_step: int = DEFAULT_VOLUME_STEP
    # 设备自己上报的，中枢只记录；None 表示设备还没上报过
    music_step: Optional[int] = None
    screen: str = "on"
    fountain: bool = False
    music: str = "idle"
    battery: Optional[int] = None
    updated_at: float = 0.0
    extra: Dict[str, Any] = field(default_factory=dict)

    # ---------------- 派生值 ----------------

    @property
    def volume_gain(self) -> float:
        return VOLUME_GAINS[clamp_volume_step(self.volume_step)]

    @property
    def volume_label(self) -> str:
        return VOLUME_LABELS[clamp_volume_step(self.volume_step)]

    def volume_sentence(self) -> str:
        """能直接朗读的一句，用于回答「现在音量多少」。"""
        step = clamp_volume_step(self.volume_step)
        return f"说话音量是 {step + 1} 档，一共 {len(VOLUME_GAINS)} 档，{VOLUME_LABELS[step]}。"

    def music_sentence(self) -> str:
        if self.music_step is None:
            return ""
        step = clamp_volume_step(self.music_step, 0) % len(MUSIC_DB)
        return f"音乐音量 {step + 1} 档，{MUSIC_DB[step]} 分贝。"

    def summary(self) -> str:
        """给 bot 提示词用的一行实时状态：短、全是事实。"""
        parts = [f"说话音量 {clamp_volume_step(self.volume_step) + 1}/{len(VOLUME_GAINS)}"]
        if self.music_step is not None:
            parts.append(f"音乐音量 {clamp_volume_step(self.music_step, 0) + 1}/{len(MUSIC_DB)}")
        parts.append(f"音乐{_MUSIC_STATE_TEXT.get(self.music, self.music or '未上报')}")
        parts.append("屏幕" + ("亮" if self.screen == "on" else "灭"))
        parts.append("喷泉" + ("开" if self.fountain else "关"))
        if self.battery is not None:
            parts.append(f"电量 {self.battery}%")
        return "；".join(parts)

    def public(self) -> Dict[str, Any]:
        payload = asdict(self)
        payload["volume_gain"] = round(self.volume_gain, 2)
        payload["volume_label"] = self.volume_label
        payload["summary"] = self.summary()
        return payload


class DeviceRegistry:
    """按 device_id 保存设备状态，落盘到 ``~/.sparkbot/devices.json``。

    单机场景下其实只有一个设备，但仍然按 id 分开存：将来同一账号挂第二台
    桌宠时不用改结构。写盘失败只告警，不影响对话。
    """

    def __init__(self, path: Optional[Path] = None) -> None:
        self._path = Path(path) if path is not None else DEFAULT_STATE_FILE
        self._lock = threading.RLock()
        self._devices: Dict[str, DeviceProfile] = {}
        self._load()

    # ---------------- 读写 ----------------

    def get(self, device_id: str) -> DeviceProfile:
        key = self._key(device_id)
        with self._lock:
            profile = self._devices.get(key)
            if profile is None:
                profile = DeviceProfile(device_id=key)
                self._devices[key] = profile
            return profile

    def all(self) -> Dict[str, DeviceProfile]:
        with self._lock:
            return dict(self._devices)

    def update_from_state(self, device_id: str, state: Any) -> DeviceProfile:
        """吸收设备上报的状态。字段名宽容，缺什么就保留旧值。

        刻意**不**接受上报里的 ``volume_step``：说话音量以中枢为准，否则
        中枢刚调完、设备一条旧状态回来就把它覆盖了。
        """
        profile = self.get(device_id)
        if not isinstance(state, dict):
            return profile
        with self._lock:
            music_step = _first(state, "music_step", "music_volume_step", "musicVolumeStep")
            if music_step is not None:
                try:
                    profile.music_step = max(0, min(len(MUSIC_DB) - 1, int(music_step)))
                except (TypeError, ValueError):
                    pass
            screen = _first(state, "screen", "screen_state", "display")
            if isinstance(screen, str) and screen:
                profile.screen = "on" if screen.lower() in {"on", "awake", "bright"} else "off"
            fountain = _first(state, "fountain", "fountain_active")
            if isinstance(fountain, bool):
                profile.fountain = fountain
            music = _first(state, "music", "music_state")
            if isinstance(music, str) and music:
                profile.music = music.lower()
            battery = _first(state, "battery", "battery_percent")
            if battery is not None:
                try:
                    profile.battery = max(0, min(100, int(battery)))
                except (TypeError, ValueError):
                    pass
            profile.updated_at = _now()
            self._save_locked()
        return profile

    def set_volume(self, device_id: str, step: Any) -> DeviceProfile:
        profile = self.get(device_id)
        with self._lock:
            profile.volume_step = clamp_volume_step(step)
            profile.updated_at = _now()
            self._save_locked()
        return profile

    def nudge_volume(self, device_id: str, delta: int) -> DeviceProfile:
        profile = self.get(device_id)
        with self._lock:
            profile.volume_step = clamp_volume_step(profile.volume_step + int(delta))
            profile.updated_at = _now()
            self._save_locked()
        return profile

    # ---------------- 音频增益 ----------------

    def volume_gain(self, device_id: str) -> float:
        return self.get(device_id).volume_gain

    def apply_volume(self, device_id: str, wav: bytes) -> bytes:
        """按说话音量缩放 WAV 里的 PCM。档位为最大时原样返回。"""
        profile = self.get(device_id)
        gain = profile.volume_gain
        if gain >= 0.999:
            return wav
        scaled = scale_wav_pcm(wav, gain)
        step = clamp_volume_step(profile.volume_step)
        try:
            print(
                f"[AUDIO] 说话音量 {step + 1}/{len(VOLUME_GAINS)} 档 gain={gain:.2f} "
                f"peak {wav_peak(wav)}->{wav_peak(scaled)} bytes={len(scaled)}",
                flush=True,
            )
        except Exception:  # pragma: no cover - 日志失败不影响下发
            pass
        return scaled

    # ---------------- 描述 ----------------

    def summary(self, device_id: str) -> str:
        return self.get(device_id).summary()

    def describe(self, device_id: str) -> str:
        profile = self.get(device_id)
        lines = [
            f"说话音量：{clamp_volume_step(profile.volume_step) + 1}/{len(VOLUME_GAINS)}"
            f"（{profile.volume_label}）",
        ]
        if profile.music_step is not None:
            lines.append(
                f"音乐音量：{clamp_volume_step(profile.music_step, 0) + 1}/{len(MUSIC_DB)}"
                f"（{MUSIC_DB[clamp_volume_step(profile.music_step, 0)]} dB）"
            )
        else:
            lines.append("音乐音量：设备还没上报")
        lines.append(f"音乐：{_MUSIC_STATE_TEXT.get(profile.music, profile.music)}")
        lines.append(f"屏幕：{'亮' if profile.screen == 'on' else '灭'}")
        lines.append(f"喷泉：{'开' if profile.fountain else '关'}")
        if profile.battery is not None:
            lines.append(f"电量：{profile.battery}%")
        return "\n".join(lines)

    # ---------------- 磁盘 ----------------

    @staticmethod
    def _key(device_id: str) -> str:
        key = str(device_id or "").strip()
        return key or "default"

    def _load(self) -> None:
        try:
            raw = json.loads(self._path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return
        if not isinstance(raw, dict):
            return
        for key, item in raw.items():
            if not isinstance(item, dict):
                continue
            try:
                self._devices[key] = DeviceProfile(
                    device_id=item.get("device_id") or key,
                    volume_step=clamp_volume_step(item.get("volume_step")),
                    music_step=(
                        None if item.get("music_step") is None else int(item["music_step"])
                    ),
                    screen=str(item.get("screen") or "on"),
                    fountain=bool(item.get("fountain")),
                    music=str(item.get("music") or "idle"),
                    battery=(
                        None if item.get("battery") is None else int(item["battery"])
                    ),
                    updated_at=float(item.get("updated_at") or 0.0),
                )
            except (TypeError, ValueError):
                continue

    def _save_locked(self) -> None:
        try:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            payload = {key: asdict(profile) for key, profile in self._devices.items()}
            tmp = self._path.with_suffix(".tmp")
            tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
            tmp.replace(self._path)
        except OSError:
            # 状态存不下也不该让对话失败，内存里的值仍然是对的
            pass


# ---------------- 工具函数 ----------------


def _first(state: Dict[str, Any], *names: str) -> Any:
    for name in names:
        if name in state and state[name] is not None:
            return state[name]
    return None


def _now() -> float:
    import time

    return time.time()


def wav_peak(data: bytes) -> int:
    """取 WAV 里 data chunk 的绝对峰值，用于验证增益是否真的生效。"""
    if not data or len(data) < 44 or data[:4] != b"RIFF":
        return 0
    pos = 12
    total = len(data)
    while pos + 8 <= total:
        chunk_id = data[pos : pos + 4]
        size = int.from_bytes(data[pos + 4 : pos + 8], "little")
        body = pos + 8
        if chunk_id == b"data":
            pcm = data[body : min(body + size, total)]
            pcm = pcm[: len(pcm) - (len(pcm) % 2)]
            if not pcm:
                return 0
            samples = array.array("h")
            samples.frombytes(pcm)
            return max((abs(s) for s in samples), default=0)
        pos = body + size + (size & 1)
    return 0


def scale_wav_pcm(data: bytes, gain: float) -> bytes:
    """把 WAV 里 data chunk 的 16bit PCM 乘上 gain，其余字节原样保留。

    只处理 PCM（``fmt `` 后两位为 1）；浮点或压缩格式一律原样返回，
    宁可音量不生效，也不要把音频弄坏。
    """
    if not data or len(data) < 44 or data[:4] != b"RIFF" or data[8:12] != b"WAVE":
        return data
    if gain >= 0.999:
        return data
    fmt_ok = True
    pos = 12
    total = len(data)
    while pos + 8 <= total:
        chunk_id = data[pos : pos + 4]
        size = int.from_bytes(data[pos + 4 : pos + 8], "little")
        body = pos + 8
        if chunk_id == b"fmt " and size >= 2:
            fmt_ok = int.from_bytes(data[body : body + 2], "little") == 1
        elif chunk_id == b"data":
            if not fmt_ok:
                return data
            end = min(body + size, total)
            pcm = data[body:end]
            if len(pcm) % 2:
                pcm = pcm[: len(pcm) - 1]
            if not pcm:
                return data
            samples = array.array("h")
            samples.frombytes(pcm)
            scaled = array.array("h", (max(-32768, min(32767, int(s * gain))) for s in samples))
            out = bytearray(data)
            out[body : body + len(pcm)] = scaled.tobytes()
            return bytes(out)
        pos = body + size + (size & 1)
    return data
