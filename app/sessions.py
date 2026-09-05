from __future__ import annotations

import asyncio
import logging
import re
import shutil
import time
import uuid
from collections import deque
from dataclasses import dataclass, field
from pathlib import Path

from .config import settings
from .xtream import redact_url

logger = logging.getLogger("xtream-online.sessions")

_VIDEO_RE = re.compile(r"\bVideo:\s*([A-Za-z0-9_]+)", re.IGNORECASE)
_AUDIO_RE = re.compile(r"\bAudio:\s*([A-Za-z0-9_]+)", re.IGNORECASE)


def parse_ffmpeg_codec_line(line: str, codecs: dict[str, str | None]) -> bool:
    """Extract source codecs from FFmpeg's input description.

    Returns True when the line indicates FFmpeg has finished describing inputs and
    moved on to stream mapping/output setup.
    """
    video = _VIDEO_RE.search(line)
    if video and codecs.get("video") is None:
        codecs["video"] = video.group(1).lower()

    audio = _AUDIO_RE.search(line)
    if audio and codecs.get("audio") is None:
        codecs["audio"] = audio.group(1).lower()

    return "Stream mapping:" in line or line.startswith("Output #0")


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
    stderr_lines: deque[str] = field(default_factory=lambda: deque(maxlen=50))
    stderr_task: asyncio.Task | None = None


@dataclass
class ProcessAttempt:
    process: asyncio.subprocess.Process
    stderr_lines: deque[str]
    stderr_task: asyncio.Task
    source_codecs: dict[str, str | None]
    probe_complete: asyncio.Event


class SessionError(RuntimeError):
    pass


class StreamSessionManager:
    def __init__(self) -> None:
        self.sessions: dict[str, StreamSession] = {}
        self._lock = asyncio.Lock()
        # Only one provider startup sequence at a time. This prevents concurrent
        # POST /api/play calls fighting over one-connection provider accounts.
        self._start_lock = asyncio.Lock()
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
        video = codecs.get("video")
        audio = codecs.get("audio")
        if video is None:
            return "transcode"
        return "copy" if video == "h264" and audio in {None, "aac"} else "transcode"

    def _ffmpeg_command(
        self,
        upstream_url: str,
        directory: Path,
        mode: str,
        *,
        probe_input: bool = False,
    ) -> list[str]:
        playlist = directory / "index.m3u8"
        segments = directory / "segment_%06d.ts"
        command = [
            "ffmpeg",
            "-hide_banner",
            "-loglevel",
            "info" if probe_input else settings.ffmpeg_loglevel,
            "-nostdin",
            "-rw_timeout",
            str(int(settings.xtream_timeout * 1_000_000)),
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
            "12",
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
        codecs: dict[str, str | None],
        probe_complete: asyncio.Event,
    ) -> None:
        assert process.stderr is not None
        while True:
            line = await process.stderr.readline()
            if not line:
                probe_complete.set()
                return
            text = line.decode("utf-8", errors="replace").strip()
            if not text:
                continue
            safe_text = text.replace(upstream_url, "<upstream>")
            lines.append(safe_text)
            if parse_ffmpeg_codec_line(safe_text, codecs):
                probe_complete.set()

    async def _spawn(
        self,
        upstream_url: str,
        directory: Path,
        mode: str,
        *,
        probe_input: bool = False,
    ) -> ProcessAttempt:
        process = await asyncio.create_subprocess_exec(
            *self._ffmpeg_command(upstream_url, directory, mode, probe_input=probe_input),
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.PIPE,
        )
        lines: deque[str] = deque(maxlen=50)
        codecs: dict[str, str | None] = {"video": None, "audio": None}
        probe_complete = asyncio.Event()
        task = asyncio.create_task(
            self._drain_attempt_stderr(process, upstream_url, lines, codecs, probe_complete)
        )
        return ProcessAttempt(
            process=process,
            stderr_lines=lines,
            stderr_task=task,
            source_codecs=codecs,
            probe_complete=probe_complete,
        )

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

    async def _wait_for_probe(self, attempt: ProcessAttempt) -> dict[str, str | None]:
        try:
            await asyncio.wait_for(attempt.probe_complete.wait(), timeout=settings.codec_probe_timeout)
        except asyncio.TimeoutError:
            pass
        return dict(attempt.source_codecs)

    async def _wait_for_playlist(self, attempt: ProcessAttempt, directory: Path) -> bool:
        playlist = directory / "index.m3u8"
        deadline = time.monotonic() + settings.session_start_timeout
        while time.monotonic() < deadline:
            if attempt.process.returncode is not None:
                return False
            if playlist.exists() and playlist.stat().st_size > 0:
                return True
            await asyncio.sleep(0.1)
        return False

    @staticmethod
    def _clear_hls_files(directory: Path) -> None:
        for path in directory.iterdir():
            if path.is_file():
                try:
                    path.unlink()
                except FileNotFoundError:
                    pass

    async def _evict_if_needed(self) -> None:
        async with self._lock:
            while len(self.sessions) >= settings.max_active_streams:
                oldest = min(self.sessions.values(), key=lambda session: session.created_at)
                await self._stop_unlocked(oldest.id)

    def _matching_session(self, stream_id: int, requested_mode: str) -> StreamSession | None:
        for session in self.sessions.values():
            if session.stream_id != stream_id or session.process.returncode is not None:
                continue
            if requested_mode == "auto" or session.mode == requested_mode:
                session.last_access = time.monotonic()
                return session
        return None

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

        # The second duplicate request waits here, then reuses the session created by
        # the first instead of evicting it and starting another FFmpeg process.
        async with self._start_lock:
            existing = self._matching_session(stream_id, requested_mode)
            if existing:
                logger.info("Reusing session %s for duplicate stream %s request", existing.id[:8], stream_id)
                return existing

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

                    started = time.monotonic()
                    attempt = await self._spawn(
                        upstream_url,
                        directory,
                        initial_mode,
                        probe_input=requested_mode == "auto",
                    )

                    codecs = {"video": None, "audio": None}
                    final_mode = initial_mode

                    if requested_mode == "auto":
                        codecs = await self._wait_for_probe(attempt)
                        if attempt.process.returncode is not None:
                            last_details = " | ".join(attempt.stderr_lines) or "FFmpeg exited while probing input"
                            logger.warning("Stream %s candidate %d failed during input probe: %s", stream_id, index, last_details)
                            await self._terminate_attempt(attempt)
                            continue

                        final_mode = self.choose_mode(requested_mode, codecs)
                        logger.info(
                            "Stream %s input probe in %.0fms: video=%s audio=%s -> %s",
                            stream_id,
                            (time.monotonic() - started) * 1000,
                            codecs.get("video"),
                            codecs.get("audio"),
                            final_mode,
                        )

                        if final_mode == "transcode":
                            await self._terminate_attempt(attempt)
                            self._clear_hls_files(directory)
                            if settings.provider_release_delay:
                                await asyncio.sleep(settings.provider_release_delay)
                            attempt = await self._spawn(upstream_url, directory, "transcode")

                    ready = await self._wait_for_playlist(attempt, directory)
                    if not ready:
                        last_details = " | ".join(attempt.stderr_lines) or "FFmpeg produced no HLS playlist"
                        logger.warning("Stream %s candidate %d failed: %s", stream_id, index, last_details)
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
                        while len(self.sessions) >= settings.max_active_streams:
                            oldest = min(self.sessions.values(), key=lambda item: item.created_at)
                            await self._stop_unlocked(oldest.id)
                        self.sessions[session_id] = session

                    logger.info(
                        "Stream %s started as session %s using %s in %.2fs (%s)",
                        stream_id,
                        session_id[:8],
                        final_mode,
                        time.monotonic() - started,
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
