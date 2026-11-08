"""SQLite users, rooms, memberships and messages, owned by the application worker."""

import sqlite3
import uuid
from datetime import datetime, timezone
from pathlib import Path

from common.protocol import ProtocolError, validate_content, validate_job_id, validate_model_id


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
                    pinned_message_id INTEGER REFERENCES messages(id),
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
                    room_key TEXT NOT NULL REFERENCES rooms(room_key),
                    sender_user_id INTEGER,
                    content TEXT NOT NULL CHECK(length(content) BETWEEN 1 AND 4000),
                    created_at TEXT NOT NULL,
                    FOREIGN KEY(room_key, sender_user_id)
                        REFERENCES room_members(room_key, user_id)
                );
                CREATE INDEX IF NOT EXISTS messages_room_id ON messages(room_key, id);
                CREATE TABLE IF NOT EXISTS message_hearts (
                    message_id INTEGER NOT NULL REFERENCES messages(id),
                    user_id INTEGER NOT NULL REFERENCES users(id),
                    created_at TEXT NOT NULL,
                    PRIMARY KEY(message_id, user_id)
                );
            """)
            # Older files keep their users, rooms and messages during this upgrade.
            columns = {row["name"] for row in connection.execute("PRAGMA table_info(rooms)")}
            if "pinned_message_id" not in columns:
                connection.execute("ALTER TABLE rooms ADD COLUMN pinned_message_id INTEGER REFERENCES messages(id)")
        _upgrade_ai_messages(connection)
        connection.execute("""
            CREATE TABLE IF NOT EXISTS message_ai (
                message_id INTEGER PRIMARY KEY REFERENCES messages(id),
                model TEXT NOT NULL,
                summary_job_id TEXT NOT NULL UNIQUE,
                source_last_message_id INTEGER NOT NULL REFERENCES messages(id)
            )
        """)
    except Exception:
        connection.close()
        raise
    return connection


# SQLite cannot remove NOT NULL with ALTER COLUMN. Rebuild only older messages
# tables, preserving IDs, the AUTOINCREMENT high-water mark and referencing rows.
# Foreign keys are disabled outside the transaction and checked before commit.
def _upgrade_ai_messages(connection: sqlite3.Connection) -> None:
    columns = {row["name"]: row for row in connection.execute("PRAGMA table_info(messages)")}
    if not columns["sender_user_id"]["notnull"]:
        return
    sequence = connection.execute("SELECT seq FROM sqlite_sequence WHERE name = 'messages'").fetchone()
    connection.execute("PRAGMA foreign_keys = OFF")
    try:
        with connection:
            connection.execute("BEGIN IMMEDIATE")
            connection.execute("""
                CREATE TABLE messages_upgrade (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    room_key TEXT NOT NULL REFERENCES rooms(room_key),
                    sender_user_id INTEGER,
                    content TEXT NOT NULL CHECK(length(content) BETWEEN 1 AND 4000),
                    created_at TEXT NOT NULL,
                    FOREIGN KEY(room_key, sender_user_id) REFERENCES room_members(room_key, user_id)
                )
            """)
            connection.execute("INSERT INTO messages_upgrade SELECT id,room_key,sender_user_id,content,created_at FROM messages")
            connection.execute("DROP TABLE messages")
            connection.execute("ALTER TABLE messages_upgrade RENAME TO messages")
            connection.execute("CREATE INDEX messages_room_id ON messages(room_key, id)")
            if sequence is not None:
                changed = connection.execute("UPDATE sqlite_sequence SET seq = MAX(seq, ?) WHERE name = 'messages'", (sequence[0],)).rowcount
                if not changed:
                    connection.execute("INSERT INTO sqlite_sequence(name,seq) VALUES ('messages',?)", (sequence[0],))
            if connection.execute("PRAGMA foreign_key_check").fetchone() is not None:
                raise sqlite3.IntegrityError("Message upgrade failed foreign-key validation")
    finally:
        connection.execute("PRAGMA foreign_keys = ON")


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
              parameters: tuple, *, user_id: int, descending: bool = False, limit: int = 100) -> list[dict]:
    order = "DESC" if descending else "ASC"
    rows = connection.execute(
        "SELECT m.*, u.username, a.model, a.summary_job_id, a.source_last_message_id, "
        "(SELECT COUNT(*) FROM message_hearts h WHERE h.message_id = m.id) AS heart_count, "
        "EXISTS(SELECT 1 FROM message_hearts h WHERE h.message_id = m.id AND h.user_id = ?) AS hearted_by_me "
        "FROM messages m LEFT JOIN users u ON u.id = m.sender_user_id "
        "LEFT JOIN message_ai a ON a.message_id = m.id "
        f"WHERE m.room_key = ? {boundary} ORDER BY m.id {order} LIMIT ?",
        (user_id, room_key, *parameters, limit),
    ).fetchall()
    if descending:
        rows.reverse()
    return [{"id": row["id"], "room_key": row["room_key"], "sender_kind": "human" if row["sender_user_id"] is not None else "ai",
             "sender": {"id": row["sender_user_id"], "username": row["username"]} if row["sender_user_id"] is not None else None,
             "content": row["content"], "created_at": row["created_at"],
             "heart_count": row["heart_count"], "hearted_by_me": bool(row["hearted_by_me"]),
             "ai": {"model": row["model"], "summary_job_id": row["summary_job_id"],
                    "source_last_message_id": row["source_last_message_id"]} if row["model"] is not None else None} for row in rows]


# Copy only the latest fifty human messages in chronological order for an API job.
# The returned data is detached from SQLite and contains no previous AI output.
def summary_source(connection: sqlite3.Connection, room_key: str, user_id: int) -> list[dict]:
    require_room(connection, room_key, user_id)
    return _messages(connection, room_key, "AND m.sender_user_id IS NOT NULL", (), user_id=user_id, descending=True, limit=50)


# Persist AI text/actual model/source boundary together, at most once per job.
# No synthetic human account is created; AI messages have a null sender ID.
def save_summary(connection: sqlite3.Connection, room_key: str, content: str,
                 model: str, job_id: str, source_last_message_id: int) -> int:
    content = validate_content(content)
    validate_model_id(model)
    validate_job_id(job_id)
    source = connection.execute("SELECT room_key, sender_user_id FROM messages WHERE id = ?", (source_last_message_id,)).fetchone()
    if source is None or source["room_key"] != room_key or source["sender_user_id"] is None:
        raise ValueError("Summary source must be a human message in the same room")
    with connection:
        message_id = connection.execute(
            "INSERT INTO messages(room_key,sender_user_id,content,created_at) VALUES (?,NULL,?,?)",
            (room_key, content, _timestamp()),
        ).lastrowid
        connection.execute("INSERT INTO message_ai VALUES (?,?,?,?)", (message_id, model, job_id, source_last_message_id))
    return message_id


# Resolve a message after room authorization, distinguishing missing/wrong-room IDs.
def _require_message(connection: sqlite3.Connection, room_key: str, message_id: int) -> None:
    message = connection.execute("SELECT room_key FROM messages WHERE id = ?", (message_id,)).fetchone()
    if message is None:
        raise ProtocolError("MESSAGE_NOT_FOUND", "Message not found")
    if message["room_key"] != room_key:
        raise ProtocolError("MESSAGE_NOT_IN_ROOM", "Message belongs to another room")


# Include the pinned message even when it is outside the client's loaded range.
# Heart ownership is always calculated for the requesting account.
def _pinned_message(connection: sqlite3.Connection, room: sqlite3.Row, user_id: int) -> dict | None:
    if room["pinned_message_id"] is None:
        return None
    return _messages(connection, room["room_key"], "AND m.id = ?", (room["pinned_message_id"],), user_id=user_id, limit=1)[0]


# Set the desired account/message heart state; repeated requests are no-ops.
# Commit the state and its room revision together, without counting devices twice.
def set_heart(connection: sqlite3.Connection, room_key: str, user_id: int, message_id: int, hearted: bool) -> dict:
    room = require_room(connection, room_key, user_id)
    _require_message(connection, room_key, message_id)
    with connection:
        if hearted:
            changed = connection.execute(
                "INSERT INTO message_hearts VALUES (?, ?, ?) ON CONFLICT(message_id, user_id) DO NOTHING",
                (message_id, user_id, _timestamp()),
            ).rowcount
        else:
            changed = connection.execute(
                "DELETE FROM message_hearts WHERE message_id = ? AND user_id = ?", (message_id, user_id),
            ).rowcount
        if changed:
            connection.execute("UPDATE rooms SET room_revision = room_revision + 1 WHERE room_key = ?", (room_key,))
    message = _messages(connection, room_key, "AND m.id = ?", (message_id,), user_id=user_id, limit=1)[0]
    return {"room_key": room_key, "message_id": message_id, "heart_count": message["heart_count"],
            "hearted_by_me": message["hearted_by_me"], "room_revision": room["room_revision"] + changed}


# Replace or clear the room's single pin after checking membership and ownership.
# The creator has no special role; only a real change advances the revision.
def set_pin(connection: sqlite3.Connection, room_key: str, user_id: int, message_id: int | None) -> dict:
    room = require_room(connection, room_key, user_id)
    if message_id is not None:
        _require_message(connection, room_key, message_id)
    changed = room["pinned_message_id"] != message_id
    if changed:
        with connection:
            connection.execute(
                "UPDATE rooms SET pinned_message_id = ?, room_revision = room_revision + 1 WHERE room_key = ?",
                (message_id, room_key),
            )
    room = connection.execute("SELECT * FROM rooms WHERE room_key = ?", (room_key,)).fetchone()
    return {"room_key": room_key, "pinned_message": _pinned_message(connection, room, user_id),
            "room_revision": room["room_revision"]}


# Read one fixed-range page at the requested revision; do not include newer chat.
# The single worker serializes reads and mutations, so each page and pin agree.
def get_room_snapshot(connection: sqlite3.Connection, user_id: int, payload: dict) -> dict:
    room = require_room(connection, payload["room_key"], user_id)
    if room["room_revision"] != payload["room_revision"]:
        raise ProtocolError("SNAPSHOT_STALE", "Room changed while reloading",
                            details={"room_revision": room["room_revision"]})
    messages = []
    limit = payload.get("limit", 100)
    if payload["from_id"] is not None:
        boundary = "AND m.id >= ? AND m.id <= ?"
        parameters = (payload["from_id"], payload["through_id"])
        if payload["after_id"] is not None:
            boundary += " AND m.id > ?"
            parameters += (payload["after_id"],)
        messages = _messages(connection, room["room_key"], boundary, parameters, user_id=user_id, limit=limit + 1)
    has_more = len(messages) > limit
    messages = messages[:limit]
    return {"room_key": room["room_key"], "messages": messages,
            "pinned_message": _pinned_message(connection, room, user_id), "room_revision": room["room_revision"],
            "next_after_id": messages[-1]["id"] if has_more else None, "has_more": has_more}


# Persist a validated human message before returning its acknowledgement.
# Authorization uses the session user, and the composite FK also enforces membership.
def send_message(connection: sqlite3.Connection, room_key: str, user_id: int, content: str) -> dict:
    require_room(connection, room_key, user_id)
    with connection:
        message_id = connection.execute(
            "INSERT INTO messages (room_key, sender_user_id, content, created_at) VALUES (?, ?, ?, ?)",
            (room_key, user_id, content, _timestamp()),
        ).lastrowid
    return {"message": _messages(connection, room_key, "AND m.id = ?", (message_id,), user_id=user_id, limit=1)[0]}


# Return initial history or bounded older/newer patches for the active room.
# Global IDs may have gaps: query by room and inequalities, never subtract IDs.
# Revision changes require a separate snapshot; patches never advance during reload.
def sync_room(connection: sqlite3.Connection, user_id: int, payload: dict) -> dict:
    room_key = payload["room_key"]
    room = require_room(connection, room_key, user_id)
    oldest, latest = payload["oldest_id"], payload["latest_id"]
    revision = room["room_revision"]
    reload_required = payload["room_revision"] is not None and payload["room_revision"] != revision
    older, newer = [], []
    if not reload_required:
        if latest is None:
            older = _messages(connection, room_key, "", (), user_id=user_id, descending=True, limit=payload["old_count"])
            oldest, latest = (older[0]["id"], older[-1]["id"]) if older else (None, 0)
        else:
            if oldest is not None and payload["old_count"]:
                older = _messages(connection, room_key, "AND m.id < ?", (oldest,),
                                  user_id=user_id, descending=True, limit=payload["old_count"])
            newer = _messages(connection, room_key, "AND m.id > ?", (latest,), user_id=user_id)
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
            "pinned_message": _pinned_message(connection, room, user_id), "effect_revision": 0, "effect": None}
