from __future__ import annotations

import asyncio
import logging
import time
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from .catalog import catalog_service
from .config import settings
from .sessions import SessionError, session_manager
from .storage import ProviderConfig, catalog_store, provider_cache_key, store
from .xtream import XtreamClient, XtreamError, normalize_base_url

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
logger = logging.getLogger("xtream-online")
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("httpcore").setLevel(logging.WARNING)

STATIC_DIR = Path(__file__).parent / "static"


@asynccontextmanager
async def lifespan(_: FastAPI):
    await session_manager.start_background_tasks()
    await catalog_service.start()
    yield
    await catalog_service.shutdown()
    await session_manager.shutdown()


app = FastAPI(title="Xtream Online", version=settings.app_version, lifespan=lifespan)
app.mount("/assets", StaticFiles(directory=STATIC_DIR), name="assets")


class ProviderConfigRequest(BaseModel):
    base_url: str = Field(min_length=1, max_length=2048)
    username: str = Field(min_length=1, max_length=512)
    password: str = Field(min_length=1, max_length=512)
    output: str = "ts"


class PlayRequest(BaseModel):
    mode: str = "auto"


class ClientEventRequest(BaseModel):
    event: str = Field(min_length=1, max_length=128)
    detail: str = Field(default="", max_length=2000)
    level: str = Field(default="warning", max_length=16)


def configured_client() -> XtreamClient:
    config = store.get()
    if not config:
        raise HTTPException(status_code=409, detail="Xtream provider is not configured")
    return XtreamClient(config)


def api_error(exc: Exception, status_code: int = 502) -> HTTPException:
    return HTTPException(status_code=status_code, detail=str(exc))


@app.get("/", include_in_schema=False)
async def index() -> FileResponse:
    return FileResponse(STATIC_DIR / "index.html")


@app.get("/api/health")
async def health() -> dict:
    return {
        "status": "ok",
        "version": settings.app_version,
        "configured": store.get() is not None,
        "active_streams": len(session_manager.sessions),
    }


@app.get("/api/status")
async def status() -> dict:
    config = store.get()
    return {
        "configured": config is not None,
        "configuration_source": store.source(),
        "output": config.output if config else None,
        "version": settings.app_version,
        "max_active_streams": settings.max_active_streams,
        "idle_timeout": settings.session_idle_timeout,
        "default_ffmpeg_mode": settings.ffmpeg_mode,
    }


@app.post("/api/config")
async def configure_provider(payload: ProviderConfigRequest) -> dict:
    if settings.env_xtream:
        raise HTTPException(status_code=409, detail="Provider configuration is managed by environment variables")
    if payload.output not in {"ts", "m3u8"}:
        raise HTTPException(status_code=422, detail="Output must be ts or m3u8")

    config = ProviderConfig(
        base_url=normalize_base_url(payload.base_url),
        username=payload.username,
        password=payload.password,
        output=payload.output,
    )
    client = XtreamClient(config)
    try:
        auth = await client.authenticate(force=True)
    except XtreamError as exc:
        raise api_error(exc, 400) from exc

    store.save(config)
    catalog_store.clear()
    await catalog_service.request_refresh(force=True)
    user_info = auth.get("user_info", {}) if isinstance(auth, dict) else {}
    return {
        "ok": True,
        "status": user_info.get("status"),
        "expires": user_info.get("exp_date"),
        "catalog_refresh_started": True,
    }


@app.delete("/api/config")
async def clear_provider() -> dict:
    try:
        store.clear()
    except RuntimeError as exc:
        raise api_error(exc, 409) from exc
    await session_manager.stop_all()
    catalog_store.clear()
    return {"ok": True}


@app.get("/api/catalog/status")
async def catalog_status() -> dict:
    return await catalog_service.status()


@app.post("/api/catalog/refresh")
async def refresh_catalog() -> dict:
    configured_client()  # return 409 immediately if there is no provider
    started = await catalog_service.request_refresh(force=True)
    status = await catalog_service.status()
    return {"ok": True, "started": started, **status}


@app.get("/api/categories")
async def categories() -> list[dict]:
    config = store.get()
    if not config:
        raise HTTPException(status_code=409, detail="Xtream provider is not configured")
    key = provider_cache_key(config)
    items = await asyncio.to_thread(catalog_store.categories, key)
    if not items:
        await catalog_service.request_refresh(force=False)
    return items


@app.get("/api/channels")
async def channels(
    category_id: str | None = Query(default=None),
    q: str = Query(default="", max_length=200),
    offset: int = Query(default=0, ge=0),
    limit: int = Query(default=200, ge=1, le=500),
) -> dict:
    config = store.get()
    if not config:
        raise HTTPException(status_code=409, detail="Xtream provider is not configured")
    key = provider_cache_key(config)
    items, total = await asyncio.to_thread(
        catalog_store.channels,
        key,
        category_id=category_id,
        search=q.strip(),
        offset=offset,
        limit=limit,
    )
    status = await catalog_service.status()
    if not status["ready"]:
        await catalog_service.request_refresh(force=False)
    return {
        "items": items,
        "total": total,
        "offset": offset,
        "limit": limit,
        "ready": status["ready"],
        "refreshing": status["refreshing"],
    }


@app.post("/api/play/{stream_id}")
async def play(stream_id: int, payload: PlayRequest) -> dict:
    client = configured_client()
    mode = payload.mode if payload.mode in {"auto", "copy", "transcode"} else settings.ffmpeg_mode
    started = time.monotonic()
    try:
        candidates = await client.stream_candidates(stream_id)
        try:
            session = await session_manager.start(stream_id, candidates, mode)
        except SessionError as primary_exc:
            # Expensive provider M3U discovery is deliberately last-resort only.
            logger.info("Fast candidates failed for stream %s; trying provider M3U fallback", stream_id)
            fallback = await client.playlist_candidates(stream_id)
            if not fallback:
                raise primary_exc
            session = await session_manager.start(stream_id, fallback, mode)

        client.remember_success(session.upstream_url)
    except (SessionError, OSError) as exc:
        logger.warning("Could not start stream %s after %.2fs: %s", stream_id, time.monotonic() - started, exc)
        raise api_error(exc) from exc

    logger.info("Playback request for stream %s completed in %.2fs", stream_id, time.monotonic() - started)
    return {
        "session_id": session.id,
        "stream_id": stream_id,
        "playlist": f"/hls/{session.id}/index.m3u8",
        "mode": session.mode,
        "source_codecs": session.source_codecs,
    }


@app.get("/api/session/{session_id}/diagnostics")
async def session_diagnostics(session_id: str) -> dict:
    session = await session_manager.touch(session_id)
    if not session:
        raise HTTPException(status_code=404, detail="Stream session not found")

    playlist = session.directory / "index.m3u8"
    segments = sorted(session.directory.glob("segment_*.ts"))
    stderr_tail = list(session.stderr_lines)[-20:]
    return {
        "session_id": session.id,
        "stream_id": session.stream_id,
        "mode": session.mode,
        "source_codecs": session.source_codecs,
        "process_alive": session.process.returncode is None,
        "process_returncode": session.process.returncode,
        "playlist_exists": playlist.exists(),
        "playlist_bytes": playlist.stat().st_size if playlist.exists() else 0,
        "segment_count": len(segments),
        "newest_segment_bytes": segments[-1].stat().st_size if segments else 0,
        "stderr_tail": stderr_tail,
    }


@app.post("/api/session/{session_id}/client-event")
async def session_client_event(session_id: str, payload: ClientEventRequest) -> dict:
    session = session_manager.sessions.get(session_id)
    if not session:
        raise HTTPException(status_code=404, detail="Stream session not found")

    detail = payload.detail.replace("\n", " ").replace("\r", " ")[:2000]
    message = "Browser event session=%s stream=%s event=%s detail=%s"
    args = (session.id[:8], session.stream_id, payload.event, detail or "-")
    if payload.level.lower() == "info":
        logger.info(message, *args)
    else:
        logger.warning(message, *args)
    return {"ok": True}


@app.delete("/api/session/{session_id}")
async def stop_session(session_id: str) -> dict:
    await session_manager.stop(session_id)
    return {"ok": True}


@app.post("/api/session/{session_id}/stop")
async def stop_session_beacon(session_id: str) -> dict:
    # POST twin used by navigator.sendBeacon() during tab/window teardown.
    await session_manager.stop(session_id)
    return {"ok": True}


@app.get("/api/sessions")
async def sessions() -> list[dict]:
    return [
        {
            "session_id": session.id,
            "stream_id": session.stream_id,
            "mode": session.mode,
            "source_codecs": session.source_codecs,
        }
        for session in session_manager.sessions.values()
    ]


@app.get("/hls/{session_id}/{filename}", include_in_schema=False)
async def hls_file(session_id: str, filename: str):
    session = await session_manager.touch(session_id)
    if not session:
        raise HTTPException(status_code=404, detail="Stream session not found")

    if filename != "index.m3u8" and not (
        filename.startswith("segment_") and filename.endswith(".ts") and filename[8:-3].isdigit()
    ):
        raise HTTPException(status_code=404, detail="File not found")

    path = session.directory / filename
    if not path.exists():
        raise HTTPException(status_code=404, detail="Segment not ready")

    headers = {"Cache-Control": "no-store" if filename.endswith(".m3u8") else "no-cache"}
    media_type = "application/vnd.apple.mpegurl" if filename.endswith(".m3u8") else "video/mp2t"
    return FileResponse(path, media_type=media_type, headers=headers)


@app.exception_handler(XtreamError)
async def xtream_exception_handler(_, exc: XtreamError):
    return JSONResponse(status_code=502, content={"detail": str(exc)})
