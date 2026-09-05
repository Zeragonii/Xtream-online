from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path


def _env_int(name: str, default: int) -> int:
    try:
        return int(os.getenv(name, str(default)))
    except ValueError:
        return default


@dataclass(frozen=True)
class Settings:
    data_dir: Path = Path(os.getenv("DATA_DIR", "/data"))
    hls_dir: Path = Path(os.getenv("HLS_DIR", "/tmp/xtream-web/hls"))
    max_active_streams: int = max(1, _env_int("MAX_ACTIVE_STREAMS", 1))
    session_idle_timeout: int = max(10, _env_int("SESSION_IDLE_TIMEOUT", 35))
    session_start_timeout: int = max(5, _env_int("SESSION_START_TIMEOUT", 15))
    xtream_timeout: int = max(5, _env_int("XTREAM_TIMEOUT", 15))
    ffprobe_timeout: int = max(5, _env_int("FFPROBE_TIMEOUT", 12))
    ffmpeg_loglevel: str = os.getenv("FFMPEG_LOGLEVEL", "warning")
    ffmpeg_mode: str = os.getenv("FFMPEG_MODE", "auto").lower()
    ffmpeg_video_encoder: str = os.getenv("FFMPEG_VIDEO_ENCODER", "libx264")
    ffmpeg_video_preset: str = os.getenv("FFMPEG_VIDEO_PRESET", "veryfast")
    ffmpeg_audio_encoder: str = os.getenv("FFMPEG_AUDIO_ENCODER", "aac")
    hls_time: int = max(1, _env_int("HLS_TIME", 2))
    hls_list_size: int = max(3, _env_int("HLS_LIST_SIZE", 6))
    app_version: str = os.getenv("APP_VERSION", "dev")

    @property
    def db_path(self) -> Path:
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
