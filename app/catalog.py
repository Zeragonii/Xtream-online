from __future__ import annotations

import asyncio
import logging
import time
from contextlib import suppress

from .config import settings
from .storage import catalog_store, provider_cache_key, store
from .xtream import XtreamClient, XtreamError

logger = logging.getLogger("xtream-online.catalog")


class CatalogService:
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
            await asyncio.sleep(30)
            await self.request_refresh(force=False)

    async def request_refresh(self, *, force: bool) -> bool:
        config = store.get()
        if not config:
            return False
        key = provider_cache_key(config)
        status = await asyncio.to_thread(catalog_store.status, key)
        if not force and status["ready"] and status["age_seconds"] is not None:
            if status["age_seconds"] < settings.catalog_refresh_interval:
                return False

        async with self._lock:
            if self._refresh_task and not self._refresh_task.done():
                return False
            self._refresh_task = asyncio.create_task(self._refresh(config, key))
            return True

    async def _refresh(self, config, key: str) -> None:
        started = time.monotonic()
        logger.info("Refreshing Xtream catalogue in background")
        client = XtreamClient(config)
        try:
            categories = await client.live_categories()
            channels = await client.live_streams(None)
            await asyncio.to_thread(catalog_store.replace, key, categories, channels)
            logger.info(
                "Catalogue refresh complete in %.2fs: %s categories, %s channels",
                time.monotonic() - started,
                len(categories),
                len(channels),
            )
        except (XtreamError, OSError, ValueError) as exc:
            await asyncio.to_thread(catalog_store.set_error, key, str(exc))
            logger.warning("Catalogue refresh failed after %.2fs: %s", time.monotonic() - started, exc)

    async def status(self) -> dict:
        config = store.get()
        if not config:
            return {
                "ready": False,
                "refreshing": False,
                "last_success": None,
                "last_error": None,
                "category_count": 0,
                "channel_count": 0,
                "age_seconds": None,
            }
        key = provider_cache_key(config)
        result = await asyncio.to_thread(catalog_store.status, key)
        result["refreshing"] = bool(self._refresh_task and not self._refresh_task.done())
        return result


catalog_service = CatalogService()
