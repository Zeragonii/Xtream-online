from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path


def _env_int(name: str, default: int) -> int:
    try:
        return int(os.getenv(name, str(default)))
    except ValueError:
        return default


def _env_float(name: str, default: float) -> float:
    try:
        return float(os.getenv(name, str(default)))
    except ValueError:
        return default


@dataclass(frozen=True)
class Settings:
    data_dir: Path = Path(os.getenv("DATA_DIR", "/data"))
    hls_dir: Path = Path(os.getenv("HLS_DIR", "/tmp/xtream-online/hls"))
    max_active_streams: int = max(1, _env_int("MAX_ACTIVE_STREAMS", 1))
    session_idle_timeout: int = max(10, _env_int("SESSION_IDLE_TIMEOUT", 35))
    session_start_timeout: float = max(3.0, _env_float("SESSION_START_TIMEOUT", 12.0))
    codec_probe_timeout: float = max(1.0, _env_float("CODEC_PROBE_TIMEOUT", 4.0))
    xtream_timeout: int = max(3, _env_int("XTREAM_TIMEOUT", 12))
    ffmpeg_loglevel: str = os.getenv("FFMPEG_LOGLEVEL", "warning")
    ffmpeg_mode: str = os.getenv("FFMPEG_MODE", "auto").lower()
    ffmpeg_video_encoder: str = os.getenv("FFMPEG_VIDEO_ENCODER", "libx264")
    ffmpeg_video_preset: str = os.getenv("FFMPEG_VIDEO_PRESET", "veryfast")
    ffmpeg_audio_encoder: str = os.getenv("FFMPEG_AUDIO_ENCODER", "aac")
    provider_release_delay: float = max(0.0, _env_float("PROVIDER_RELEASE_DELAY", 0.5))
    provider_cache_ttl: float = max(10.0, _env_float("PROVIDER_CACHE_TTL", 600.0))
    hls_time: int = max(1, _env_int("HLS_TIME", 2))
    hls_list_size: int = max(3, _env_int("HLS_LIST_SIZE", 12))
    m3u_max_bytes: int = max(1_000_000, _env_int("M3U_MAX_BYTES", 25_000_000))
    m3u_timeout: float = max(2.0, _env_float("M3U_TIMEOUT", 6.0))
    xtream_stream_base_url: str | None = os.getenv("XTREAM_STREAM_BASE_URL") or None
    app_version: str = os.getenv("APP_VERSION", "dev")

    @property
    def db_path(self) -> Path:
        # Keep the original filename for seamless upgrades of existing /data volumes.
        return self.data_dir / "xtream-web.db"

    @property
    def env_xtream(self) -> dict[str, str] | None:
        url = os.getenv("XTREAM_URL")
        username = os.getenv("XTREAM_USERNAME")
        password = os.getenv("XTREAM_PASSWORD")
        if url and username and password:
            return {
                "base_url": url,
                "username": username,
                "password": password,
                "output": os.getenv("XTREAM_OUTPUT", "ts"),
            }
        return None


settings = Settings()
settings.data_dir.mkdir(parents=True, exist_ok=True)
settings.hls_dir.mkdir(parents=True, exist_ok=True)
