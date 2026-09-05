from __future__ import annotations

import hashlib
import sqlite3
import threading
import time
from dataclasses import dataclass

from .config import settings


@dataclass(frozen=True)
class ProviderConfig:
    base_url: str
    username: str
    password: str
    output: str = "ts"


def provider_cache_key(config: ProviderConfig) -> str:
    raw = f"{config.base_url.rstrip('/')}\0{config.username}\0{config.password}".encode()
    return hashlib.sha256(raw).hexdigest()


class ConfigStore:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._init_db()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(settings.db_path)
        conn.row_factory = sqlite3.Row
        return conn

    def _init_db(self) -> None:
        with self._lock, self._connect() as conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS provider_config (
                    id INTEGER PRIMARY KEY CHECK (id = 1),
                    base_url TEXT NOT NULL,
                    username TEXT NOT NULL,
                    password TEXT NOT NULL,
                    output TEXT NOT NULL DEFAULT 'ts',
                    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
                )
                """
            )
            conn.commit()

    def get(self) -> ProviderConfig | None:
        env = settings.env_xtream
        if env:
            return ProviderConfig(**env)

        with self._lock, self._connect() as conn:
            row = conn.execute(
                "SELECT base_url, username, password, output FROM provider_config WHERE id = 1"
            ).fetchone()
        if not row:
            return None
        return ProviderConfig(
            base_url=row["base_url"],
            username=row["username"],
            password=row["password"],
            output=row["output"],
        )

    def source(self) -> str | None:
        if settings.env_xtream:
            return "environment"
        with self._lock, self._connect() as conn:
            row = conn.execute("SELECT 1 FROM provider_config WHERE id = 1").fetchone()
        return "database" if row else None

    def save(self, config: ProviderConfig) -> None:
        if settings.env_xtream:
            raise RuntimeError("Provider configuration is managed by environment variables")
        with self._lock, self._connect() as conn:
            conn.execute(
                """
                INSERT INTO provider_config (id, base_url, username, password, output, updated_at)
                VALUES (1, ?, ?, ?, ?, CURRENT_TIMESTAMP)
                ON CONFLICT(id) DO UPDATE SET
                    base_url = excluded.base_url,
                    username = excluded.username,
                    password = excluded.password,
                    output = excluded.output,
                    updated_at = CURRENT_TIMESTAMP
                """,
                (config.base_url, config.username, config.password, config.output),
            )
            conn.commit()

    def clear(self) -> None:
        if settings.env_xtream:
            raise RuntimeError("Provider configuration is managed by environment variables")
        with self._lock, self._connect() as conn:
            conn.execute("DELETE FROM provider_config WHERE id = 1")
            conn.commit()


class CatalogStore:
    """Persistent provider catalogue cache.

    Browser/API reads hit SQLite only; network refreshes happen separately in
    CatalogService so page navigation never waits for the Xtream provider.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._init_db()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(settings.db_path)
        conn.row_factory = sqlite3.Row
        return conn

    def _init_db(self) -> None:
        with self._lock, self._connect() as conn:
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS catalog_meta (
                    id INTEGER PRIMARY KEY CHECK (id = 1),
                    provider_key TEXT,
                    refreshed_at REAL,
                    last_error TEXT
                );
                INSERT OR IGNORE INTO catalog_meta (id) VALUES (1);

                CREATE TABLE IF NOT EXISTS catalog_categories (
                    category_id TEXT PRIMARY KEY,
                    category_name TEXT NOT NULL,
                    parent_id TEXT,
                    position INTEGER NOT NULL
                );

                CREATE TABLE IF NOT EXISTS catalog_channels (
                    stream_id INTEGER PRIMARY KEY,
                    name TEXT NOT NULL,
                    category_id TEXT NOT NULL,
                    tv_archive INTEGER NOT NULL DEFAULT 0,
                    epg_channel_id TEXT,
                    position INTEGER NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_catalog_channels_category_position
                    ON catalog_channels(category_id, position);
                CREATE INDEX IF NOT EXISTS idx_catalog_channels_position
                    ON catalog_channels(position);
                CREATE INDEX IF NOT EXISTS idx_catalog_channels_name
                    ON catalog_channels(name COLLATE NOCASE);
                """
            )
            columns = {row[1] for row in conn.execute("PRAGMA table_info(catalog_channels)").fetchall()}
            if "epg_channel_id" not in columns:
                conn.execute("ALTER TABLE catalog_channels ADD COLUMN epg_channel_id TEXT")
                # Existing v0.1.6 rows lack the provider EPG mapping. Mark the
                # catalogue stale once so the background refresher repopulates it.
                conn.execute("UPDATE catalog_meta SET refreshed_at = NULL WHERE id = 1")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_catalog_channels_epg ON catalog_channels(epg_channel_id)")
            conn.commit()

    def clear(self) -> None:
        with self._lock, self._connect() as conn:
            conn.execute("DELETE FROM catalog_categories")
            conn.execute("DELETE FROM catalog_channels")
            conn.execute(
                "UPDATE catalog_meta SET provider_key = NULL, refreshed_at = NULL, last_error = NULL WHERE id = 1"
            )
            conn.commit()

    def replace(self, provider_key: str, categories: list[dict], channels: list[dict]) -> None:
        now = time.time()
        with self._lock, self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            conn.execute("DELETE FROM catalog_categories")
            conn.execute("DELETE FROM catalog_channels")
            conn.executemany(
                "INSERT INTO catalog_categories(category_id, category_name, parent_id, position) VALUES (?, ?, ?, ?)",
                [
                    (
                        str(item.get("category_id", "")),
                        str(item.get("category_name", "Unnamed")),
                        str(item.get("parent_id", "0")),
                        idx,
                    )
                    for idx, item in enumerate(categories)
                ],
            )
            conn.executemany(
                "INSERT INTO catalog_channels(stream_id, name, category_id, tv_archive, epg_channel_id, position) VALUES (?, ?, ?, ?, ?, ?)",
                [
                    (
                        int(item["stream_id"]),
                        str(item.get("name", f"Channel {item['stream_id']}")),
                        str(item.get("category_id", "")),
                        1 if item.get("tv_archive") else 0,
                        str(item.get("epg_channel_id") or "") or None,
                        idx,
                    )
                    for idx, item in enumerate(channels)
                ],
            )
            conn.execute(
                "UPDATE catalog_meta SET provider_key = ?, refreshed_at = ?, last_error = NULL WHERE id = 1",
                (provider_key, now),
            )
            conn.commit()

    def set_error(self, provider_key: str, message: str) -> None:
        with self._lock, self._connect() as conn:
            row = conn.execute("SELECT provider_key FROM catalog_meta WHERE id = 1").fetchone()
            # Preserve a valid warm cache for the same provider on transient refresh errors.
            if row and row["provider_key"] == provider_key:
                conn.execute("UPDATE catalog_meta SET last_error = ? WHERE id = 1", (message[:1000],))
            else:
                conn.execute(
                    "UPDATE catalog_meta SET provider_key = ?, refreshed_at = NULL, last_error = ? WHERE id = 1",
                    (provider_key, message[:1000]),
                )
            conn.commit()

    def _valid_for(self, conn: sqlite3.Connection, provider_key: str) -> bool:
        row = conn.execute("SELECT provider_key, refreshed_at FROM catalog_meta WHERE id = 1").fetchone()
        return bool(row and row["provider_key"] == provider_key and row["refreshed_at"] is not None)

    def categories(self, provider_key: str) -> list[dict]:
        with self._lock, self._connect() as conn:
            if not self._valid_for(conn, provider_key):
                return []
            rows = conn.execute(
                "SELECT category_id, category_name, parent_id FROM catalog_categories ORDER BY position"
            ).fetchall()
        return [dict(row) for row in rows]

    def channels(
        self,
        provider_key: str,
        *,
        category_id: str | None,
        search: str,
        offset: int,
        limit: int,
    ) -> tuple[list[dict], int]:
        with self._lock, self._connect() as conn:
            if not self._valid_for(conn, provider_key):
                return [], 0
            where: list[str] = []
            params: list[object] = []
            if category_id:
                where.append("category_id = ?")
                params.append(str(category_id))
            if search:
                where.append("name LIKE ? COLLATE NOCASE")
                params.append(f"%{search}%")
            where_sql = f" WHERE {' AND '.join(where)}" if where else ""
            total = int(conn.execute(f"SELECT COUNT(*) FROM catalog_channels{where_sql}", params).fetchone()[0])
            rows = conn.execute(
                f"""
                SELECT stream_id, name, category_id, tv_archive, epg_channel_id
                FROM catalog_channels
                {where_sql}
                ORDER BY position
                LIMIT ? OFFSET ?
                """,
                [*params, limit, offset],
            ).fetchall()
        return [
            {
                "stream_id": int(row["stream_id"]),
                "name": row["name"],
                "category_id": row["category_id"],
                "tv_archive": bool(row["tv_archive"]),
                "epg_channel_id": row["epg_channel_id"],
            }
            for row in rows
        ], total

    def epg_channel_ids(self, provider_key: str) -> set[str]:
        with self._lock, self._connect() as conn:
            if not self._valid_for(conn, provider_key):
                return set()
            rows = conn.execute(
                "SELECT DISTINCT epg_channel_id FROM catalog_channels WHERE epg_channel_id IS NOT NULL AND epg_channel_id != ''"
            ).fetchall()
        return {str(row[0]) for row in rows if row[0]}

    def epg_channel_for_stream(self, provider_key: str, stream_id: int) -> str | None:
        with self._lock, self._connect() as conn:
            if not self._valid_for(conn, provider_key):
                return None
            row = conn.execute(
                "SELECT epg_channel_id FROM catalog_channels WHERE stream_id = ?", (int(stream_id),)
            ).fetchone()
        return str(row[0]) if row and row[0] else None

    def status(self, provider_key: str) -> dict:
        now = time.time()
        with self._lock, self._connect() as conn:
            row = conn.execute(
                "SELECT provider_key, refreshed_at, last_error FROM catalog_meta WHERE id = 1"
            ).fetchone()
            valid = bool(row and row["provider_key"] == provider_key and row["refreshed_at"] is not None)
            category_count = int(conn.execute("SELECT COUNT(*) FROM catalog_categories").fetchone()[0]) if valid else 0
            channel_count = int(conn.execute("SELECT COUNT(*) FROM catalog_channels").fetchone()[0]) if valid else 0
        refreshed_at = float(row["refreshed_at"]) if valid else None
        return {
            "ready": valid,
            "refreshing": False,
            "last_success": refreshed_at,
            "last_error": row["last_error"] if row and row["provider_key"] == provider_key else None,
            "category_count": category_count,
            "channel_count": channel_count,
            "age_seconds": max(0.0, now - refreshed_at) if refreshed_at is not None else None,
        }


class EpgStore:
    """Persistent parsed XMLTV cache keyed by the configured provider."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._init_db()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(settings.db_path)
        conn.row_factory = sqlite3.Row
        return conn

    def _init_db(self) -> None:
        with self._lock, self._connect() as conn:
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS epg_meta (
                    id INTEGER PRIMARY KEY CHECK (id = 1),
                    provider_key TEXT,
                    refreshed_at REAL,
                    last_error TEXT,
                    programme_count INTEGER NOT NULL DEFAULT 0,
                    matched_channel_count INTEGER NOT NULL DEFAULT 0
                );
                INSERT OR IGNORE INTO epg_meta (id) VALUES (1);

                CREATE TABLE IF NOT EXISTS epg_programmes (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    epg_channel_id TEXT NOT NULL,
                    start_ts REAL NOT NULL,
                    stop_ts REAL NOT NULL,
                    title TEXT NOT NULL,
                    description TEXT,
                    category TEXT
                );
                CREATE INDEX IF NOT EXISTS idx_epg_channel_time
                    ON epg_programmes(epg_channel_id, start_ts, stop_ts);
                """
            )
            conn.commit()

    def clear(self) -> None:
        with self._lock, self._connect() as conn:
            conn.execute("DELETE FROM epg_programmes")
            conn.execute(
                "UPDATE epg_meta SET provider_key = NULL, refreshed_at = NULL, last_error = NULL, programme_count = 0, matched_channel_count = 0 WHERE id = 1"
            )
            conn.commit()

    def replace(self, provider_key: str, programmes: list[dict]) -> None:
        now = time.time()
        matched = len({item["epg_channel_id"] for item in programmes})
        rows = [
            (
                str(item["epg_channel_id"]),
                float(item["start_ts"]),
                float(item["stop_ts"]),
                str(item.get("title") or "Untitled"),
                str(item.get("description") or ""),
                str(item.get("category") or ""),
            )
            for item in programmes
        ]
        with self._lock, self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            conn.execute("DELETE FROM epg_programmes")
            conn.executemany(
                "INSERT INTO epg_programmes(epg_channel_id, start_ts, stop_ts, title, description, category) VALUES (?, ?, ?, ?, ?, ?)",
                rows,
            )
            conn.execute(
                "UPDATE epg_meta SET provider_key = ?, refreshed_at = ?, last_error = NULL, programme_count = ?, matched_channel_count = ? WHERE id = 1",
                (provider_key, now, len(rows), matched),
            )
            conn.commit()

    def set_error(self, provider_key: str, message: str) -> None:
        with self._lock, self._connect() as conn:
            row = conn.execute("SELECT provider_key FROM epg_meta WHERE id = 1").fetchone()
            if row and row["provider_key"] == provider_key:
                conn.execute("UPDATE epg_meta SET last_error = ? WHERE id = 1", (message[:1000],))
            else:
                conn.execute(
                    "UPDATE epg_meta SET provider_key = ?, refreshed_at = NULL, last_error = ?, programme_count = 0, matched_channel_count = 0 WHERE id = 1",
                    (provider_key, message[:1000]),
                )
            conn.commit()

    def _valid_for(self, conn: sqlite3.Connection, provider_key: str) -> bool:
        row = conn.execute("SELECT provider_key, refreshed_at FROM epg_meta WHERE id = 1").fetchone()
        return bool(row and row["provider_key"] == provider_key and row["refreshed_at"] is not None)

    def schedule(self, provider_key: str, epg_channel_id: str, *, now: float, limit: int = 16) -> list[dict]:
        with self._lock, self._connect() as conn:
            if not self._valid_for(conn, provider_key):
                return []
            rows = conn.execute(
                """
                SELECT start_ts, stop_ts, title, description, category
                FROM epg_programmes
                WHERE epg_channel_id = ? AND stop_ts > ?
                ORDER BY start_ts
                LIMIT ?
                """,
                (epg_channel_id, now, limit),
            ).fetchall()
        return [dict(row) for row in rows]

    def now_for_streams(self, provider_key: str, stream_ids: list[int], *, now: float) -> dict[int, dict]:
        if not stream_ids:
            return {}
        placeholders = ",".join("?" for _ in stream_ids)
        with self._lock, self._connect() as conn:
            if not self._valid_for(conn, provider_key):
                return {}
            rows = conn.execute(
                f"""
                SELECT c.stream_id, p.start_ts, p.stop_ts, p.title
                FROM catalog_channels c
                JOIN epg_programmes p ON p.epg_channel_id = c.epg_channel_id
                WHERE c.stream_id IN ({placeholders})
                  AND p.start_ts <= ? AND p.stop_ts > ?
                ORDER BY p.start_ts DESC
                """,
                [*map(int, stream_ids), now, now],
            ).fetchall()
        result: dict[int, dict] = {}
        for row in rows:
            sid = int(row["stream_id"])
            if sid not in result:
                result[sid] = {"title": row["title"], "start_ts": row["start_ts"], "stop_ts": row["stop_ts"]}
        return result

    def status(self, provider_key: str) -> dict:
        now = time.time()
        with self._lock, self._connect() as conn:
            row = conn.execute(
                "SELECT provider_key, refreshed_at, last_error, programme_count, matched_channel_count FROM epg_meta WHERE id = 1"
            ).fetchone()
            valid = bool(row and row["provider_key"] == provider_key and row["refreshed_at"] is not None)
        refreshed_at = float(row["refreshed_at"]) if valid else None
        return {
            "ready": valid,
            "refreshing": False,
            "last_success": refreshed_at,
            "last_error": row["last_error"] if row and row["provider_key"] == provider_key else None,
            "programme_count": int(row["programme_count"] or 0) if valid else 0,
            "matched_channel_count": int(row["matched_channel_count"] or 0) if valid else 0,
            "age_seconds": max(0.0, now - refreshed_at) if refreshed_at is not None else None,
        }


store = ConfigStore()
catalog_store = CatalogStore()
epg_store = EpgStore()
