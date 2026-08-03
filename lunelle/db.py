"""SQLite access layer: connection factory, migration runner, small query helpers.

Design notes:
- WAL journal mode so the API thread and worker threads can read while one writes.
- Connections run in autocommit; multi-statement units use explicit
  BEGIN IMMEDIATE / COMMIT via the `transaction` context manager, which takes
  the SQLite write lock up front and avoids lock-upgrade deadlocks.
- Numbered .sql files in lunelle/migrations are applied in order, each inside
  one transaction (including its schema_migrations record). Adding a migration
  file is the only supported way to change the schema.
"""

from __future__ import annotations

import logging
import re
import sqlite3
import threading
from contextlib import contextmanager
from datetime import UTC, datetime
from importlib import resources
from pathlib import Path

logger = logging.getLogger(__name__)

_MIGRATION_RE = re.compile(r"^(\d{4})_[a-z0-9_]+\.sql$")


def utcnow() -> str:
    """Canonical timestamp format used across the DB (UTC, second precision)."""
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def connect(db_path: Path) -> sqlite3.Connection:
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db_path, timeout=30, check_same_thread=False, isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("PRAGMA busy_timeout=30000")
    return conn


@contextmanager
def transaction(conn: sqlite3.Connection):
    """Explicit write transaction; rolls back on any exception."""
    conn.execute("BEGIN IMMEDIATE")
    try:
        yield conn
    except BaseException:
        conn.execute("ROLLBACK")
        raise
    else:
        conn.execute("COMMIT")


def _migration_files() -> list[tuple[int, str, str]]:
    """Return sorted (number, name, sql) for all bundled migrations."""
    out: list[tuple[int, str, str]] = []
    package_dir = resources.files("lunelle").joinpath("migrations")
    for entry in package_dir.iterdir():
        m = _MIGRATION_RE.match(entry.name)
        if m:
            out.append((int(m.group(1)), entry.name, entry.read_text(encoding="utf-8")))
    out.sort()
    if not out:
        raise RuntimeError("No migration files found in lunelle/migrations")
    numbers = [n for n, _, _ in out]
    if len(set(numbers)) != len(numbers):
        raise RuntimeError(f"Duplicate migration numbers: {numbers}")
    return out


def migrate(conn: sqlite3.Connection, *, target: int | None = None) -> list[str]:
    """Apply pending migrations; returns names applied. Safe to call repeatedly.

    `target` stops after that version, which lets scripts/check_migrations.py
    walk the upgrade one step at a time with data present at each — the failure
    mode a from-scratch test database can never reproduce.
    """
    conn.execute(
        "CREATE TABLE IF NOT EXISTS schema_migrations ("
        " number INTEGER PRIMARY KEY, name TEXT NOT NULL, applied_at TEXT NOT NULL)"
    )
    applied = {row["number"] for row in conn.execute("SELECT number FROM schema_migrations")}
    done: list[str] = []
    for number, name, sql in _migration_files():
        if number in applied:
            continue
        if target is not None and number > target:
            break
        # Table rebuilds (CHECK/column changes) need the SQLite documented
        # procedure: disable FK enforcement OUTSIDE the transaction, rebuild,
        # then prove integrity with foreign_key_check before committing.
        # (defer_foreign_keys cannot survive a parent-table DROP+recreate.)
        fk_off = "lunelle:foreign_keys=off" in sql
        try:
            if fk_off:
                conn.execute("PRAGMA foreign_keys=OFF")
            try:
                with transaction(conn):
                    raced = conn.execute(
                        "SELECT 1 FROM schema_migrations WHERE number = ?", (number,)
                    ).fetchone()
                    if raced is not None:  # another process applied it first
                        continue
                    for statement in _split_statements(sql):
                        conn.execute(statement)
                    if fk_off:
                        violations = conn.execute("PRAGMA foreign_key_check").fetchall()
                        if violations:
                            sample = [tuple(v) for v in violations[:5]]
                            raise sqlite3.IntegrityError(
                                f"foreign_key_check found {len(violations)} violation(s): {sample}"
                            )
                    conn.execute(
                        "INSERT INTO schema_migrations (number, name, applied_at) VALUES (?, ?, ?)",
                        (number, name, utcnow()),
                    )
            finally:
                if fk_off:
                    conn.execute("PRAGMA foreign_keys=ON")
        except sqlite3.Error as exc:
            raise RuntimeError(f"Migration {name} failed: {exc}") from exc
        done.append(name)
        logger.info("applied migration %s", name)
    return done


def _split_statements(sql: str) -> list[str]:
    """Split a migration file into executable statements.

    Migrations use plain DDL/DML separated by semicolons at line ends; string
    literals containing ';' are not supported by design (keep migrations simple).
    """
    statements: list[str] = []
    buffer: list[str] = []
    for line in sql.splitlines():
        stripped = line.strip()
        if stripped.startswith("--") or not stripped:
            continue
        buffer.append(line)
        if stripped.endswith(";"):
            statements.append("\n".join(buffer))
            buffer = []
    if buffer:
        statements.append("\n".join(buffer))
    return statements


def db_healthy(conn: sqlite3.Connection) -> bool:
    try:
        conn.execute("SELECT 1").fetchone()
        return True
    except sqlite3.Error:
        return False


def backup(conn: sqlite3.Connection, dest: Path) -> Path:
    """Consistent online backup using SQLite's backup API."""
    dest.parent.mkdir(parents=True, exist_ok=True)
    target = sqlite3.connect(dest)
    try:
        conn.backup(target)
        target.commit()
    finally:
        target.close()
    return dest


class Database:
    """Thread-safe wrapper handing out one connection per thread."""

    def __init__(self, db_path: Path):
        self.db_path = db_path
        self._local = threading.local()
        self._connections: list[sqlite3.Connection] = []
        self._lock = threading.Lock()

    def conn(self) -> sqlite3.Connection:
        conn = getattr(self._local, "conn", None)
        if conn is None:
            conn = connect(self.db_path)
            self._local.conn = conn
            with self._lock:
                self._connections.append(conn)
        return conn

    def close_all(self) -> None:
        with self._lock:
            for conn in self._connections:
                try:
                    conn.close()
                except sqlite3.Error:  # pragma: no cover - best effort shutdown
                    pass
            self._connections.clear()
        self._local = threading.local()
