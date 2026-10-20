"""SQLite storage. Every query shared between tracks lives here; tracks don't write their own SQL."""

from __future__ import annotations

import json
from dataclasses import astuple, dataclass, fields
from pathlib import Path
from typing import Any

import aiosqlite

from bb.timeutil import now

SCHEMA = """
PRAGMA journal_mode = WAL;

CREATE TABLE IF NOT EXISTS guild_settings (
    guild_id INTEGER PRIMARY KEY,
    data     TEXT NOT NULL
);

-- Every supported link anyone posted, recorded at detection time (before BB replies).
CREATE TABLE IF NOT EXISTS posts (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    guild_id      INTEGER NOT NULL,
    channel_id    INTEGER NOT NULL,
    message_id    INTEGER NOT NULL,
    author_id     INTEGER NOT NULL,
    author_name   TEXT    NOT NULL,
    site          TEXT    NOT NULL,
    canonical_key TEXT    NOT NULL,
    created_at    REAL    NOT NULL
);
CREATE INDEX IF NOT EXISTS posts_by_key ON posts (guild_id, canonical_key, created_at);
CREATE INDEX IF NOT EXISTS posts_by_message ON posts (message_id);
CREATE INDEX IF NOT EXISTS posts_by_time ON posts (guild_id, created_at);

-- BB's own repost messages. repost_id is the Discord message id of the repost.
CREATE TABLE IF NOT EXISTS reposts (
    repost_id         INTEGER PRIMARY KEY,
    guild_id          INTEGER NOT NULL,
    channel_id        INTEGER NOT NULL,
    origin_message_id INTEGER NOT NULL,
    origin_author_id  INTEGER NOT NULL,
    site              TEXT    NOT NULL,
    canonical_key     TEXT    NOT NULL,
    original_url      TEXT    NOT NULL,
    spoiler           INTEGER NOT NULL DEFAULT 0,
    fixer_id          TEXT,
    quip              TEXT,
    via_webhook       INTEGER NOT NULL DEFAULT 0,
    webhook_id        INTEGER,
    translated        INTEGER NOT NULL DEFAULT 0,
    status            TEXT    NOT NULL DEFAULT 'pending',  -- pending | ok | uploaded | failed
    created_at        REAL    NOT NULL,
    media             TEXT    NOT NULL DEFAULT ''  -- "photo,video": a mixed tweet, one preview per item
);
CREATE INDEX IF NOT EXISTS reposts_by_origin ON reposts (origin_message_id);
CREATE INDEX IF NOT EXISTS reposts_by_time ON reposts (created_at);

CREATE TABLE IF NOT EXISTS rerun_events (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    guild_id      INTEGER NOT NULL,
    offender_id   INTEGER NOT NULL,
    offender_name TEXT    NOT NULL,
    canonical_key TEXT    NOT NULL,
    created_at    REAL    NOT NULL
);
CREATE INDEX IF NOT EXISTS rerun_events_by_time ON rerun_events (guild_id, created_at);

CREATE TABLE IF NOT EXISTS fixer_health (
    fixer_id             TEXT PRIMARY KEY,
    healthy              INTEGER NOT NULL,
    consecutive_failures INTEGER NOT NULL,
    last_checked         REAL    NOT NULL,
    last_change          REAL    NOT NULL
);
"""


# Bump SCHEMA_VERSION with every schema change: put the change in SCHEMA (new databases) and the
# step that takes an existing database there in MIGRATIONS[new version].
SCHEMA_VERSION = 2
MIGRATIONS: dict[int, str] = {
    2: "ALTER TABLE reposts ADD COLUMN media TEXT NOT NULL DEFAULT '';",
}


@dataclass
class PostRow:
    id: int
    guild_id: int
    channel_id: int
    message_id: int
    author_id: int
    author_name: str
    site: str
    canonical_key: str
    created_at: float


@dataclass
class RepostRow:
    repost_id: int
    guild_id: int
    channel_id: int
    origin_message_id: int
    origin_author_id: int
    site: str
    canonical_key: str
    original_url: str
    spoiler: bool = False
    fixer_id: str | None = None
    quip: str | None = None
    via_webhook: bool = False
    webhook_id: int | None = None
    translated: bool = False
    status: str = "pending"
    created_at: float = 0.0
    media: str = ""  # comma-separated kinds of a tweet that mixes photos and videos, else ""


@dataclass
class HealthRow:
    fixer_id: str
    healthy: bool
    consecutive_failures: int
    last_checked: float
    last_change: float


_REPOST_COLUMNS = [f.name for f in fields(RepostRow)]
_REPOST_UPDATABLE = {"fixer_id", "quip", "translated", "status", "webhook_id", "via_webhook"}
_BOOL_REPOST_FIELDS = {"spoiler", "via_webhook", "translated"}


def _repost_from_row(row: aiosqlite.Row) -> RepostRow:
    data = {name: row[name] for name in _REPOST_COLUMNS}
    for name in _BOOL_REPOST_FIELDS:
        data[name] = bool(data[name])
    return RepostRow(**data)


class Database:
    def __init__(self, path: str) -> None:
        self.path = path
        self._conn: aiosqlite.Connection | None = None

    @property
    def conn(self) -> aiosqlite.Connection:
        if self._conn is None:
            raise RuntimeError("Database.connect() was not called")
        return self._conn

    async def connect(self) -> None:
        if self.path != ":memory:":
            Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        self._conn = await aiosqlite.connect(self.path)
        self._conn.row_factory = aiosqlite.Row
        try:
            await self._migrate()
            await self._conn.executescript(SCHEMA)  # new tables and indexes; existing ones are kept
            await self._check_columns()
            await self._conn.commit()
        except BaseException:
            await self.close()  # its worker thread would otherwise keep the process from exiting
            raise

    async def _migrate(self) -> None:
        """A new database is created at the latest shape by SCHEMA, so it starts at SCHEMA_VERSION. An
        existing one runs each missing step first (each in its own transaction), then SCHEMA."""
        async with self.conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'reposts'"
        ) as cursor:
            existing = await cursor.fetchone() is not None
        if not existing:
            await self.conn.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
            return
        async with self.conn.execute("PRAGMA user_version") as cursor:
            row = await cursor.fetchone()
        stored = int(row[0]) if row else 0
        version = max(stored, 1)  # 0: made before versioning existed, which is version 1
        if version > SCHEMA_VERSION:
            raise RuntimeError(
                f"the database is schema version {version}, newer than this BB ({SCHEMA_VERSION})"
            )
        if stored == 0:
            await self.conn.execute("PRAGMA user_version = 1")
        for target in range(version + 1, SCHEMA_VERSION + 1):
            step = MIGRATIONS.get(target, "")
            await self.conn.executescript(f"BEGIN; {step}\n; PRAGMA user_version = {target}; COMMIT;")

    async def _check_columns(self) -> None:
        """Fail at startup, not later: a repost whose row can't be saved after it went out is a repost
        BB loses track of (no edit or delete sync, no buttons)."""
        async with self.conn.execute("PRAGMA table_info(reposts)") as cursor:
            have = {row[1] for row in await cursor.fetchall()}
        missing = [name for name in _REPOST_COLUMNS if name not in have]
        if missing:
            raise RuntimeError(f"the reposts table is missing columns {missing}; add a migration")

    async def close(self) -> None:
        if self._conn is not None:
            await self._conn.close()
            self._conn = None

    async def _execute(self, sql: str, params: tuple[Any, ...] = ()) -> aiosqlite.Cursor:
        cursor = await self.conn.execute(sql, params)
        await self.conn.commit()
        return cursor

    async def _fetchall(self, sql: str, params: tuple[Any, ...] = ()) -> list[aiosqlite.Row]:
        async with self.conn.execute(sql, params) as cursor:
            return list(await cursor.fetchall())

    async def _fetchone(self, sql: str, params: tuple[Any, ...] = ()) -> aiosqlite.Row | None:
        async with self.conn.execute(sql, params) as cursor:
            return await cursor.fetchone()

    # --- guild settings -------------------------------------------------------------------

    async def get_guild_settings(self, guild_id: int) -> dict[str, Any]:
        """Raw stored overrides (may be empty). Use SettingsStore for merged settings."""
        row = await self._fetchone("SELECT data FROM guild_settings WHERE guild_id = ?", (guild_id,))
        return json.loads(row["data"]) if row else {}

    async def set_guild_settings(self, guild_id: int, data: dict[str, Any]) -> None:
        await self._execute(
            "INSERT INTO guild_settings (guild_id, data) VALUES (?, ?) "
            "ON CONFLICT (guild_id) DO UPDATE SET data = excluded.data",
            (guild_id, json.dumps(data)),
        )

    # --- posts ----------------------------------------------------------------------------

    async def insert_post(
        self,
        *,
        guild_id: int,
        channel_id: int,
        message_id: int,
        author_id: int,
        author_name: str,
        site: str,
        canonical_key: str,
        created_at: float | None = None,
    ) -> int:
        cursor = await self._execute(
            "INSERT INTO posts (guild_id, channel_id, message_id, author_id, author_name, site, "
            "canonical_key, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (guild_id, channel_id, message_id, author_id, author_name, site, canonical_key,
             now() if created_at is None else created_at),
        )
        return int(cursor.lastrowid)

    async def find_prior_posts(
        self,
        *,
        guild_id: int,
        canonical_key: str,
        since: float,
        channel_id: int | None = None,
        exclude_author_id: int | None = None,
        exclude_message_id: int | None = None,
        limit: int = 5,
    ) -> list[PostRow]:
        """Earlier posts of the same key, newest first."""
        sql = "SELECT * FROM posts WHERE guild_id = ? AND canonical_key = ? AND created_at >= ?"
        params: list[Any] = [guild_id, canonical_key, since]
        if channel_id is not None:
            sql += " AND channel_id = ?"
            params.append(channel_id)
        if exclude_author_id is not None:
            sql += " AND author_id != ?"
            params.append(exclude_author_id)
        if exclude_message_id is not None:
            sql += " AND message_id != ?"
            params.append(exclude_message_id)
        sql += " ORDER BY created_at DESC LIMIT ?"
        params.append(limit)
        return [PostRow(**dict(row)) for row in await self._fetchall(sql, tuple(params))]

    async def post_keys_for_message(self, message_id: int) -> set[str]:
        rows = await self._fetchall("SELECT canonical_key FROM posts WHERE message_id = ?", (message_id,))
        return {row["canonical_key"] for row in rows}

    async def delete_post(self, post_id: int) -> None:
        await self._execute("DELETE FROM posts WHERE id = ?", (post_id,))

    async def delete_posts_for_message(self, message_id: int, canonical_key: str | None = None) -> None:
        if canonical_key is None:
            await self._execute("DELETE FROM posts WHERE message_id = ?", (message_id,))
        else:
            await self._execute(
                "DELETE FROM posts WHERE message_id = ? AND canonical_key = ?", (message_id, canonical_key)
            )

    # --- reruns ---------------------------------------------------------------------------

    async def insert_rerun_event(
        self,
        *,
        guild_id: int,
        offender_id: int,
        offender_name: str,
        canonical_key: str,
        created_at: float | None = None,
    ) -> None:
        await self._execute(
            "INSERT INTO rerun_events (guild_id, offender_id, offender_name, canonical_key, created_at) "
            "VALUES (?, ?, ?, ?, ?)",
            (guild_id, offender_id, offender_name, canonical_key,
             now() if created_at is None else created_at),
        )

    # --- reposts --------------------------------------------------------------------------

    async def insert_repost(self, row: RepostRow) -> None:
        if not row.created_at:
            row.created_at = now()
        placeholders = ", ".join("?" for _ in _REPOST_COLUMNS)
        await self._execute(
            f"INSERT INTO reposts ({', '.join(_REPOST_COLUMNS)}) VALUES ({placeholders})",
            astuple(row),
        )

    async def get_repost(self, repost_id: int) -> RepostRow | None:
        row = await self._fetchone("SELECT * FROM reposts WHERE repost_id = ?", (repost_id,))
        return _repost_from_row(row) if row else None

    async def get_reposts_for_origin(self, origin_message_id: int) -> list[RepostRow]:
        rows = await self._fetchall(
            "SELECT * FROM reposts WHERE origin_message_id = ? ORDER BY created_at", (origin_message_id,)
        )
        return [_repost_from_row(row) for row in rows]

    async def update_repost(self, repost_id: int, **changes: Any) -> None:
        """Update fixer_id, quip, translated, status, webhook_id or via_webhook."""
        unknown = set(changes) - _REPOST_UPDATABLE
        if unknown:
            raise ValueError(f"not updatable: {sorted(unknown)}")
        if not changes:
            return
        assignments = ", ".join(f"{name} = ?" for name in changes)
        await self._execute(
            f"UPDATE reposts SET {assignments} WHERE repost_id = ?", (*changes.values(), repost_id)
        )

    async def delete_repost(self, repost_id: int) -> None:
        await self._execute("DELETE FROM reposts WHERE repost_id = ?", (repost_id,))

    async def delete_reposts_for_origin(self, origin_message_id: int) -> None:
        await self._execute("DELETE FROM reposts WHERE origin_message_id = ?", (origin_message_id,))

    # --- stats (status text, roundup) ----------------------------------------------------

    async def count_fixes_since(self, since: float, guild_id: int | None = None) -> int:
        sql = "SELECT COUNT(*) AS n FROM reposts WHERE created_at >= ? AND status IN ('ok', 'uploaded')"
        params: tuple[Any, ...] = (since,)
        if guild_id is not None:
            sql += " AND guild_id = ?"
            params = (since, guild_id)
        row = await self._fetchone(sql, params)
        return int(row["n"]) if row else 0

    async def top_posters(self, guild_id: int, since: float, limit: int = 3) -> list[tuple[int, str, int]]:
        """(author_id, most recent display name, link count), most links first."""
        rows = await self._fetchall(
            "SELECT author_id, author_name, MAX(created_at) AS latest, COUNT(*) AS n FROM posts "
            "WHERE guild_id = ? AND created_at >= ? GROUP BY author_id ORDER BY n DESC, latest DESC LIMIT ?",
            (guild_id, since, limit),
        )
        return [(row["author_id"], row["author_name"], row["n"]) for row in rows]

    async def top_sites(self, guild_id: int, since: float, limit: int = 3) -> list[tuple[str, int]]:
        rows = await self._fetchall(
            "SELECT site, COUNT(*) AS n FROM posts WHERE guild_id = ? AND created_at >= ? "
            "GROUP BY site ORDER BY n DESC LIMIT ?",
            (guild_id, since, limit),
        )
        return [(row["site"], row["n"]) for row in rows]

    async def top_rerun_offenders(
        self, guild_id: int, since: float, limit: int = 1
    ) -> list[tuple[int, str, int]]:
        """(offender_id, most recent display name, rerun count), most reruns first."""
        rows = await self._fetchall(
            "SELECT offender_id, offender_name, MAX(created_at) AS latest, COUNT(*) AS n FROM rerun_events "
            "WHERE guild_id = ? AND created_at >= ? GROUP BY offender_id "
            "ORDER BY n DESC, latest DESC LIMIT ?",
            (guild_id, since, limit),
        )
        return [(row["offender_id"], row["offender_name"], row["n"]) for row in rows]

    # --- fixer health ---------------------------------------------------------------------

    async def get_fixer_health(self) -> dict[str, HealthRow]:
        rows = await self._fetchall("SELECT * FROM fixer_health")
        return {
            row["fixer_id"]: HealthRow(
                fixer_id=row["fixer_id"],
                healthy=bool(row["healthy"]),
                consecutive_failures=row["consecutive_failures"],
                last_checked=row["last_checked"],
                last_change=row["last_change"],
            )
            for row in rows
        }

    async def set_fixer_health(self, row: HealthRow) -> None:
        await self._execute(
            "INSERT INTO fixer_health (fixer_id, healthy, consecutive_failures, last_checked, last_change) "
            "VALUES (?, ?, ?, ?, ?) ON CONFLICT (fixer_id) DO UPDATE SET healthy = excluded.healthy, "
            "consecutive_failures = excluded.consecutive_failures, last_checked = excluded.last_checked, "
            "last_change = excluded.last_change",
            (row.fixer_id, int(row.healthy), row.consecutive_failures, row.last_checked, row.last_change),
        )
