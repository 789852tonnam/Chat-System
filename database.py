"""SQLite user storage; the server's application worker owns the connection."""

import sqlite3
from datetime import datetime, timezone
from pathlib import Path


# Open storage and create only the tables implemented by the current milestone.
# SQLite's thread check remains enabled: create, use, and close on one worker.
# A failed initialization closes its connection and is reported by server.start().
def open_database(path: str) -> sqlite3.Connection:
    if path != ":memory:":
        Path(path).parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(path)
    connection.row_factory = sqlite3.Row
    try:
        with connection:
            connection.execute("PRAGMA foreign_keys = ON")
            connection.execute("""
                CREATE TABLE IF NOT EXISTS users (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    username TEXT NOT NULL COLLATE NOCASE UNIQUE
                        CHECK(length(username) BETWEEN 3 AND 32),
                    created_at TEXT NOT NULL
                )
            """)
    except Exception:
        connection.close()
        raise
    return connection


# Return the existing identity for a validated name, or create it atomically.
# NOCASE matches the protocol's ASCII case-insensitive usernames; the first
# spelling is retained. No password or ownership verification is performed.
def get_or_create_user(connection: sqlite3.Connection, username: str) -> dict:
    created_at = datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")
    with connection:
        connection.execute(
            "INSERT INTO users (username, created_at) VALUES (?, ?) "
            "ON CONFLICT(username) DO NOTHING",
            (username, created_at),
        )
        row = connection.execute(
            "SELECT id, username FROM users WHERE username = ?", (username,),
        ).fetchone()
    return dict(row)
