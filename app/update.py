from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass

import httpx

from .config import settings


@dataclass
class _Cache:
    checked_at: float = 0.0
    value: dict | None = None


class UpdateChecker:
    def __init__(self) -> None:
        self._cache = _Cache()
        self._lock = asyncio.Lock()

    async def status(self, force: bool = False) -> dict:
        if not settings.update_check_enabled:
            return self._base(enabled=False, available=False, message=None)

        now = time.monotonic()
        if not force and self._cache.value and now - self._cache.checked_at < settings.update_check_interval:
            return self._cache.value

        async with self._lock:
            now = time.monotonic()
            if not force and self._cache.value and now - self._cache.checked_at < settings.update_check_interval:
                return self._cache.value
            value = await self._check()
            self._cache = _Cache(now, value)
            return value

    def _base(self, **extra) -> dict:
        return {
            "enabled": settings.update_check_enabled,
            "current_version": settings.app_version,
            "channel": settings.app_channel,
            "commit": settings.app_commit or None,
            "repository": settings.app_repository,
            **extra,
        }

    async def _check(self) -> dict:
        repo = settings.app_repository.strip().strip('/')
        if '/' not in repo:
            return self._base(available=False, message=None, error="Update repository is not configured")

        headers = {
            "Accept": "application/vnd.github+json",
            "User-Agent": "xtream-online-update-checker",
        }
        try:
            async with httpx.AsyncClient(timeout=settings.update_check_timeout, headers=headers, follow_redirects=True) as client:
                if settings.app_channel == "edge" and settings.app_commit:
                    response = await client.get(f"https://api.github.com/repos/{repo}/commits/main")
                    response.raise_for_status()
                    data = response.json()
                    latest_commit = str(data.get("sha") or "")
                    available = bool(latest_commit and not latest_commit.startswith(settings.app_commit) and not settings.app_commit.startswith(latest_commit))
                    return self._base(
                        available=available,
                        latest_version=None,
                        latest_commit=latest_commit or None,
                        url=(data.get("html_url") or f"https://github.com/{repo}/commits/main"),
                        message="New edge container build available" if available else None,
                        error=None,
                    )

                response = await client.get(f"https://api.github.com/repos/{repo}/releases/latest")
                response.raise_for_status()
                data = response.json()
                latest = str(data.get("tag_name") or "").lstrip('v')
                available = _version_tuple(latest) > _version_tuple(settings.app_version)
                return self._base(
                    available=available,
                    latest_version=latest or None,
                    latest_commit=None,
                    url=(data.get("html_url") or f"https://github.com/{repo}/releases/latest"),
                    message=f"Update available: v{latest}" if available and latest else None,
                    error=None,
                )
        except Exception as exc:
            return self._base(available=False, message=None, error=str(exc))


def _version_tuple(value: str) -> tuple[int, int, int]:
    base = (value or "0.0.0").split('-', 1)[0].split('+', 1)[0]
    parts = []
    for token in base.split('.')[:3]:
        try:
            parts.append(int(token))
        except ValueError:
            parts.append(0)
    while len(parts) < 3:
        parts.append(0)
    return tuple(parts[:3])


update_checker = UpdateChecker()
