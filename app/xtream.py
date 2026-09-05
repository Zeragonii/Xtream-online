from __future__ import annotations

import logging
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
            if tail.endswith((".ts", ".m3u8")):
                safe_path = f"/.../{tail}"
            else:
                safe_path = "/..."
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

    # Preserve an explicit port in server_info.url. Otherwise add the advertised
    # Xtream port when it is non-default for the selected protocol.
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

    def stream_url(self, stream_id: int, *, base_url: str | None = None, output: str | None = None,
                   live_prefix: bool = True) -> str:
        username = quote(self.config.username, safe="")
        password = quote(self.config.password, safe="")
        extension = output if output in {"ts", "m3u8"} else self.config.output
        base = normalize_base_url(base_url or self.config.base_url)
        prefix = "/live" if live_prefix else ""
        return f"{base}{prefix}/{username}/{password}/{int(stream_id)}.{extension}"

    def _client(self, timeout: int | None = None) -> httpx.AsyncClient:
        return httpx.AsyncClient(
            timeout=timeout or settings.xtream_timeout,
            follow_redirects=True,
            headers={"User-Agent": "Xtream-Online/0.1"},
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

    async def _playlist_candidate(self, stream_id: int, output: str) -> str | None:
        """Ask get.php for the provider's exact URL when M3U export is available."""
        params = {
            "username": self.config.username,
            "password": self.config.password,
            "type": "m3u_plus",
            "output": output,
        }
        try:
            async with self._client(timeout=max(settings.xtream_timeout, 20)) as client:
                response = await client.get(f"{self.config.base_url}/get.php", params=params)
                response.raise_for_status()
                # Xtream M3U files can be large, but are normally modest enough to
                # parse once on playback. Cap the accepted body to avoid pathological responses.
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
                logger.info("Resolved stream %s from provider M3U: %s", stream_id, redact_url(line))
                return line
        return None

    async def stream_candidates(self, stream_id: int) -> list[str]:
        """Return plausible provider stream URLs, safest/most authoritative first.

        Xtream-compatible panels are inconsistent: some advertise a different
        streaming protocol/port from the Player API, and live paths are seen both
        with and without the /live prefix. We use the provider's own M3U URL when
        available, then fall back to server_info and conventional path variants.
        """
        try:
            auth = await self.authenticate()
        except XtreamError:
            auth = {}

        user_info = auth.get("user_info", {}) if isinstance(auth, dict) else {}
        server_info = auth.get("server_info", {}) if isinstance(auth, dict) else {}

        formats: list[str] = [self.config.output]
        allowed = user_info.get("allowed_output_formats", []) if isinstance(user_info, dict) else []
        if isinstance(allowed, list):
            for fmt in allowed:
                fmt = str(fmt).lower()
                if fmt in {"ts", "m3u8"} and fmt not in formats:
                    formats.append(fmt)

        candidates: list[str] = []

        # get.php is the best source of truth because it contains the exact URL
        # the provider expects clients to use.
        for fmt in formats:
            exact = await self._playlist_candidate(stream_id, fmt)
            if exact:
                candidates.append(exact)
                break

        bases: list[str] = []
        advertised = _base_from_server_info(server_info) if isinstance(server_info, dict) else None
        override = settings.xtream_stream_base_url
        for base in (override, advertised, self.config.base_url):
            if not base:
                continue
            try:
                normalized = normalize_base_url(base)
            except XtreamError:
                continue
            if normalized not in bases:
                bases.append(normalized)

        # Some panels expose player_api.php through HTTPS/CDN while their live
        # transport remains plain HTTP on port 80. Include that conservative
        # fallback when the configured API endpoint is HTTPS.
        configured_parts = urlsplit(self.config.base_url)
        if configured_parts.scheme == "https" and configured_parts.hostname:
            host = configured_parts.hostname
            if ":" in host and not host.startswith("["):
                host = f"[{host}]"
            http_fallback = urlunsplit(("http", f"{host}:80", configured_parts.path, "", "")).rstrip("/")
            if http_fallback not in bases:
                bases.append(http_fallback)

        for base in bases:
            for fmt in formats:
                for live_prefix in (True, False):
                    candidate = self.stream_url(
                        stream_id,
                        base_url=base,
                        output=fmt,
                        live_prefix=live_prefix,
                    )
                    if candidate not in candidates:
                        candidates.append(candidate)

        logger.info(
            "Prepared %d playback candidate(s) for stream %s across %d base URL(s)",
            len(candidates), stream_id, len(bases),
        )
        return candidates
