from __future__ import annotations

import asyncio
import logging
import re
import tempfile
import time
from contextlib import suppress
from datetime import datetime, timezone, timedelta
from pathlib import Path
from xml.etree import ElementTree as ET

from .config import settings
from .storage import catalog_store, epg_store, provider_cache_key, store
from .xtream import XtreamClient, XtreamError

logger = logging.getLogger("xtream-online.epg")
_XMLTV_RE = re.compile(r"^(\d{8,14})(?:\s*([+-]\d{4}|Z))?")


def parse_xmltv_time(value: str) -> float | None:
    match = _XMLTV_RE.match((value or "").strip())
    if not match:
        return None
    digits, offset = match.groups()
    fmt = {
        8: "%Y%m%d",
        10: "%Y%m%d%H",
        12: "%Y%m%d%H%M",
        14: "%Y%m%d%H%M%S",
    }.get(len(digits))
    if not fmt:
        return None
    try:
        dt = datetime.strptime(digits, fmt)
        if offset and offset != "Z":
            sign = 1 if offset[0] == "+" else -1
            hours = int(offset[1:3])
            minutes = int(offset[3:5])
            dt = dt.replace(tzinfo=timezone(sign * timedelta(hours=hours, minutes=minutes)))
        else:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.timestamp()
    except (ValueError, OverflowError):
        return None


def _child_text(element: ET.Element, name: str) -> str:
    child = element.find(name)
    return (child.text or "").strip() if child is not None else ""


def parse_xmltv_file(
    path: Path,
    wanted_channel_ids: set[str],
    *,
    earliest_ts: float,
    latest_ts: float,
) -> list[dict]:
    programmes: list[dict] = []
    if not wanted_channel_ids:
        return programmes

    for _, elem in ET.iterparse(path, events=("end",)):
        if elem.tag != "programme":
            # Do not clear programme children (title/desc/category) before the
            # enclosing programme end event has been processed. Top-level
            # channel metadata is not needed once encountered.
            if elem.tag == "channel":
                elem.clear()
            continue

        channel_id = str(elem.attrib.get("channel") or "")
        if channel_id not in wanted_channel_ids:
            elem.clear()
            continue

        start_ts = parse_xmltv_time(str(elem.attrib.get("start") or ""))
        stop_ts = parse_xmltv_time(str(elem.attrib.get("stop") or ""))
        if start_ts is None or stop_ts is None or stop_ts <= start_ts:
            elem.clear()
            continue
        if stop_ts <= earliest_ts or start_ts >= latest_ts:
            elem.clear()
            continue

        programmes.append(
            {
                "epg_channel_id": channel_id,
                "start_ts": start_ts,
                "stop_ts": stop_ts,
                "title": _child_text(elem, "title") or "Untitled",
                "description": _child_text(elem, "desc"),
                "category": _child_text(elem, "category"),
            }
        )
        elem.clear()

    programmes.sort(key=lambda item: (item["epg_channel_id"], item["start_ts"]))
    return programmes


class EpgService:
    def __init__(self) -> None:
        self._refresh_task: asyncio.Task | None = None
        self._periodic_task: asyncio.Task | None = None
        self._lock = asyncio.Lock()

    async def start(self) -> None:
        if not self._periodic_task or self._periodic_task.done():
            self._periodic_task = asyncio.create_task(self._periodic_loop())
        await self.request_refresh(force=False)

    async def shutdown(self) -> None:
        for task in (self._refresh_task, self._periodic_task):
            if task and not task.done():
                task.cancel()
        for task in (self._refresh_task, self._periodic_task):
            if task:
                with suppress(asyncio.CancelledError):
                    await task

    async def _periodic_loop(self) -> None:
        while True:
            await asyncio.sleep(60)
            await self.request_refresh(force=False)

    async def request_refresh(self, *, force: bool) -> bool:
        config = store.get()
        if not config:
            return False
        key = provider_cache_key(config)
        status = await asyncio.to_thread(epg_store.status, key)
        if not force and status["ready"] and status["age_seconds"] is not None:
            if status["age_seconds"] < settings.epg_refresh_interval:
                return False

        wanted_ids = await asyncio.to_thread(catalog_store.epg_channel_ids, key)
        if not wanted_ids:
            return False

        async with self._lock:
            if self._refresh_task and not self._refresh_task.done():
                return False
            self._refresh_task = asyncio.create_task(self._refresh(config, key, wanted_ids))
            return True

    async def _refresh(self, config, key: str, wanted_ids: set[str]) -> None:
        started = time.monotonic()
        logger.info("Refreshing EPG in background for %s mapped channel(s)", len(wanted_ids))
        client = XtreamClient(config)
        temp_path: Path | None = None
        try:
            with tempfile.NamedTemporaryFile(prefix="xtream-online-epg-", suffix=".xml", delete=False) as tmp:
                temp_path = Path(tmp.name)
            byte_count = await client.download_xmltv(temp_path)
            now = time.time()
            programmes = await asyncio.to_thread(
                parse_xmltv_file,
                temp_path,
                wanted_ids,
                earliest_ts=now - settings.epg_past_hours * 3600,
                latest_ts=now + settings.epg_horizon_hours * 3600,
            )
            await asyncio.to_thread(epg_store.replace, key, programmes)
            logger.info(
                "EPG refresh complete in %.2fs: %.1f MiB, %s programme(s), %s channel(s)",
                time.monotonic() - started,
                byte_count / 1024 / 1024,
                len(programmes),
                len({item["epg_channel_id"] for item in programmes}),
            )
        except (XtreamError, OSError, ValueError, ET.ParseError) as exc:
            await asyncio.to_thread(epg_store.set_error, key, str(exc))
            logger.warning("EPG refresh failed after %.2fs: %s", time.monotonic() - started, exc)
        finally:
            if temp_path:
                with suppress(OSError):
                    temp_path.unlink()

    async def status(self) -> dict:
        config = store.get()
        if not config:
            return {
                "ready": False,
                "refreshing": False,
                "last_success": None,
                "last_error": None,
                "programme_count": 0,
                "matched_channel_count": 0,
                "age_seconds": None,
                "refresh_interval": settings.epg_refresh_interval,
            }
        key = provider_cache_key(config)
        result = await asyncio.to_thread(epg_store.status, key)
        result["refreshing"] = bool(self._refresh_task and not self._refresh_task.done())
        result["refresh_interval"] = settings.epg_refresh_interval
        return result


epg_service = EpgService()
