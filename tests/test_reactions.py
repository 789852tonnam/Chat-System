"""Heart/pin authorization, transactions, migration and revision snapshots."""

import concurrent.futures
import contextlib
import sqlite3
import tempfile
import unittest
from pathlib import Path

import database
from server import ChatServer, ServerConfig
from test_chat import ChatPeer, initial_sync, next_sync


def snapshot_request(data, *, revision=None, after=None, limit=100):
    return {"room_key": data["room_key"], "from_id": data["oldest_id"],
            "through_id": data["latest_id"] if data["oldest_id"] is not None else None,
            "after_id": after, "room_revision": data["room_revision"] if revision is None else revision,
            "limit": limit}


class ReactionTests(unittest.TestCase):
    def setUp(self):
        self.resources = contextlib.ExitStack()
        self.addCleanup(self.resources.close)

    def server(self, path=":memory:"):
        server = ChatServer(ServerConfig(host="127.0.0.1", port=0, database_path=path))
        server.start()
        self.addCleanup(server.stop)
        return server

    def peer(self, server, name=None):
        return self.resources.enter_context(ChatPeer(server.address, name))

    def room(self, alice, bob):
        return alice.ok("open_private", {"target_user_id": bob.user["id"]})["room"]["room_key"]

    def send(self, peer, key, content="Hello"):
        return peer.ok("send_message", {"room_key": key, "content": content})["message"]

    def heart(self, peer, key, message_id, hearted=True):
        return peer.ok("set_heart", {"room_key": key, "message_id": message_id, "hearted": hearted})

    def pin(self, peer, key, message_id):
        return peer.ok("set_pin", {"room_key": key, "message_id": message_id})

    def test_hearts_are_per_account_idempotent_and_viewer_specific(self):
        server = self.server()
        alice, same_alice, bob = [self.peer(server, name) for name in ("Alice", "ALICE", "Bob")]
        key = self.room(alice, bob)
        message_id = self.send(alice, key)["id"]
        self.assertEqual(self.heart(alice, key, message_id)["room_revision"], 1)
        self.assertEqual(self.heart(same_alice, key, message_id)["room_revision"], 1)
        view = bob.ok("sync_room", initial_sync(key))["older_messages"][0]
        self.assertEqual((view["heart_count"], view["hearted_by_me"]), (1, False))
        self.assertEqual(self.heart(bob, key, message_id)["heart_count"], 2)
        removed = self.heart(alice, key, message_id, False)
        self.assertEqual((removed["heart_count"], removed["hearted_by_me"], removed["room_revision"]), (1, False, 3))
        self.assertEqual(self.heart(same_alice, key, message_id, False)["room_revision"], 3)
        view = bob.ok("sync_room", initial_sync(key))["older_messages"][0]
        self.assertEqual((view["heart_count"], view["hearted_by_me"]), (1, True))
        self.assertEqual(self.heart(bob, key, message_id, False)["room_revision"], 4)

    def test_every_member_can_replace_or_clear_the_single_pin(self):
        server = self.server()
        alice, bob = self.peer(server, "Alice"), self.peer(server, "Bob")
        key = self.room(alice, bob)
        first, second = self.send(alice, key, "First"), self.send(bob, key, "Second")
        self.assertEqual(self.pin(alice, key, first["id"])["room_revision"], 1)
        self.assertEqual(self.pin(bob, key, first["id"])["room_revision"], 1)
        replaced = self.pin(bob, key, second["id"])
        self.assertEqual((replaced["pinned_message"]["id"], replaced["room_revision"]), (second["id"], 2))
        self.assertIsNone(self.pin(alice, key, None)["pinned_message"])
        self.assertEqual(self.pin(bob, key, None)["room_revision"], 3)
        self.assertEqual(len(alice.ok("sync_room", initial_sync(key))["older_messages"]), 2)

    def test_nonmembers_and_guests_cannot_mutate_or_snapshot(self):
        server = self.server()
        alice, bob, carol, guest = [self.peer(server, name) for name in ("Alice", "Bob", "Carol", None)]
        key = self.room(alice, bob)
        message = self.send(alice, key)
        history = alice.ok("sync_room", initial_sync(key))
        for action, payload in [("set_heart", {"room_key": key, "message_id": message["id"], "hearted": True}),
                                ("set_pin", {"room_key": key, "message_id": None}),
                                ("get_room_snapshot", snapshot_request(history))]:
            self.assertEqual(carol.call(action, payload)["error"]["code"], "NOT_ROOM_MEMBER")
            self.assertEqual(guest.call(action, payload)["error"]["code"], "UNAUTHENTICATED")
        self.assertEqual(alice.ok("sync_room", initial_sync(key))["room_revision"], 0)

    def test_missing_or_wrong_room_messages_never_change_state(self):
        server = self.server()
        alice, bob, carol = [self.peer(server, name) for name in ("Alice", "Bob", "Carol")]
        key, other = self.room(alice, bob), self.room(alice, carol)
        foreign = self.send(alice, other)["id"]
        for action in ("set_heart", "set_pin"):
            for message_id, code in [(foreign, "MESSAGE_NOT_IN_ROOM"), (99999, "MESSAGE_NOT_FOUND")]:
                payload = {"room_key": key, "message_id": message_id}
                if action == "set_heart":
                    payload["hearted"] = True
                self.assertEqual(alice.call(action, payload)["error"]["code"], code)
        self.assertEqual(alice.ok("sync_room", initial_sync(key))["room_revision"], 0)

    def test_pin_outside_latest_twenty_is_returned_with_viewer_heart_state(self):
        server = self.server()
        alice, bob = self.peer(server, "Alice"), self.peer(server, "Bob")
        key = self.room(alice, bob)
        first = self.send(alice, key)["id"]
        for index in range(30):
            self.send(bob, key, f"Later {index}")
        self.heart(alice, key, first)
        self.pin(bob, key, first)
        for peer, mine in ((alice, True), (bob, False)):
            data = peer.ok("sync_room", initial_sync(key))
            self.assertNotIn(first, [message["id"] for message in data["older_messages"]])
            self.assertEqual(data["pinned_message"]["id"], first)
            self.assertEqual(data["pinned_message"]["hearted_by_me"], mine)

    def test_revision_mismatch_keeps_cursors_and_snapshot_pages_keep_fixed_range(self):
        server = self.server()
        alice, bob = self.peer(server, "Alice"), self.peer(server, "Bob")
        key = self.room(alice, bob)
        ids = [self.send(alice, key, f"Message {i}")["id"] for i in range(205)]
        request = {**initial_sync(key), "old_count": 100}
        loaded = alice.ok("sync_room", request)
        for _ in range(2):
            loaded = alice.ok("sync_room", next_sync(loaded, 100))
        self.heart(bob, key, ids[0])
        self.pin(bob, key, ids[-1])
        mismatch = alice.ok("sync_room", next_sync(loaded))
        self.assertTrue(mismatch["reload_required"])
        self.assertEqual((mismatch["oldest_id"], mismatch["latest_id"]), (ids[0], ids[-1]))
        self.assertEqual(mismatch["older_messages"] + mismatch["new_messages"], [])
        collected, after = [], None
        for expected in (100, 100, 5):
            page = alice.ok("get_room_snapshot", snapshot_request(loaded, revision=2, after=after))
            self.assertEqual(len(page["messages"]), expected)
            self.assertEqual(page["room_revision"], 2)
            self.assertEqual(page["pinned_message"]["id"], ids[-1])
            collected.extend(page["messages"])
            after = page["next_after_id"]
            if len(collected) == 100:
                new_id = self.send(bob, key, "Arrived during reload")["id"]
        self.assertIsNone(after)
        self.assertEqual([message["id"] for message in collected], ids)
        self.assertEqual((collected[0]["heart_count"], collected[0]["hearted_by_me"]), (1, False))
        forward = alice.ok("sync_room", {**next_sync(loaded), "room_revision": 2})
        self.assertEqual([message["id"] for message in forward["new_messages"]], [new_id])

    def test_stale_snapshot_reports_current_revision_and_null_range_still_returns_pin(self):
        server = self.server()
        alice, bob = self.peer(server, "Alice"), self.peer(server, "Bob")
        key = self.room(alice, bob)
        empty = alice.ok("sync_room", initial_sync(key))
        message = self.send(bob, key)
        history = alice.ok("sync_room", next_sync(empty))
        self.pin(bob, key, message["id"])
        error = alice.call("get_room_snapshot", snapshot_request(history))["error"]
        self.assertEqual(error, {"code": "SNAPSHOT_STALE", "message": "Room changed while reloading", "details": {"room_revision": 1}})
        snapshot = alice.ok("get_room_snapshot", snapshot_request(empty, revision=1))
        self.assertEqual(snapshot["messages"], [])
        self.assertIsNone(snapshot["next_after_id"])
        self.assertFalse(snapshot["has_more"])
        self.assertEqual(snapshot["pinned_message"]["id"], message["id"])

    def test_invalid_mutation_and_snapshot_fields_are_rejected(self):
        server = self.server()
        alice, bob = self.peer(server, "Alice"), self.peer(server, "Bob")
        key = self.room(alice, bob)
        message_id = self.send(alice, key)["id"]
        heart = {"room_key": key, "message_id": message_id, "hearted": True}
        for action, payload in [("set_heart", {**heart, "hearted": 1}), ("set_heart", {**heart, "message_id": None}),
                                ("set_heart", {**heart, "user_id": bob.user["id"]}),
                                ("set_pin", {"room_key": key, "message_id": True}), ("set_pin", {"room_key": key})]:
            self.assertEqual(alice.call(action, payload)["error"]["code"], "INVALID_PAYLOAD")
        request = {"room_key": key, "from_id": 1, "through_id": 5, "after_id": None, "room_revision": 0}
        for payload in [{**request, "from_id": None}, {**request, "through_id": 0}, {**request, "from_id": 6},
                        {**request, "after_id": 6}, {**request, "room_revision": True},
                        {**request, "limit": 0}, {**request, "limit": 101}, {**request, "limit": True},
                        {**request, "extra": 1}, {key: value for key, value in request.items() if key != "after_id"}]:
            self.assertEqual(alice.call("get_room_snapshot", payload)["error"]["code"], "INVALID_PAYLOAD")

    def test_concurrent_same_account_hearts_increment_revision_only_once(self):
        server = self.server()
        alice, bob = self.peer(server, "Alice"), self.peer(server, "Bob")
        key = self.room(alice, bob)
        message_id = self.send(alice, key)["id"]
        def heart(_):
            with ChatPeer(server.address, "Bob") as peer:
                return self.heart(peer, key, message_id)
        with concurrent.futures.ThreadPoolExecutor(max_workers=5) as pool:
            results = list(pool.map(heart, range(5)))
        self.assertEqual({(item["heart_count"], item["room_revision"]) for item in results}, {(1, 1)})

    def test_revision_failure_rolls_back_the_heart_and_pin(self):
        with contextlib.closing(database.open_database(":memory:")) as connection:
            alice = database.get_or_create_user(connection, "Alice")["id"]
            bob = database.get_or_create_user(connection, "Bob")["id"]
            key = database.open_private(connection, alice, bob)["room"]["room_key"]
            message_id = database.send_message(connection, key, alice, "Atomic")["message"]["id"]
            connection.execute("CREATE TRIGGER fail_revision BEFORE UPDATE OF room_revision ON rooms BEGIN SELECT RAISE(ABORT, 'simulated'); END")
            for mutate in [lambda: database.set_heart(connection, key, alice, message_id, True),
                           lambda: database.set_pin(connection, key, alice, message_id)]:
                with self.assertRaises(sqlite3.IntegrityError):
                    mutate()
                self.assertEqual(connection.execute("SELECT COUNT(*) FROM message_hearts").fetchone()[0], 0)
                row = connection.execute("SELECT room_revision, pinned_message_id FROM rooms").fetchone()
                self.assertEqual(tuple(row), (0, None))

    def test_existing_database_upgrade_and_reactions_survive_restart(self):
        directory = self.resources.enter_context(tempfile.TemporaryDirectory())
        path = str(Path(directory) / "legacy.sqlite3")
        with contextlib.closing(sqlite3.connect(path)) as connection:
            connection.executescript("""
                CREATE TABLE users(id INTEGER PRIMARY KEY AUTOINCREMENT, username TEXT UNIQUE COLLATE NOCASE, created_at TEXT);
                CREATE TABLE rooms(room_key TEXT PRIMARY KEY, kind TEXT CHECK(kind IN ('private','group')), name TEXT, created_by INTEGER REFERENCES users(id), room_revision INTEGER DEFAULT 0, created_at TEXT);
                CREATE TABLE room_members(room_key TEXT REFERENCES rooms(room_key), user_id INTEGER REFERENCES users(id), joined_at TEXT, PRIMARY KEY(room_key,user_id));
                CREATE TABLE messages(id INTEGER PRIMARY KEY AUTOINCREMENT, room_key TEXT, sender_user_id INTEGER, content TEXT CHECK(length(content) BETWEEN 1 AND 4000), created_at TEXT, FOREIGN KEY(room_key,sender_user_id) REFERENCES room_members(room_key,user_id));
                INSERT INTO users VALUES(1,'Alice','old'),(2,'Bob','old');
                INSERT INTO rooms VALUES('private:1:2','private',NULL,1,0,'old');
                INSERT INTO room_members VALUES('private:1:2',1,'old'),('private:1:2',2,'old');
                INSERT INTO messages VALUES(7,'private:1:2',1,'Preserved','old');
            """)
        server = self.server(path)
        alice = self.peer(server, "Alice")
        key = "private:1:2"
        self.assertEqual(alice.ok("sync_room", initial_sync(key))["older_messages"][0]["id"], 7)
        self.heart(alice, key, 7)
        self.pin(alice, key, 7)
        server.stop()
        restarted = self.server(path)
        returning = self.peer(restarted, "alice")
        history = returning.ok("sync_room", initial_sync(key))
        self.assertEqual(history["room_revision"], 2)
        self.assertEqual(history["pinned_message"]["content"], "Preserved")
        self.assertTrue(history["older_messages"][0]["hearted_by_me"])
        self.assertEqual(history["older_messages"][0]["heart_count"], 1)
        restarted.stop()
        with contextlib.closing(sqlite3.connect(path)) as connection:
            self.assertEqual(connection.execute("PRAGMA foreign_key_check").fetchall(), [])
            self.assertEqual([row[1] for row in connection.execute("PRAGMA table_info(rooms)")].count("pinned_message_id"), 1)
