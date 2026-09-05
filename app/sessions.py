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
from .xtream import redact_url

logger = logging.getLogger("xtream-online.sessions")


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
    stderr_lines: deque[str] = field(default_factory=lambda: deque(maxlen=40))
    stderr_task: asyncio.Task | None = None


@dataclass
class ProcessAttempt:
    process: asyncio.subprocess.Process
    stderr_lines: deque[str]
    stderr_task: asyncio.Task


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

    @staticmethod
    def choose_mode(requested_mode: str, codecs: dict[str, str | None]) -> str:
        requested_mode = requested_mode.lower()
        if requested_mode in {"copy", "transcode"}:
            return requested_mode
        # Conservative cross-browser baseline: H.264 video and AAC audio.
        video = codecs.get("video")
        audio = codecs.get("audio")
        if video is None:
            return "transcode"
        return "copy" if video == "h264" and audio in {None, "aac"} else "transcode"

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

    async def _drain_attempt_stderr(
        self,
        process: asyncio.subprocess.Process,
        upstream_url: str,
        lines: deque[str],
    ) -> None:
        assert process.stderr is not None
        while True:
            line = await process.stderr.readline()
            if not line:
                return
            text = line.decode("utf-8", errors="replace").strip()
            if text:
                # FFmpeg sometimes prints the full URL; never retain provider credentials.
                safe_text = text.replace(upstream_url, "<upstream>")
                lines.append(safe_text)

    async def _spawn(self, upstream_url: str, directory: Path, mode: str) -> ProcessAttempt:
        process = await asyncio.create_subprocess_exec(
            *self._ffmpeg_command(upstream_url, directory, mode),
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.PIPE,
        )
        lines: deque[str] = deque(maxlen=40)
        task = asyncio.create_task(self._drain_attempt_stderr(process, upstream_url, lines))
        return ProcessAttempt(process=process, stderr_lines=lines, stderr_task=task)

    async def _terminate_attempt(self, attempt: ProcessAttempt) -> None:
        if attempt.process.returncode is None:
            attempt.process.terminate()
            try:
                await asyncio.wait_for(attempt.process.wait(), timeout=4)
            except asyncio.TimeoutError:
                attempt.process.kill()
                await attempt.process.wait()
        if not attempt.stderr_task.done():
            attempt.stderr_task.cancel()
        try:
            await attempt.stderr_task
        except asyncio.CancelledError:
            pass

    async def _wait_for_playlist(self, attempt: ProcessAttempt, directory: Path) -> bool:
        playlist = directory / "index.m3u8"
        deadline = time.monotonic() + settings.session_start_timeout
        while time.monotonic() < deadline:
            if attempt.process.returncode is not None:
                return False
            if playlist.exists() and playlist.stat().st_size > 0:
                return True
            await asyncio.sleep(0.2)
        return False

    @staticmethod
    def _clear_hls_files(directory: Path) -> None:
        for path in directory.iterdir():
            if path.is_file():
                try:
                    path.unlink()
                except FileNotFoundError:
                    pass

    async def _probe_local_output(self, directory: Path) -> dict[str, str | None]:
        """Probe our local remuxed segment, never the provider URL.

        This is deliberately different from the original v0.1 implementation.
        Probing the upstream first can briefly consume a provider connection, which
        breaks accounts limited to one active stream when FFmpeg connects moments later.
        """
        segment: Path | None = None
        deadline = time.monotonic() + settings.ffprobe_timeout
        while time.monotonic() < deadline:
            segments = sorted(directory.glob("segment_*.ts"))
            if segments:
                segment = segments[0]
                break
            await asyncio.sleep(0.1)
        if segment is None:
            return {"video": None, "audio": None}

        command = [
            "ffprobe",
            "-v",
            "error",
            "-show_entries",
            "stream=codec_type,codec_name",
            "-of",
            "json",
            str(segment),
        ]
        try:
            proc = await asyncio.create_subprocess_exec(
                *command,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=settings.ffprobe_timeout)
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

    async def _evict_if_needed(self) -> None:
        async with self._lock:
            while len(self.sessions) >= settings.max_active_streams:
                oldest = min(self.sessions.values(), key=lambda session: session.created_at)
                await self._stop_unlocked(oldest.id)

    async def start(
        self,
        stream_id: int,
        upstream_urls: str | list[str],
        requested_mode: str = "auto",
    ) -> StreamSession:
        if requested_mode not in {"auto", "copy", "transcode"}:
            raise SessionError("Playback mode must be auto, copy, or transcode")

        candidates = [upstream_urls] if isinstance(upstream_urls, str) else list(upstream_urls)
        candidates = list(dict.fromkeys(url for url in candidates if url))
        if not candidates:
            raise SessionError("Provider did not supply a usable stream URL")

        await self._evict_if_needed()

        session_id = uuid.uuid4().hex
        directory = settings.hls_dir / session_id
        directory.mkdir(parents=True, exist_ok=False)
        last_details = "No stream candidate produced media"

        try:
            for index, upstream_url in enumerate(candidates, start=1):
                self._clear_hls_files(directory)
                initial_mode = "copy" if requested_mode == "auto" else requested_mode
                logger.info(
                    "Trying stream %s candidate %d/%d (%s) in %s mode",
                    stream_id,
                    index,
                    len(candidates),
                    redact_url(upstream_url),
                    initial_mode,
                )

                attempt = await self._spawn(upstream_url, directory, initial_mode)
                ready = await self._wait_for_playlist(attempt, directory)
                if not ready:
                    last_details = " | ".join(attempt.stderr_lines) or "FFmpeg produced no HLS playlist"
                    logger.warning(
                        "Stream %s candidate %d failed: %s",
                        stream_id,
                        index,
                        last_details,
                    )
                    await self._terminate_attempt(attempt)
                    continue

                codecs = (
                    await self._probe_local_output(directory)
                    if requested_mode == "auto"
                    else {"video": None, "audio": None}
                )
                final_mode = self.choose_mode(requested_mode, codecs)

                if requested_mode == "auto" and final_mode == "transcode":
                    logger.info(
                        "Stream %s remux probe found video=%s audio=%s; restarting as transcode",
                        stream_id,
                        codecs.get("video"),
                        codecs.get("audio"),
                    )
                    await self._terminate_attempt(attempt)
                    self._clear_hls_files(directory)
                    if settings.provider_release_delay:
                        await asyncio.sleep(settings.provider_release_delay)

                    attempt = await self._spawn(upstream_url, directory, "transcode")
                    ready = await self._wait_for_playlist(attempt, directory)
                    if not ready:
                        last_details = " | ".join(attempt.stderr_lines) or "FFmpeg produced no HLS playlist"
                        logger.warning(
                            "Stream %s transcode restart failed for candidate %d: %s",
                            stream_id,
                            index,
                            last_details,
                        )
                        await self._terminate_attempt(attempt)
                        continue

                session = StreamSession(
                    id=session_id,
                    stream_id=stream_id,
                    upstream_url=upstream_url,
                    directory=directory,
                    process=attempt.process,
                    mode=final_mode,
                    source_codecs=codecs,
                    stderr_lines=attempt.stderr_lines,
                    stderr_task=attempt.stderr_task,
                )
                async with self._lock:
                    # Another concurrent start may have filled the limit while this
                    # candidate was opening. Evict oldest before registering this one.
                    while len(self.sessions) >= settings.max_active_streams:
                        oldest = min(self.sessions.values(), key=lambda item: item.created_at)
                        await self._stop_unlocked(oldest.id)
                    self.sessions[session_id] = session

                logger.info(
                    "Stream %s started as session %s using %s (%s)",
                    stream_id,
                    session_id[:8],
                    final_mode,
                    redact_url(upstream_url),
                )
                return session
        except Exception:
            shutil.rmtree(directory, ignore_errors=True)
            raise

        shutil.rmtree(directory, ignore_errors=True)
        raise SessionError(f"FFmpeg could not open the provider stream. Last error: {last_details}")

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
