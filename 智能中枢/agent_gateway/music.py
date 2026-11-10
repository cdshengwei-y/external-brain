from __future__ import annotations

"""桌宠点歌：本地音乐库 + B 站在线搜索，统一转成 16k 单声道 WAV 供设备拉流。

设计要点
--------
1. **本地优先**：~/SparkBotMusic 里能匹配到就直接用，不外网。
2. **在线兜底**：yt-dlp 走 ``bilisearch`` 搜同名音频，挑最像的一条下载。
3. **统一格式**：不管源是什么，一律 ffmpeg 转 16000Hz / 单声道 / pcm_s16le，
   设备端只认这一种格式（I2S 固定 16k，MCLK 4.096MHz）。
4. **落盘缓存**：转好的 WAV 存在 ~/.sparkbot/music_cache，第二次点同一首歌立刻可播，
   也让暂停/重播可以按字节 Range 续播。
"""

import hashlib
import json
import os
import re
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

TARGET_RATE = 16000
TARGET_CHANNELS = 1
TARGET_BYTES_PER_SECOND = TARGET_RATE * TARGET_CHANNELS * 2

AUDIO_SUFFIXES = {
    ".mp3",
    ".m4a",
    ".aac",
    ".flac",
    ".wav",
    ".ogg",
    ".opus",
    ".wma",
    ".aiff",
    ".aif",
}

DEFAULT_LIBRARY_DIR = Path.home() / "SparkBotMusic"
DEFAULT_CACHE_DIR = Path.home() / ".sparkbot" / "music_cache"

# 在线搜索 / 下载的预算（秒）。设备侧轮询上限 75s，这里要留出余量。
SEARCH_TIMEOUT_SECONDS = 30
DOWNLOAD_TIMEOUT_SECONDS = 60

NETEASE_BASE = "https://music.163.com"
BROWSER_USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
)


class MusicError(RuntimeError):
    pass


@dataclass
class Track:
    track_id: str
    title: str
    source: str  # "local" | "netease" | "online"
    origin: str  # 本地绝对路径 / netease:<id> / 在线页面地址
    duration: float = 0.0
    artist: str = ""
    cached_path: Optional[str] = None

    def public(self) -> Dict[str, Any]:
        return {
            "track_id": self.track_id,
            "title": self.title,
            "artist": self.artist,
            "source": self.source,
            "duration": round(self.duration, 1),
            "cached": bool(self.cached_path and Path(self.cached_path).is_file()),
        }

    def display_title(self) -> str:
        if self.artist:
            return f"{self.title} - {self.artist}"
        return self.title


@dataclass
class PlaybackSession:
    device_id: str
    playlist: List[Track] = field(default_factory=list)
    index: int = 0
    state: str = "playing"  # playing | paused | stopped
    updated_at: float = field(default_factory=time.time)

    def current(self) -> Optional[Track]:
        if 0 <= self.index < len(self.playlist):
            return self.playlist[self.index]
        return None


def _sha1(text: str) -> str:
    return hashlib.sha1(text.encode("utf-8")).hexdigest()


def _normalize(text: str) -> str:
    return "".join(str(text).lower().split())


def _tokens(text: str) -> List[str]:
    return [token for token in re.split(r"[\s\-_.,，。、《》()（）\[\]]+", text) if token]


class MusicService:
    """点歌服务：搜索、转码、缓存、流式输出、播放会话状态。"""

    def __init__(
        self,
        library_dir: Optional[Path] = None,
        cache_dir: Optional[Path] = None,
        allow_online: bool = True,
    ) -> None:
        self.library_dir = Path(library_dir or DEFAULT_LIBRARY_DIR).expanduser()
        self.cache_dir = Path(cache_dir or DEFAULT_CACHE_DIR).expanduser()
        self.allow_online = allow_online
        self._lock = threading.RLock()
        self._track_locks: Dict[str, threading.Lock] = {}
        self._sessions: Dict[str, PlaybackSession] = {}
        self._library: List[Track] = []
        self._library_scanned_at = 0.0
        self._meta_path = self.cache_dir / "index.json"
        self._meta: Dict[str, Dict[str, Any]] = self._load_meta()

        try:
            self.library_dir.mkdir(parents=True, exist_ok=True)
            self.cache_dir.mkdir(parents=True, exist_ok=True)
        except OSError:
            pass

    # ------------------------------------------------------------------ 元数据
    def _load_meta(self) -> Dict[str, Dict[str, Any]]:
        try:
            return json.loads(self._meta_path.read_text("utf-8"))
        except (OSError, json.JSONDecodeError):
            return {}

    def _save_meta(self) -> None:
        try:
            self._meta_path.write_text(
                json.dumps(self._meta, ensure_ascii=False, indent=1), "utf-8"
            )
        except OSError:
            pass

    # ------------------------------------------------------------------ 本地库
    def scan_library(self, force: bool = False) -> List[Track]:
        with self._lock:
            if not force and self._library and time.time() - self._library_scanned_at < 120:
                return list(self._library)
            tracks: List[Track] = []
            if self.library_dir.is_dir():
                for root, _dirs, files in os.walk(self.library_dir):
                    for name in files:
                        if Path(name).suffix.lower() not in AUDIO_SUFFIXES:
                            continue
                        path = Path(root) / name
                        track_id = "loc" + _sha1(str(path.resolve()))[:14]
                        tracks.append(
                            Track(
                                track_id=track_id,
                                title=path.stem.replace("_", " ").strip(),
                                source="local",
                                origin=str(path.resolve()),
                                cached_path=self._cached_wav(track_id)
                                if self._wav_exists(track_id)
                                else None,
                            )
                        )
            self._library = tracks
            self._library_scanned_at = time.time()
            return list(tracks)

    # ------------------------------------------------------------------ 缓存
    def _wav_path(self, track_id: str) -> Path:
        return self.cache_dir / f"{track_id}.wav"

    def _wav_exists(self, track_id: str) -> bool:
        path = self._wav_path(track_id)
        return path.is_file() and path.stat().st_size > 1024

    def _cached_wav(self, track_id: str) -> Optional[str]:
        return str(self._wav_path(track_id)) if self._wav_exists(track_id) else None

    def cached_track(self, track_id: str) -> Optional[Track]:
        """从缓存元数据还原一条已下载过的在线曲目。"""
        meta = self._meta.get(track_id)
        if not meta:
            return None
        return Track(
            track_id=track_id,
            title=str(meta.get("title") or "未命名"),
            source=str(meta.get("source") or "online"),
            origin=str(meta.get("origin") or ""),
            duration=float(meta.get("duration") or 0.0),
            cached_path=self._cached_wav(track_id),
        )

    # ------------------------------------------------------------------ 搜索
    def search(self, query: str, limit: int = 5) -> List[Track]:
        """本地优先，找不到再联网。返回按相关度排序的候选列表。"""
        query = (query or "").strip()
        local = self._search_local(query)
        if local:
            return local[:limit]
        if not self.allow_online:
            return []
        try:
            online = self._search_online(query, limit)
        except MusicError as error:
            print(f"[MUSIC] 在线搜索失败：{error}", flush=True)
            return []
        return online

    def _search_local(self, query: str) -> List[Track]:
        tracks = self.scan_library()
        if not query:
            return tracks[:5]
        needle = _normalize(query)
        scored: List[Tuple[float, Track]] = []
        for track in tracks:
            haystack = _normalize(track.title) + "|" + _normalize(track.origin)
            if needle and needle in haystack:
                scored.append((1.0 + len(needle) / max(len(haystack), 1), track))
                continue
            tokens = _tokens(query)
            if tokens and all(_normalize(token) in haystack for token in tokens):
                scored.append((0.8, track))
                continue
            hits = sum(1 for token in tokens if _normalize(token) and _normalize(token) in haystack)
            if hits and tokens:
                scored.append((0.5 * hits / len(tokens), track))
        scored.sort(key=lambda item: item[0], reverse=True)
        return [track for score, track in scored if score >= 0.5]

    def _python_module_command(self, module: str, *args: str) -> List[str]:
        return [sys.executable, "-m", module, *args]

    def _search_online(self, query: str, limit: int) -> List[Track]:
        """网易云优先（歌名/歌手/时长都规整），B 站兜底（播客、翻唱等）。"""
        tracks: List[Track] = []
        if query:
            try:
                tracks = self._search_netease(query, limit)
            except MusicError as error:
                print(f"[MUSIC] 网易云搜索失败：{error}", flush=True)
        if tracks:
            return tracks
        try:
            return self._search_bilibili(query, limit)
        except MusicError as error:
            if not tracks:
                raise
            return tracks

    def _search_netease(self, query: str, limit: int) -> List[Track]:
        params = urllib.parse.urlencode(
            {"s": query, "type": 1, "limit": limit, "offset": 0}
        )
        payload = self._http_json(f"{NETEASE_BASE}/api/search/get?{params}")
        result = payload.get("result") or {}
        songs = result.get("songs") or []
        tracks: List[Track] = []
        scores: List[float] = []
        for song in songs:
            if not isinstance(song, dict):
                continue
            song_id = song.get("id")
            name = str(song.get("name") or "").strip()
            if not song_id or not name:
                continue
            artists = "、".join(
                str(artist.get("name") or "")
                for artist in (song.get("artists") or [])
                if isinstance(artist, dict)
            )
            origin = f"netease:{song_id}"
            track_id = "ne" + _sha1(origin)[:14]
            duration = float(song.get("duration") or 0) / 1000.0
            tracks.append(
                Track(
                    track_id=track_id,
                    title=name,
                    artist=artists,
                    source="netease",
                    origin=origin,
                    duration=duration,
                    cached_path=self._cached_wav(track_id),
                )
            )
            scores.append(_relevance(query, name, artists, duration))
        ranked = [
            track
            for _, track in sorted(
                zip(scores, tracks), key=lambda item: item[0], reverse=True
            )
        ]
        return ranked[:limit]

    def _search_bilibili(self, query: str, limit: int) -> List[Track]:
        search_term = f"bilisearch{max(limit, 3)}:{query}"
        command = self._python_module_command(
            "yt_dlp",
            search_term,
            "--flat-playlist",
            "--dump-single-json",
            "--no-warnings",
            "--skip-download",
            "--user-agent",
            BROWSER_USER_AGENT,
        )
        payload = self._run_json(command, SEARCH_TIMEOUT_SECONDS)
        entries = payload.get("entries") or []
        tracks: List[Track] = []
        for entry in entries:
            if not isinstance(entry, dict):
                continue
            url = str(entry.get("url") or entry.get("webpage_url") or "")
            title = str(entry.get("title") or "").strip()
            if not url or not title:
                continue
            duration = float(entry.get("duration") or 0.0)
            track_id = "web" + _sha1(url)[:14]
            tracks.append(
                Track(
                    track_id=track_id,
                    title=title,
                    source="online",
                    origin=url,
                    duration=duration,
                    cached_path=self._cached_wav(track_id),
                )
            )
        return tracks[:limit]

    def _http_json(self, url: str) -> Dict[str, Any]:
        body = self._http_get(url, timeout=SEARCH_TIMEOUT_SECONDS)
        try:
            payload = json.loads(body.decode("utf-8", errors="replace"))
        except json.JSONDecodeError as error:
            raise MusicError("接口返回的不是 JSON") from error
        if not isinstance(payload, dict):
            raise MusicError("接口返回格式不正确")
        return payload

    def _http_get(self, url: str, timeout: int) -> bytes:
        """先直连，失败再走系统代理——避免环境里的代理变量把外网请求打死。"""
        request = urllib.request.Request(
            url,
            headers={
                "User-Agent": BROWSER_USER_AGENT,
                "Referer": NETEASE_BASE,
                "Accept": "*/*",
            },
        )
        errors: List[str] = []
        for opener in (
            urllib.request.build_opener(urllib.request.ProxyHandler({})),
            urllib.request.build_opener(),
        ):
            try:
                with opener.open(request, timeout=timeout) as response:
                    return response.read()
            except (urllib.error.URLError, TimeoutError, OSError) as error:
                errors.append(str(error))
        raise MusicError(f"网络请求失败：{errors[-1] if errors else 'unknown'}")

    def _run_json(self, command: List[str], timeout: int) -> Dict[str, Any]:
        try:
            completed = subprocess.run(
                command,
                capture_output=True,
                timeout=timeout,
                check=False,
            )
        except FileNotFoundError as error:
            raise MusicError("没装 yt-dlp，无法在线搜索") from error
        except subprocess.TimeoutExpired as error:
            raise MusicError("在线搜索超时") from error
        output = completed.stdout.decode("utf-8", errors="replace").strip()
        if completed.returncode != 0 or not output:
            detail = completed.stderr.decode("utf-8", errors="replace").strip()[-300:]
            raise MusicError(f"yt-dlp 返回错误：{detail or completed.returncode}")
        try:
            payload = json.loads(output)
        except json.JSONDecodeError as error:
            raise MusicError("yt-dlp 输出不是 JSON") from error
        if not isinstance(payload, dict):
            raise MusicError("yt-dlp 输出格式不正确")
        return payload

    # ------------------------------------------------------------------ 转码
    def prepare(self, track: Track) -> Track:
        """把曲目转成 16k 单声道 WAV 并落到缓存目录（幂等、带锁）。"""
        if self._wav_exists(track.track_id):
            track.cached_path = self._cached_wav(track.track_id)
            track.duration = track.duration or self._duration(track.cached_path)
            return track

        with self._lock:
            lock = self._track_locks.setdefault(track.track_id, threading.Lock())
        with lock:
            if self._wav_exists(track.track_id):
                track.cached_path = self._cached_wav(track.track_id)
                return track
            target = self._wav_path(track.track_id)
            target.parent.mkdir(parents=True, exist_ok=True)
            temp = target.with_suffix(".part.wav")
            if track.source == "local":
                self._transcode_file(Path(track.origin), temp)
            else:
                self._download_online(track, temp)
            temp.replace(target)
            track.cached_path = str(target)
            track.duration = self._duration(str(target))
            self._meta[track.track_id] = {
                "title": track.title,
                "source": track.source,
                "origin": track.origin,
                "duration": track.duration,
            }
            self._save_meta()
            return track

    def _transcode_file(self, source: Path, target: Path) -> None:
        command = [
            "ffmpeg",
            "-hide_banner",
            "-loglevel",
            "error",
            "-y",
            "-i",
            str(source),
            "-vn",
            "-ac",
            str(TARGET_CHANNELS),
            "-ar",
            str(TARGET_RATE),
            "-c:a",
            "pcm_s16le",
            str(target),
        ]
        self._run_ffmpeg(command, timeout=120)

    def _download_online(self, track: Track, target: Path) -> None:
        if track.origin.startswith("netease:"):
            self._download_netease(track, target)
            return
        """yt-dlp 拉音频 -> ffmpeg 转码，全程走管道，不落中间文件。"""
        download_cmd = self._python_module_command(
            "yt_dlp",
            "-f",
            "bestaudio/best",
            "--no-playlist",
            "--no-warnings",
            "--quiet",
            "-o",
            "-",
            track.origin,
        )
        ffmpeg_cmd = [
            "ffmpeg",
            "-hide_banner",
            "-loglevel",
            "error",
            "-y",
            "-i",
            "pipe:0",
            "-vn",
            "-ac",
            str(TARGET_CHANNELS),
            "-ar",
            str(TARGET_RATE),
            "-c:a",
            "pcm_s16le",
            str(target),
        ]
        try:
            downloader = subprocess.Popen(
                download_cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE
            )
        except FileNotFoundError as error:
            raise MusicError("没装 yt-dlp，无法下载音频") from error
        try:
            converter = subprocess.Popen(
                ffmpeg_cmd, stdin=downloader.stdout, stderr=subprocess.PIPE
            )
        finally:
            if downloader.stdout is not None:
                downloader.stdout.close()
        deadline = time.time() + DOWNLOAD_TIMEOUT_SECONDS
        while True:
            try:
                converter.wait(timeout=1)
                break
            except subprocess.TimeoutExpired:
                if time.time() > deadline:
                    converter.kill()
                    downloader.kill()
                    raise MusicError("下载音频超时")
        downloader.wait(timeout=5)
        if converter.returncode != 0 or downloader.returncode not in (0, None):
            detail = (converter.stderr.read() if converter.stderr else b"").decode(
                "utf-8", errors="replace"
            )[-300:]
            raise MusicError(f"音频转码失败：{detail or converter.returncode}")
        if not target.is_file() or target.stat().st_size <= 1024:
            raise MusicError("下载到的音频为空")

    def _download_netease(self, track: Track, target: Path) -> None:
        """网易云：先取直链（有 20 分钟有效期），失败退回外链播放地址。"""
        song_id = track.origin.split(":", 1)[1]
        media_url = ""
        try:
            payload = self._http_json(
                f"{NETEASE_BASE}/api/song/enhance/player/url"
                f"?ids=[{song_id}]&br=128000"
            )
            entries = payload.get("data") or []
            if entries and isinstance(entries[0], dict):
                media_url = str(entries[0].get("url") or "")
        except MusicError:
            media_url = ""
        if not media_url:
            media_url = f"{NETEASE_BASE}/song/media/outer/url?id={song_id}.mp3"
        audio = self._http_get(media_url, timeout=DOWNLOAD_TIMEOUT_SECONDS)
        if len(audio) < 16384 or not (
            audio[:3] == b"ID3" or audio[:2] in (b"\xff\xfb", b"\xff\xf3")
        ):
            raise MusicError("拿到的不是可用的音频（可能是 VIP 版权受限）")
        temp_source = target.with_suffix(".src.mp3")
        temp_source.write_bytes(audio)
        try:
            self._transcode_file(temp_source, target)
        finally:
            try:
                temp_source.unlink()
            except OSError:
                pass

    def _run_ffmpeg(self, command: List[str], timeout: int) -> None:
        try:
            completed = subprocess.run(
                command, capture_output=True, timeout=timeout, check=False
            )
        except FileNotFoundError as error:
            raise MusicError("没装 ffmpeg，无法转码音频") from error
        except subprocess.TimeoutExpired as error:
            raise MusicError("ffmpeg 转码超时") from error
        if completed.returncode != 0:
            detail = completed.stderr.decode("utf-8", errors="replace")[-300:]
            raise MusicError(f"ffmpeg 失败：{detail or completed.returncode}")

    @staticmethod
    def _duration(path: Optional[str]) -> float:
        if not path:
            return 0.0
        try:
            size = Path(path).stat().st_size
        except OSError:
            return 0.0
        return max(0.0, (size - 44) / TARGET_BYTES_PER_SECOND)

    # ------------------------------------------------------------------ 流式输出
    def stream_info(self, track_id: str) -> Dict[str, Any]:
        """返回可以直接切片的 WAV 信息；缓存缺失时先准备。"""
        track = self.cached_track(track_id)
        if track is None:
            for candidate in self.scan_library():
                if candidate.track_id == track_id:
                    track = candidate
                    break
        if track is None:
            raise MusicError(f"未知曲目：{track_id}")
        self.prepare(track)
        path = Path(track.cached_path or "")
        if not path.is_file():
            raise MusicError("音频缓存不存在")
        data_offset, data_bytes = _wav_layout(path)
        return {
            "path": str(path),
            "title": track.title,
            "data_offset": data_offset,
            "data_bytes": data_bytes,
            "total_bytes": path.stat().st_size,
            "duration": data_bytes / TARGET_BYTES_PER_SECOND,
        }

    # ------------------------------------------------------------------ 播放会话
    def begin_session(self, device_id: str, playlist: List[Track], index: int = 0) -> Track:
        with self._lock:
            session = PlaybackSession(
                device_id=device_id,
                playlist=list(playlist),
                index=max(0, min(index, len(playlist) - 1)),
                state="playing",
            )
            self._sessions[device_id] = session
            track = session.current()
            if track is None:
                raise MusicError("候选列表为空")
            return track

    def session(self, device_id: str) -> Optional[PlaybackSession]:
        with self._lock:
            return self._sessions.get(device_id)

    def set_state(self, device_id: str, state: str) -> Optional[PlaybackSession]:
        with self._lock:
            session = self._sessions.get(device_id)
            if session is None:
                return None
            session.state = state
            session.updated_at = time.time()
            if state == "stopped":
                self._sessions.pop(device_id, None)
            return session

    def next_track(self, device_id: str) -> Optional[Track]:
        with self._lock:
            session = self._sessions.get(device_id)
            if session is None:
                return None
            if session.index + 1 >= len(session.playlist):
                return None
            session.index += 1
            session.state = "playing"
            session.updated_at = time.time()
            return session.current()

    def has_session(self, device_id: str) -> bool:
        with self._lock:
            session = self._sessions.get(device_id)
            return session is not None and session.state in {"playing", "paused"}


def _relevance(query: str, title: str, artist: str, duration: float) -> float:
    """粗排：歌名匹配 > 歌手匹配，再给完整长度（>=150s）加分。

    网易云的相关性排序会把翻唱短片段排前面，用户点「稻香」更想要原曲。
    """
    needle = _normalize(query)
    haystack_title = _normalize(title)
    haystack_artist = _normalize(artist)
    score = 0.0
    if needle == haystack_title:
        score += 3.0
    elif needle and needle in haystack_title:
        score += 2.0
    elif haystack_title and haystack_title in needle:
        score += 1.0
    for token in _tokens(query):
        normalized_token = _normalize(token)
        if not normalized_token:
            continue
        if normalized_token in haystack_title:
            score += 0.8
        elif normalized_token in haystack_artist:
            score += 0.6
    if duration >= 150:
        score += 1.2
    elif duration >= 90:
        score += 0.4
    return score


def _wav_layout(path: Path) -> Tuple[int, int]:
    """解析 WAV，返回 data 块偏移与长度。"""
    with path.open("rb") as handle:
        header = handle.read(12)
        if len(header) < 12 or header[:4] != b"RIFF" or header[8:12] != b"WAVE":
            raise MusicError("缓存文件不是 WAV")
        offset = 12
        while True:
            chunk_header = handle.read(8)
            if len(chunk_header) < 8:
                raise MusicError("WAV 缺少 data 块")
            chunk_id = chunk_header[:4]
            chunk_size = int.from_bytes(chunk_header[4:8], "little")
            if chunk_id == b"data":
                data_offset = offset + 8
                total = path.stat().st_size
                available = max(0, total - data_offset)
                if chunk_size in (0, 0xFFFFFFFF) or chunk_size > available:
                    chunk_size = available
                return data_offset, chunk_size
            offset += 8 + chunk_size + (chunk_size & 1)
            handle.seek(offset)
