"""SQLite users, rooms, memberships and messages, owned by the application worker."""

import sqlite3
import uuid
from datetime import datetime, timezone
from pathlib import Path

from common.protocol import ProtocolError


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
            connection.executescript("""
                CREATE TABLE IF NOT EXISTS rooms (
                    room_key TEXT PRIMARY KEY,
                    kind TEXT NOT NULL CHECK(kind IN ('private', 'group')),
                    name TEXT,
                    created_by INTEGER NOT NULL REFERENCES users(id),
                    room_revision INTEGER NOT NULL DEFAULT 0,
                    created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS room_members (
                    room_key TEXT NOT NULL REFERENCES rooms(room_key),
                    user_id INTEGER NOT NULL REFERENCES users(id),
                    joined_at TEXT NOT NULL,
                    PRIMARY KEY(room_key, user_id)
                );
                CREATE TABLE IF NOT EXISTS messages (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    room_key TEXT NOT NULL,
                    sender_user_id INTEGER NOT NULL,
                    content TEXT NOT NULL CHECK(length(content) BETWEEN 1 AND 4000),
                    created_at TEXT NOT NULL,
                    FOREIGN KEY(room_key, sender_user_id)
                        REFERENCES room_members(room_key, user_id)
                );
                CREATE INDEX IF NOT EXISTS messages_room_id ON messages(room_key, id);
            """)
    except Exception:
        connection.close()
        raise
    return connection


# Return the existing identity for a validated name, or create it atomically.
# NOCASE matches the protocol's ASCII case-insensitive usernames; the first
# spelling is retained. No password or ownership verification is performed.
def get_or_create_user(connection: sqlite3.Connection, username: str) -> dict:
    created_at = _timestamp()
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


# Use one UTC millisecond format for all persisted timestamps and wire objects.
def _timestamp() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


# Check room existence before membership so all room actions share error order.
# Knowing any room key never grants read or write access.
def require_room(connection: sqlite3.Connection, room_key: str, user_id: int) -> sqlite3.Row:
    room = connection.execute("SELECT * FROM rooms WHERE room_key = ?", (room_key,)).fetchone()
    if room is None:
        raise ProtocolError("ROOM_NOT_FOUND", "Room not found")
    if connection.execute(
        "SELECT 1 FROM room_members WHERE room_key = ? AND user_id = ?", (room_key, user_id),
    ).fetchone() is None:
        raise ProtocolError("NOT_ROOM_MEMBER", "You are not a member of this room")
    return room


# Build public room metadata/member names without including message history.
# Membership refers to accounts, so multiple devices do not duplicate members.
def _room_document(connection: sqlite3.Connection, room: sqlite3.Row) -> dict:
    members = connection.execute(
        "SELECT u.id, u.username FROM room_members m JOIN users u ON u.id = m.user_id "
        "WHERE m.room_key = ? ORDER BY u.id", (room["room_key"],),
    ).fetchall()
    return {"room_key": room["room_key"], "kind": room["kind"], "name": room["name"],
            "created_by_user_id": room["created_by"], "created_at": room["created_at"],
            "members": [dict(user) for user in members]}


# Create the sorted-ID private room and both memberships in one transaction.
# Returning users can open an existing room even if its other member is offline.
def open_private(connection: sqlite3.Connection, user_id: int, target_id: int) -> dict:
    if user_id == target_id:
        raise ProtocolError("CANNOT_CHAT_SELF", "Cannot open a private room with yourself")
    if connection.execute("SELECT 1 FROM users WHERE id = ?", (target_id,)).fetchone() is None:
        raise ProtocolError("USER_NOT_FOUND", "User not found")
    lower, higher = sorted((user_id, target_id))
    room_key = f"private:{lower}:{higher}"
    timestamp = _timestamp()
    with connection:
        created = connection.execute(
            "INSERT INTO rooms (room_key, kind, created_by, created_at) VALUES (?, 'private', ?, ?) "
            "ON CONFLICT(room_key) DO NOTHING", (room_key, user_id, timestamp),
        ).rowcount == 1
        if created:
            connection.executemany(
                "INSERT INTO room_members (room_key, user_id, joined_at) VALUES (?, ?, ?)",
                [(room_key, member, timestamp) for member in (lower, higher)],
            )
    room = require_room(connection, room_key, user_id)
    return {"room": _room_document(connection, room), "created": created}


# Publish all groups and their account members without revealing room messages.
# Private rooms are excluded; ordering stays stable even for equal timestamps.
def list_groups(connection: sqlite3.Connection) -> list[dict]:
    rooms = connection.execute(
        "SELECT * FROM rooms WHERE kind = 'group' ORDER BY created_at, room_key",
    ).fetchall()
    return [_room_document(connection, room) for room in rooms]


# Create one UUID group and its creator membership in the same transaction.
# The caller validates/normalizes the name; duplicate names are allowed.
def create_group(connection: sqlite3.Connection, user_id: int, name: str) -> dict:
    room_key = f"group:{uuid.uuid4()}"
    timestamp = _timestamp()
    with connection:
        connection.execute(
            "INSERT INTO rooms (room_key, kind, name, created_by, created_at) VALUES (?, 'group', ?, ?, ?)",
            (room_key, name, user_id, timestamp),
        )
        connection.execute(
            "INSERT INTO room_members (room_key, user_id, joined_at) VALUES (?, ?, ?)",
            (room_key, user_id, timestamp),
        )
    return {"room": _room_document(connection, require_room(connection, room_key, user_id))}


# Add only the caller's account to an existing group; repeating the request is safe.
# Check existence/kind before membership, since non-members are allowed to join.
# Membership changes leave the message/pin/heart revision unchanged.
def join_group(connection: sqlite3.Connection, user_id: int, room_key: str) -> dict:
    room = connection.execute("SELECT * FROM rooms WHERE room_key = ?", (room_key,)).fetchone()
    if room is None:
        raise ProtocolError("ROOM_NOT_FOUND", "Room not found")
    if room["kind"] != "group":
        raise ProtocolError("GROUP_REQUIRED", "Only group rooms can be joined")
    with connection:
        joined = connection.execute(
            "INSERT INTO room_members (room_key, user_id, joined_at) VALUES (?, ?, ?) "
            "ON CONFLICT(room_key, user_id) DO NOTHING", (room_key, user_id, _timestamp()),
        ).rowcount == 1
    return {"room": _room_document(connection, room), "joined": joined}


# Read bounded message rows with sender names, using only fixed SQL fragments
# supplied by this module; values always use parameters rather than formatting.
def _messages(connection: sqlite3.Connection, room_key: str, boundary: str,
              parameters: tuple, *, descending: bool = False, limit: int = 100) -> list[dict]:
    order = "DESC" if descending else "ASC"
    rows = connection.execute(
        "SELECT m.*, u.username FROM messages m JOIN users u ON u.id = m.sender_user_id "
        f"WHERE m.room_key = ? {boundary} ORDER BY m.id {order} LIMIT ?",
        (room_key, *parameters, limit),
    ).fetchall()
    if descending:
        rows.reverse()
    return [{"id": row["id"], "room_key": row["room_key"], "sender_kind": "human",
             "sender": {"id": row["sender_user_id"], "username": row["username"]},
             "content": row["content"], "created_at": row["created_at"],
             "heart_count": 0, "hearted_by_me": False, "ai": None} for row in rows]


# Persist a validated human message before returning its acknowledgement.
# Authorization uses the session user, and the composite FK also enforces membership.
def send_message(connection: sqlite3.Connection, room_key: str, user_id: int, content: str) -> dict:
    require_room(connection, room_key, user_id)
    with connection:
        message_id = connection.execute(
            "INSERT INTO messages (room_key, sender_user_id, content, created_at) VALUES (?, ?, ?, ?)",
            (room_key, user_id, content, _timestamp()),
        ).lastrowid
    return {"message": _messages(connection, room_key, "AND m.id = ?", (message_id,), limit=1)[0]}


# Return initial history or bounded older/newer patches for the active room.
# Global IDs may have gaps: query by room and inequalities, never subtract IDs.
# Revision/pin/effect fields retain their empty state until those features exist.
def sync_room(connection: sqlite3.Connection, user_id: int, payload: dict) -> dict:
    room_key = payload["room_key"]
    room = require_room(connection, room_key, user_id)
    oldest, latest = payload["oldest_id"], payload["latest_id"]
    revision = room["room_revision"]
    reload_required = payload["room_revision"] is not None and payload["room_revision"] != revision
    older, newer = [], []
    if not reload_required:
        if latest is None:
            older = _messages(connection, room_key, "", (), descending=True, limit=payload["old_count"])
            oldest, latest = (older[0]["id"], older[-1]["id"]) if older else (None, 0)
        else:
            if oldest is not None and payload["old_count"]:
                older = _messages(connection, room_key, "AND m.id < ?", (oldest,),
                                  descending=True, limit=payload["old_count"])
            newer = _messages(connection, room_key, "AND m.id > ?", (latest,))
            if older:
                oldest = older[0]["id"]
            if newer:
                oldest = oldest if oldest is not None else newer[0]["id"]
                latest = newer[-1]["id"]
    has_older = oldest is not None and connection.execute(
        "SELECT 1 FROM messages WHERE room_key = ? AND id < ? LIMIT 1", (room_key, oldest),
    ).fetchone() is not None
    has_new = latest is not None and connection.execute(
        "SELECT 1 FROM messages WHERE room_key = ? AND id > ? LIMIT 1", (room_key, latest),
    ).fetchone() is not None
    return {"room_key": room_key, "older_messages": older, "new_messages": newer,
            "oldest_id": oldest, "latest_id": latest, "has_more_older": has_older,
            "has_more_new": has_new, "room_revision": revision, "reload_required": reload_required,
            "pinned_message": None, "effect_revision": 0, "effect": None}
