"""
SQLite connection factory and schema initialisation for rent-finder.

Usage:
    from rent_finder.storage.database import get_connection, init_db

    conn = get_connection("data/rent_finder.db")
    init_db(conn)  # idempotent — safe to call on every startup
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

from rent_finder.utils.logging_config import get_logger

log = get_logger(__name__)

# Path to the schema DDL file, relative to this module
_SCHEMA_PATH = Path(__file__).parent / "schema.sql"


def get_connection(db_path: str) -> sqlite3.Connection:
    """
    Open a SQLite connection with production-safe PRAGMAs applied.

    PRAGMAs set:
    - journal_mode=WAL  : Allows concurrent reads during writes
    - foreign_keys=ON   : Enforce referential integrity
    - busy_timeout=5000 : Wait up to 5s if the DB is locked
    - synchronous=NORMAL: Balanced durability vs performance

    Returns a connection with row_factory=sqlite3.Row for dict-like access.
    """
    path = Path(db_path)
    path.parent.mkdir(parents=True, exist_ok=True)

    conn = sqlite3.connect(str(path))
    conn.row_factory = sqlite3.Row

    conn.execute("PRAGMA journal_mode = WAL;")
    conn.execute("PRAGMA foreign_keys = ON;")
    conn.execute("PRAGMA busy_timeout = 5000;")
    conn.execute("PRAGMA synchronous = NORMAL;")

    log.debug("db_connected", path=str(path))
    return conn


def init_db(conn: sqlite3.Connection) -> None:
    """
    Apply the canonical schema DDL to the given connection.

    Uses CREATE TABLE IF NOT EXISTS and CREATE INDEX IF NOT EXISTS throughout,
    so this is safe to call on every pipeline startup — it is a no-op if the
    schema is already up to date.
    """
    schema_sql = _SCHEMA_PATH.read_text(encoding="utf-8")
    conn.executescript(schema_sql)
    conn.commit()

    # Verify tables were created
    tables = {
        row[0]
        for row in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table';"
        ).fetchall()
    }
    expected = {"listings", "run_log", "cookie_health"}
    missing = expected - tables
    if missing:
        raise RuntimeError(
            f"Schema initialisation failed — missing tables: {missing}"
        )

    log.debug("db_schema_initialised", tables=sorted(tables))


def close_connection(conn: sqlite3.Connection) -> None:
    """Close the database connection, committing any open transaction first."""
    try:
        conn.commit()
    except Exception:
        pass
    finally:
        conn.close()
        log.debug("db_connection_closed")
