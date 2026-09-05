from __future__ import annotations

import hashlib
import logging
import time
from dataclasses import dataclass
from urllib.parse import quote, urlsplit, urlunsplit

import httpx

from .config import settings
from .storage import ProviderConfig

logger = logging.getLogger("xtream-online.xtream")


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


def redact_url(value: str) -> str:
    """Return a log-safe URL without path credentials or query parameters."""
    try:
        parts = urlsplit(value)
        path_parts = [part for part in parts.path.split("/") if part]
        if path_parts:
            tail = path_parts[-1]
            safe_path = f"/.../{tail}" if tail.endswith((".ts", ".m3u8")) else "/..."
        else:
            safe_path = ""
        return urlunsplit((parts.scheme, parts.netloc, safe_path, "", ""))
    except ValueError:
        return "<upstream>"


def _base_from_server_info(server_info: dict) -> str | None:
    raw_url = str(server_info.get("url") or "").strip()
    protocol = str(server_info.get("server_protocol") or "http").strip().lower()
    if protocol not in {"http", "https"}:
        protocol = "http"
    if not raw_url:
        return None

    if "://" not in raw_url:
        raw_url = f"{protocol}://{raw_url}"

    try:
        parts = urlsplit(raw_url)
    except ValueError:
        return None
    if not parts.hostname:
        return None

    scheme = parts.scheme if parts.scheme in {"http", "https"} else protocol
    port_key = "https_port" if scheme == "https" else "port"
    advertised_port = str(server_info.get(port_key) or "").strip()

    netloc = parts.netloc
    if parts.port is None and advertised_port.isdigit():
        port = int(advertised_port)
        default_port = 443 if scheme == "https" else 80
        if port != default_port:
            host = parts.hostname
            if ":" in host and not host.startswith("["):
                host = f"[{host}]"
            netloc = f"{host}:{port}"

    return urlunsplit((scheme, netloc, parts.path.rstrip("/"), "", "")).rstrip("/")


@dataclass(frozen=True)
class LearnedRoute:
    base_url: str
    output: str
    live_prefix: bool


# Process-local caches. They contain no plaintext password material.
_AUTH_CACHE: dict[str, tuple[float, dict]] = {}
_ROUTE_CACHE: dict[str, LearnedRoute] = {}


def _provider_key(config: ProviderConfig) -> str:
    raw = f"{normalize_base_url(config.base_url)}\0{config.username}\0{config.password}".encode()
    return hashlib.sha256(raw).hexdigest()


class XtreamClient:
    def __init__(self, config: ProviderConfig) -> None:
        self.config = ProviderConfig(
            base_url=normalize_base_url(config.base_url),
            username=config.username,
            password=config.password,
            output=config.output if config.output in {"ts", "m3u8"} else "ts",
        )
        self._key = _provider_key(self.config)
        self._candidate_routes: dict[str, LearnedRoute] = {}

    @property
    def api_url(self) -> str:
        return f"{self.config.base_url}/player_api.php"

    def stream_url(
        self,
        stream_id: int,
        *,
        base_url: str | None = None,
        output: str | None = None,
        live_prefix: bool = True,
    ) -> str:
        username = quote(self.config.username, safe="")
        password = quote(self.config.password, safe="")
        extension = output if output in {"ts", "m3u8"} else self.config.output
        base = normalize_base_url(base_url or self.config.base_url)
        prefix = "/live" if live_prefix else ""
        return f"{base}{prefix}/{username}/{password}/{int(stream_id)}.{extension}"

    def _client(self, timeout: int | float | None = None) -> httpx.AsyncClient:
        return httpx.AsyncClient(
            timeout=timeout or settings.xtream_timeout,
            follow_redirects=True,
            headers={"User-Agent": "Xtream-Online/0.1.2"},
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
            async with self._client() as client:
                response = await client.get(self.api_url, params=params)
                response.raise_for_status()
                return response.json()
        except httpx.HTTPStatusError as exc:
            raise XtreamError(f"Xtream request returned HTTP {exc.response.status_code}") from exc
        except httpx.RequestError as exc:
            raise XtreamError(f"Xtream request failed: {exc.__class__.__name__}") from exc
        except ValueError as exc:
            raise XtreamError("Xtream provider returned invalid JSON") from exc

    async def authenticate(self, *, force: bool = False) -> dict:
        cached = _AUTH_CACHE.get(self._key)
        if not force and cached and time.monotonic() - cached[0] < settings.provider_cache_ttl:
            return cached[1]

        data = await self._get()
        if not isinstance(data, dict):
            raise XtreamError("Unexpected authentication response")
        user_info = data.get("user_info") or {}
        auth = user_info.get("auth")
        status = str(user_info.get("status", "")).lower()
        if str(auth) != "1" and status not in {"active", "enabled"}:
            raise XtreamError("Provider rejected the supplied credentials")
        _AUTH_CACHE[self._key] = (time.monotonic(), data)
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

    def _add_route_candidate(self, candidates: list[str], stream_id: int, route: LearnedRoute) -> None:
        try:
            url = self.stream_url(
                stream_id,
                base_url=route.base_url,
                output=route.output,
                live_prefix=route.live_prefix,
            )
        except XtreamError:
            return
        if url not in candidates:
            candidates.append(url)
            self._candidate_routes[url] = route

    def remember_success(self, upstream_url: str) -> None:
        route = self._candidate_routes.get(upstream_url)
        if not route:
            return
        previous = _ROUTE_CACHE.get(self._key)
        _ROUTE_CACHE[self._key] = route
        if route != previous:
            logger.info(
                "Learned provider playback route: %s %s %s",
                redact_url(route.base_url),
                "/live" if route.live_prefix else "root-path",
                route.output,
            )

    async def stream_candidates(self, stream_id: int) -> list[str]:
        """Build fast conventional playback candidates.

        v0.1.2 deliberately keeps get.php/M3U discovery out of this hot path. A
        learned working route is tried first. Provider authentication/server_info is
        cached so normal channel changes do not repeatedly rediscover the provider.
        """
        started = time.monotonic()
        auth: dict = {}
        try:
            auth = await self.authenticate()
        except XtreamError:
            pass

        user_info = auth.get("user_info", {}) if isinstance(auth, dict) else {}
        server_info = auth.get("server_info", {}) if isinstance(auth, dict) else {}

        formats: list[str] = []
        learned = _ROUTE_CACHE.get(self._key)
        for fmt in (
            learned.output if learned else None,
            self.config.output,
            *(
                user_info.get("allowed_output_formats", [])
                if isinstance(user_info, dict) and isinstance(user_info.get("allowed_output_formats", []), list)
                else []
            ),
            "ts",
            "m3u8",
        ):
            fmt = str(fmt or "").lower()
            if fmt in {"ts", "m3u8"} and fmt not in formats:
                formats.append(fmt)

        candidates: list[str] = []
        self._candidate_routes.clear()

        if learned:
            self._add_route_candidate(candidates, stream_id, learned)

        bases: list[str] = []
        advertised = _base_from_server_info(server_info) if isinstance(server_info, dict) else None
        for base in (settings.xtream_stream_base_url, advertised, self.config.base_url):
            if not base:
                continue
            try:
                normalized = normalize_base_url(base)
            except XtreamError:
                continue
            if normalized not in bases:
                bases.append(normalized)

        configured_parts = urlsplit(self.config.base_url)
        if configured_parts.scheme == "https" and configured_parts.hostname:
            host = configured_parts.hostname
            if ":" in host and not host.startswith("["):
                host = f"[{host}]"
            http_fallback = urlunsplit(("http", f"{host}:80", configured_parts.path, "", "")).rstrip("/")
            if http_fallback not in bases:
                bases.append(http_fallback)

        # Conventional Xtream live URLs first. TS is commonly the most reliable
        # transport even when the panel advertises m3u8 output.
        for base in bases:
            for fmt in formats:
                for live_prefix in (True, False):
                    self._add_route_candidate(
                        candidates,
                        stream_id,
                        LearnedRoute(base_url=base, output=fmt, live_prefix=live_prefix),
                    )

        elapsed_ms = (time.monotonic() - started) * 1000
        logger.info(
            "Prepared %d fast playback candidate(s) for stream %s in %.0fms%s",
            len(candidates),
            stream_id,
            elapsed_ms,
            " using learned route first" if learned else "",
        )
        return candidates

    async def playlist_candidates(self, stream_id: int) -> list[str]:
        """Last-resort provider M3U lookup, intentionally not used on normal starts."""
        outputs = [self.config.output, "ts", "m3u8"]
        candidates: list[str] = []
        for output in dict.fromkeys(outputs):
            exact = await self._playlist_candidate(stream_id, output)
            if exact and exact not in candidates:
                candidates.append(exact)
        return candidates

    async def _playlist_candidate(self, stream_id: int, output: str) -> str | None:
        params = {
            "username": self.config.username,
            "password": self.config.password,
            "type": "m3u_plus",
            "output": output,
        }
        try:
            async with self._client(timeout=settings.m3u_timeout) as client:
                response = await client.get(f"{self.config.base_url}/get.php", params=params)
                response.raise_for_status()
                if len(response.content) > settings.m3u_max_bytes:
                    return None
                text = response.text
        except (httpx.HTTPError, UnicodeError):
            return None

        target_names = {f"{int(stream_id)}.ts", f"{int(stream_id)}.m3u8"}
        for raw_line in text.splitlines():
            line = raw_line.strip()
            if not line or line.startswith("#") or not line.startswith(("http://", "https://")):
                continue
            try:
                last_part = urlsplit(line).path.rsplit("/", 1)[-1]
            except ValueError:
                continue
            if last_part in target_names:
                logger.info("Resolved stream %s from provider M3U fallback: %s", stream_id, redact_url(line))
                return line
        return None
