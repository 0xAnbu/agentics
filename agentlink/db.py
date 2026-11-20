"""Thin database layer: one API over SQLite (dev, phone, VPS) and Postgres (Vercel/Neon).

Why not an ORM: the schema is small, and keeping SQL explicit lets the same
statements run on both engines. Conventions every caller must follow:

* Placeholders are always ``?``; they are rewritten to ``%s`` for Postgres.
* Never write a literal ``?`` or ``%`` in SQL text: values go in params.
* Timestamps are integer epoch milliseconds; JSON is stored as TEXT.
"""

from __future__ import annotations

import contextlib
import logging
import os
import sqlite3
import ssl
import threading
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from typing import Any, Protocol
from urllib.parse import parse_qs, unquote, urlparse

log = logging.getLogger("agentlink.db")

Row = dict[str, Any]


class Executor(Protocol):
    def query(self, sql: str, params: Sequence[Any] = ()) -> list[Row]: ...
    def one(self, sql: str, params: Sequence[Any] = ()) -> Row | None: ...
    def execute(self, sql: str, params: Sequence[Any] = ()) -> int: ...


class _Cursorish:
    """Shared helpers over a DB-API cursor factory."""

    dialect: str

    def _cursor(self) -> Any:  # pragma: no cover - overridden
        raise NotImplementedError

    def _sql(self, sql: str) -> str:
        return sql.replace("?", "%s") if self.dialect == "postgres" else sql

    def query(self, sql: str, params: Sequence[Any] = ()) -> list[Row]:
        cur = self._cursor()
        cur.execute(self._sql(sql), tuple(params))
        if cur.description is None:
            return []
        cols = [d[0] for d in cur.description]
        return [dict(zip(cols, r, strict=True)) for r in cur.fetchall()]

    def one(self, sql: str, params: Sequence[Any] = ()) -> Row | None:
        rows = self.query(sql, params)
        return rows[0] if rows else None

    def execute(self, sql: str, params: Sequence[Any] = ()) -> int:
        cur = self._cursor()
        cur.execute(self._sql(sql), tuple(params))
        return int(cur.rowcount or 0)


class _Tx(_Cursorish):
    def __init__(self, conn: Any, dialect: str) -> None:
        self._conn = conn
        self.dialect = dialect

    def _cursor(self) -> Any:
        return self._conn.cursor()


class Database(_Cursorish):
    """Connection manager. Thread-safe; safe to share as a process singleton."""

    def __init__(self, url: str = "", sqlite_path: str = "data/agentlink.db") -> None:
        self.dialect = "postgres" if url.startswith(("postgres://", "postgresql://")) else "sqlite"
        self._url = url
        self._sqlite_path = sqlite_path
        self._lock = threading.RLock()  # SQLite: serialise all access on one connection
        self._local = threading.local()  # Postgres: one connection per thread
        self._sqlite: sqlite3.Connection | None = None

    # -- connections -----------------------------------------------------
    def _sqlite_conn(self) -> sqlite3.Connection:
        if self._sqlite is None:
            if sqlite3.sqlite_version_info < (3, 35, 0):  # RETURNING / UPSERT used throughout
                raise RuntimeError(f"SQLite >= 3.35 required, found {sqlite3.sqlite_version}")
            if self._sqlite_path != ":memory:":
                os.makedirs(os.path.dirname(os.path.abspath(self._sqlite_path)), exist_ok=True)
            conn = sqlite3.connect(self._sqlite_path, check_same_thread=False, isolation_level=None, timeout=10)
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA synchronous=NORMAL")  # durable enough with WAL, far fewer fsyncs
            conn.execute("PRAGMA foreign_keys=ON")
            conn.execute("PRAGMA busy_timeout=10000")
            self._sqlite = conn
        return self._sqlite

    def _pg_connect(self) -> Any:
        import pg8000.dbapi  # imported lazily: not needed for SQLite deployments

        u = urlparse(self._url)
        qs = parse_qs(u.query)
        sslmode = (qs.get("sslmode") or ["prefer"])[0]
        ctx: ssl.SSLContext | None = None
        if sslmode in ("require", "verify-ca", "verify-full"):
            ctx = ssl.create_default_context()
        conn = pg8000.dbapi.connect(
            user=unquote(u.username or "postgres"),
            password=unquote(u.password) if u.password else None,
            host=u.hostname or "localhost",
            port=u.port or 5432,
            database=(u.path or "/postgres").lstrip("/") or "postgres",
            ssl_context=ctx,
            timeout=30,
            application_name="agentlink",
        )
        conn.autocommit = True
        return conn

    def _pg_conn(self) -> Any:
        conn = getattr(self._local, "conn", None)
        if conn is None:
            conn = self._pg_connect()
            self._local.conn = conn
        return conn

    def _pg_reset(self) -> None:
        conn = getattr(self._local, "conn", None)
        self._local.conn = None
        if conn is not None:
            with contextlib.suppress(Exception):  # connection already broken
                conn.close()

    def _cursor(self) -> Any:
        if self.dialect == "sqlite":
            return self._sqlite_conn().cursor()
        return self._pg_conn().cursor()

    # -- public API --------------------------------------------------------
    def query(self, sql: str, params: Sequence[Any] = ()) -> list[Row]:
        if self.dialect == "sqlite":
            with self._lock:
                return super().query(sql, params)
        try:
            return super().query(sql, params)
        except Exception as exc:
            if not _is_connection_error(exc):
                raise
            log.warning("postgres connection lost, reconnecting")
            self._pg_reset()
            return super().query(sql, params)

    def execute(self, sql: str, params: Sequence[Any] = ()) -> int:
        if self.dialect == "sqlite":
            with self._lock:
                return super().execute(sql, params)
        try:
            return super().execute(sql, params)
        except Exception as exc:
            if not _is_connection_error(exc):
                raise
            self._pg_reset()
            return super().execute(sql, params)

    @contextmanager
    def transaction(self) -> Iterator[_Tx]:
        """Atomic unit of work. SQLite takes the write lock up front (IMMEDIATE)."""
        if self.dialect == "sqlite":
            with self._lock:
                conn = self._sqlite_conn()
                conn.execute("BEGIN IMMEDIATE")
                try:
                    yield _Tx(conn, "sqlite")
                except BaseException:
                    conn.execute("ROLLBACK")
                    raise
                conn.execute("COMMIT")
            return
        conn = self._pg_conn()
        cur = conn.cursor()
        try:
            cur.execute("BEGIN")
        except Exception as exc:
            if not _is_connection_error(exc):
                raise
            self._pg_reset()
            conn = self._pg_conn()
            cur = conn.cursor()
            cur.execute("BEGIN")
        try:
            yield _Tx(conn, "postgres")
        except BaseException:
            try:
                cur.execute("ROLLBACK")
            except Exception:
                self._pg_reset()
            raise
        cur.execute("COMMIT")

    def size_bytes(self) -> int:
        if self.dialect == "sqlite":
            if self._sqlite_path == ":memory:":
                row = self.one("SELECT page_count * page_size AS s FROM pragma_page_count, pragma_page_size")
                return int(row["s"]) if row else 0
            total = 0
            for suffix in ("", "-wal"):
                path = self._sqlite_path + suffix
                if os.path.exists(path):
                    total += os.path.getsize(path)
            return total
        row = self.one("SELECT pg_database_size(current_database()) AS s")
        return int(row["s"]) if row else 0

    def close(self) -> None:
        if self._sqlite is not None:
            self._sqlite.close()
            self._sqlite = None
        self._pg_reset()


def _is_connection_error(exc: Exception) -> bool:
    name = type(exc).__name__
    return name in ("InterfaceError", "ConnectionResetError", "BrokenPipeError") or isinstance(
        exc, (ConnectionError, OSError)
    )


# ---------------------------------------------------------------------------
# Schema & migrations
# ---------------------------------------------------------------------------


def _ddl(dialect: str) -> list[str]:
    pk = "INTEGER PRIMARY KEY AUTOINCREMENT" if dialect == "sqlite" else "BIGSERIAL PRIMARY KEY"
    big = "INTEGER" if dialect == "sqlite" else "BIGINT"
    return [
        f"""CREATE TABLE IF NOT EXISTS agents (
            id {pk}, name TEXT NOT NULL UNIQUE, role TEXT NOT NULL,
            description TEXT NOT NULL DEFAULT '', disabled INTEGER NOT NULL DEFAULT 0,
            created_at {big} NOT NULL)""",
        f"""CREATE TABLE IF NOT EXISTS tokens (
            id {pk}, agent_id {big} NOT NULL REFERENCES agents(id),
            token_hash TEXT NOT NULL UNIQUE, label TEXT NOT NULL DEFAULT '',
            created_at {big} NOT NULL, expires_at {big}, revoked_at {big}, last_used_at {big})""",
        f"""CREATE TABLE IF NOT EXISTS channels (
            id {pk}, name TEXT NOT NULL UNIQUE, repo TEXT, description TEXT NOT NULL DEFAULT '',
            created_by {big}, created_at {big} NOT NULL)""",
        f"""CREATE TABLE IF NOT EXISTS members (
            channel_id {big} NOT NULL REFERENCES channels(id),
            agent_id {big} NOT NULL REFERENCES agents(id),
            PRIMARY KEY (channel_id, agent_id))""",
        f"""CREATE TABLE IF NOT EXISTS messages (
            id {pk}, channel_id {big} NOT NULL REFERENCES channels(id),
            author_id {big} NOT NULL REFERENCES agents(id), kind TEXT NOT NULL,
            body TEXT NOT NULL, data TEXT, refs TEXT, tags TEXT,
            thread_id {big}, reply_to {big}, client_id TEXT, created_at {big} NOT NULL)""",
        "CREATE INDEX IF NOT EXISTS ix_messages_channel ON messages(channel_id, id)",
        "CREATE INDEX IF NOT EXISTS ix_messages_thread ON messages(thread_id)",
        "CREATE INDEX IF NOT EXISTS ix_messages_created ON messages(created_at)",
        "CREATE UNIQUE INDEX IF NOT EXISTS ux_messages_client ON messages(author_id, client_id)",
        f"""CREATE TABLE IF NOT EXISTS mentions (
            agent_id {big} NOT NULL, message_id {big} NOT NULL,
            PRIMARY KEY (agent_id, message_id))""",
        f"""CREATE TABLE IF NOT EXISTS tasks (
            id {pk}, channel_id {big} NOT NULL REFERENCES channels(id), title TEXT NOT NULL,
            description TEXT NOT NULL DEFAULT '', status TEXT NOT NULL, priority INTEGER NOT NULL DEFAULT 2,
            assignee_id {big}, created_by {big} NOT NULL, refs TEXT, version INTEGER NOT NULL DEFAULT 1,
            created_at {big} NOT NULL, updated_at {big} NOT NULL)""",
        "CREATE INDEX IF NOT EXISTS ix_tasks_channel ON tasks(channel_id, status)",
        "CREATE INDEX IF NOT EXISTS ix_tasks_assignee ON tasks(assignee_id, status)",
        f"""CREATE TABLE IF NOT EXISTS leases (
            id {pk}, channel_id {big} NOT NULL REFERENCES channels(id), resource TEXT NOT NULL,
            holder_id {big} NOT NULL, note TEXT NOT NULL DEFAULT '', expires_at {big} NOT NULL,
            created_at {big} NOT NULL, UNIQUE (channel_id, resource))""",
        f"""CREATE TABLE IF NOT EXISTS kv (
            channel_id {big} NOT NULL REFERENCES channels(id), key TEXT NOT NULL, value TEXT NOT NULL,
            version INTEGER NOT NULL, updated_by {big} NOT NULL, updated_at {big} NOT NULL,
            PRIMARY KEY (channel_id, key))""",
        f"""CREATE TABLE IF NOT EXISTS presence (
            agent_id {big} PRIMARY KEY, status TEXT NOT NULL, activity TEXT NOT NULL DEFAULT '',
            channel_id {big}, last_seen {big} NOT NULL)""",
        f"""CREATE TABLE IF NOT EXISTS audit (
            id {pk}, actor_id {big}, action TEXT NOT NULL, target TEXT NOT NULL DEFAULT '',
            detail TEXT NOT NULL DEFAULT '', created_at {big} NOT NULL)""",
        f"""CREATE TABLE IF NOT EXISTS ratelimit (
            bucket TEXT PRIMARY KEY, win {big} NOT NULL, count INTEGER NOT NULL)""",
        f"""CREATE TABLE IF NOT EXISTS archives (
            id {pk}, upto_id {big} NOT NULL, count {big} NOT NULL, sha256 TEXT NOT NULL,
            created_by {big}, created_at {big} NOT NULL)""",
    ]


SCHEMA_VERSION = 1


def migrate(db: Database) -> None:
    """Idempotent: safe to call on every cold start, even concurrently."""
    with db.transaction() as tx:
        if db.dialect == "postgres":
            tx.execute("SELECT pg_advisory_xact_lock(4242)")
        tx.execute("CREATE TABLE IF NOT EXISTS schema_version (version INTEGER NOT NULL)")
        row = tx.one("SELECT MAX(version) AS v FROM schema_version")
        current = int(row["v"]) if row and row["v"] is not None else 0
        if current >= SCHEMA_VERSION:
            return
        for stmt in _ddl(db.dialect):
            tx.execute(stmt)
        tx.execute("INSERT INTO schema_version(version) VALUES (?)", (SCHEMA_VERSION,))
    log.info("database migrated to schema v%s (%s)", SCHEMA_VERSION, db.dialect)
