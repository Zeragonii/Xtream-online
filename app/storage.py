from __future__ import annotations

import sqlite3
import threading
from dataclasses import dataclass

from .config import settings


@dataclass(frozen=True)
class ProviderConfig:
    base_url: str
    username: str
    password: str
    output: str = "ts"


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


store = ConfigStore()
