from __future__ import annotations

import asyncio
import json
import logging
import shutil
import time
import uuid
from collections import deque
from dataclasses import dataclass, field
from pathlib import Path

from .config import settings

logger = logging.getLogger("xtream-web.sessions")


@dataclass
class StreamSession:
    id: str
    stream_id: int
    upstream_url: str
    directory: Path
    process: asyncio.subprocess.Process
    mode: str
    source_codecs: dict[str, str | None]
    created_at: float = field(default_factory=time.monotonic)
    last_access: float = field(default_factory=time.monotonic)
    stderr_lines: deque[str] = field(default_factory=lambda: deque(maxlen=30))
    stderr_task: asyncio.Task | None = None


class SessionError(RuntimeError):
    pass


class StreamSessionManager:
    def __init__(self) -> None:
        self.sessions: dict[str, StreamSession] = {}
        self._lock = asyncio.Lock()
        self._reaper_task: asyncio.Task | None = None

    async def start_background_tasks(self) -> None:
        if not self._reaper_task:
            self._reaper_task = asyncio.create_task(self._reaper(), name="stream-session-reaper")

    async def shutdown(self) -> None:
        if self._reaper_task:
            self._reaper_task.cancel()
            try:
                await self._reaper_task
            except asyncio.CancelledError:
                pass
            self._reaper_task = None
        await self.stop_all()

    async def _reaper(self) -> None:
        while True:
            await asyncio.sleep(5)
            now = time.monotonic()
            stale = [
                session_id
                for session_id, session in list(self.sessions.items())
                if now - session.last_access > settings.session_idle_timeout
                or session.process.returncode is not None
            ]
            for session_id in stale:
                await self.stop(session_id)

    async def _probe(self, upstream_url: str) -> dict[str, str | None]:
        command = [
            "ffprobe",
            "-v",
            "error",
            "-rw_timeout",
            str(settings.ffprobe_timeout * 1_000_000),
            "-show_entries",
            "stream=codec_type,codec_name",
            "-of",
            "json",
            upstream_url,
        ]
        try:
            proc = await asyncio.create_subprocess_exec(
                *command,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=settings.ffprobe_timeout + 2)
            if proc.returncode != 0:
                return {"video": None, "audio": None}
            payload = json.loads(stdout.decode("utf-8", errors="replace"))
            result: dict[str, str | None] = {"video": None, "audio": None}
            for stream in payload.get("streams", []):
                stream_type = stream.get("codec_type")
                if stream_type in result and result[stream_type] is None:
                    result[stream_type] = stream.get("codec_name")
            return result
        except (asyncio.TimeoutError, ValueError, OSError):
            return {"video": None, "audio": None}

    @staticmethod
    def choose_mode(requested_mode: str, codecs: dict[str, str | None]) -> str:
        requested_mode = requested_mode.lower()
        if requested_mode in {"copy", "transcode"}:
            return requested_mode
        # Conservative browser baseline: H.264 video and AAC audio.
        video = codecs.get("video")
        audio = codecs.get("audio")
        if video is None:
            return "transcode"
        video_ok = video == "h264"
        audio_ok = audio in {None, "aac"}
        return "copy" if video_ok and audio_ok else "transcode"

    def _ffmpeg_command(self, upstream_url: str, directory: Path, mode: str) -> list[str]:
        playlist = directory / "index.m3u8"
        segments = directory / "segment_%06d.ts"
        command = [
            "ffmpeg",
            "-hide_banner",
            "-loglevel",
            settings.ffmpeg_loglevel,
            "-nostdin",
            "-rw_timeout",
            str(settings.xtream_timeout * 1_000_000),
            "-fflags",
            "+genpts+discardcorrupt",
            "-i",
            upstream_url,
            "-map",
            "0:v:0?",
            "-map",
            "0:a:0?",
            "-sn",
            "-dn",
        ]

        if mode == "copy":
            command += ["-c:v", "copy", "-c:a", "copy"]
        else:
            command += [
                "-c:v",
                settings.ffmpeg_video_encoder,
                "-preset",
                settings.ffmpeg_video_preset,
                "-tune",
                "zerolatency",
                "-pix_fmt",
                "yuv420p",
                "-force_key_frames",
                f"expr:gte(t,n_forced*{settings.hls_time})",
                "-c:a",
                settings.ffmpeg_audio_encoder,
                "-b:a",
                "160k",
                "-ac",
                "2",
            ]

        command += [
            "-max_muxing_queue_size",
            "2048",
            "-f",
            "hls",
            "-hls_time",
            str(settings.hls_time),
            "-hls_list_size",
            str(settings.hls_list_size),
            "-hls_delete_threshold",
            "2",
            "-hls_flags",
            "delete_segments+omit_endlist",
            "-hls_segment_filename",
            str(segments),
            str(playlist),
        ]
        return command

    async def _drain_stderr(self, session: StreamSession) -> None:
        assert session.process.stderr is not None
        while True:
            line = await session.process.stderr.readline()
            if not line:
                return
            text = line.decode("utf-8", errors="replace").strip()
            if text:
                safe_text = text.replace(session.upstream_url, "<upstream>")
                session.stderr_lines.append(safe_text)
                logger.debug("ffmpeg[%s]: %s", session.id, safe_text)

    async def start(self, stream_id: int, upstream_url: str, requested_mode: str = "auto") -> StreamSession:
        if requested_mode not in {"auto", "copy", "transcode"}:
            raise SessionError("Playback mode must be auto, copy, or transcode")

        codecs = await self._probe(upstream_url) if requested_mode == "auto" else {"video": None, "audio": None}
        mode = self.choose_mode(requested_mode, codecs)

        async with self._lock:
            while len(self.sessions) >= settings.max_active_streams:
                oldest = min(self.sessions.values(), key=lambda session: session.created_at)
                await self._stop_unlocked(oldest.id)

            session_id = uuid.uuid4().hex
            directory = settings.hls_dir / session_id
            directory.mkdir(parents=True, exist_ok=False)
            process = await asyncio.create_subprocess_exec(
                *self._ffmpeg_command(upstream_url, directory, mode),
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.PIPE,
            )
            session = StreamSession(
                id=session_id,
                stream_id=stream_id,
                upstream_url=upstream_url,
                directory=directory,
                process=process,
                mode=mode,
                source_codecs=codecs,
            )
            session.stderr_task = asyncio.create_task(self._drain_stderr(session))
            self.sessions[session_id] = session

        playlist = directory / "index.m3u8"
        deadline = time.monotonic() + settings.session_start_timeout
        while time.monotonic() < deadline:
            if process.returncode is not None:
                break
            if playlist.exists() and playlist.stat().st_size > 0:
                return session
            await asyncio.sleep(0.2)

        details = " | ".join(session.stderr_lines) or "FFmpeg did not produce an HLS playlist"
        logger.warning("ffmpeg[%s] failed to start: %s", session_id, details)
        await self.stop(session_id)
        raise SessionError("FFmpeg failed to start this stream; check the container logs for details")

    async def touch(self, session_id: str) -> StreamSession | None:
        session = self.sessions.get(session_id)
        if session:
            session.last_access = time.monotonic()
        return session

    async def stop(self, session_id: str) -> None:
        async with self._lock:
            await self._stop_unlocked(session_id)

    async def _stop_unlocked(self, session_id: str) -> None:
        session = self.sessions.pop(session_id, None)
        if not session:
            return

        if session.process.returncode is None:
            session.process.terminate()
            try:
                await asyncio.wait_for(session.process.wait(), timeout=4)
            except asyncio.TimeoutError:
                session.process.kill()
                await session.process.wait()

        if session.stderr_task:
            if not session.stderr_task.done():
                session.stderr_task.cancel()
            try:
                await session.stderr_task
            except asyncio.CancelledError:
                pass

        shutil.rmtree(session.directory, ignore_errors=True)

    async def stop_all(self) -> None:
        async with self._lock:
            for session_id in list(self.sessions):
                await self._stop_unlocked(session_id)


session_manager = StreamSessionManager()
