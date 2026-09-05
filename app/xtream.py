from __future__ import annotations

from urllib.parse import quote, urlsplit, urlunsplit

import httpx

from .config import settings
from .storage import ProviderConfig


class XtreamError(RuntimeError):
    pass


def normalize_base_url(value: str) -> str:
    value = value.strip().rstrip("/")
    if not value:
        raise XtreamError("Server URL is required")
    if "://" not in value:
        value = f"http://{value}"

    parts = urlsplit(value)
    if parts.scheme not in {"http", "https"} or not parts.netloc:
        raise XtreamError("Server URL must be a valid http:// or https:// URL")

    path = parts.path.rstrip("/")
    if path.endswith("/player_api.php"):
        path = path[: -len("/player_api.php")]
    elif path == "/player_api.php":
        path = ""

    return urlunsplit((parts.scheme, parts.netloc, path, "", "")).rstrip("/")


class XtreamClient:
    def __init__(self, config: ProviderConfig) -> None:
        self.config = ProviderConfig(
            base_url=normalize_base_url(config.base_url),
            username=config.username,
            password=config.password,
            output=config.output if config.output in {"ts", "m3u8"} else "ts",
        )

    @property
    def api_url(self) -> str:
        return f"{self.config.base_url}/player_api.php"

    def stream_url(self, stream_id: int) -> str:
        username = quote(self.config.username, safe="")
        password = quote(self.config.password, safe="")
        return (
            f"{self.config.base_url}/live/{username}/{password}/"
            f"{int(stream_id)}.{self.config.output}"
        )

    async def _get(self, action: str | None = None, **extra: str | int) -> object:
        params: dict[str, str | int] = {
            "username": self.config.username,
            "password": self.config.password,
        }
        if action:
            params["action"] = action
        params.update(extra)

        try:
            async with httpx.AsyncClient(
                timeout=settings.xtream_timeout,
                follow_redirects=True,
                headers={"User-Agent": "Xtream-Web/0.1"},
            ) as client:
                response = await client.get(self.api_url, params=params)
                response.raise_for_status()
                return response.json()
        except (httpx.HTTPError, ValueError) as exc:
            raise XtreamError(f"Xtream request failed: {exc}") from exc

    async def authenticate(self) -> dict:
        data = await self._get()
        if not isinstance(data, dict):
            raise XtreamError("Unexpected authentication response")
        user_info = data.get("user_info") or {}
        auth = user_info.get("auth")
        status = str(user_info.get("status", "")).lower()
        if str(auth) != "1" and status not in {"active", "enabled"}:
            raise XtreamError("Provider rejected the supplied credentials")
        return data

    async def live_categories(self) -> list[dict]:
        data = await self._get("get_live_categories")
        if not isinstance(data, list):
            raise XtreamError("Unexpected live categories response")
        return [
            {
                "category_id": str(item.get("category_id", "")),
                "category_name": str(item.get("category_name", "Unnamed")),
                "parent_id": item.get("parent_id", 0),
            }
            for item in data
            if isinstance(item, dict)
        ]

    async def live_streams(self, category_id: str | None = None) -> list[dict]:
        extra = {"category_id": category_id} if category_id else {}
        data = await self._get("get_live_streams", **extra)
        if not isinstance(data, list):
            raise XtreamError("Unexpected live streams response")

        streams: list[dict] = []
        for item in data:
            if not isinstance(item, dict) or item.get("stream_id") is None:
                continue
            try:
                stream_id = int(item["stream_id"])
            except (TypeError, ValueError):
                continue
            streams.append(
                {
                    "stream_id": stream_id,
                    "name": str(item.get("name", f"Channel {stream_id}")),
                    "category_id": str(item.get("category_id", "")),
                    "tv_archive": bool(int(item.get("tv_archive", 0) or 0)),
                }
            )
        return streams
