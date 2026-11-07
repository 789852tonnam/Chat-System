"""Private-room authorization, persistent history and cursor integration tests."""

import contextlib
import sqlite3
import tempfile
import unittest
from pathlib import Path

from server import ChatServer, ServerConfig
from test_server import Peer


class ChatPeer(Peer):
    def __init__(self, address, username=None):
        super().__init__(address)
        self.request_id = 0
        self.user = self.ok("login", {"username": username})["user"] if username else None

    def call(self, action, payload=None):
        self.request_id += 1
        self.send(self.request_id, action, payload)
        return self.read()

    def ok(self, action, payload=None):
        response = self.call(action, payload)
        if not response["ok"]:
            raise AssertionError(response)
        return response["data"]


def initial_sync(room_key):
    return {"room_key": room_key, "old_count": 20, "oldest_id": None,
            "latest_id": None, "room_revision": None}


def next_sync(data, old_count=0):
    return {"room_key": data["room_key"], "old_count": old_count,
            "oldest_id": data["oldest_id"], "latest_id": data["latest_id"],
            "room_revision": data["room_revision"]}


class PrivateChatTests(unittest.TestCase):
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

    def test_same_private_room_and_two_members_across_connections(self):
        server = self.server()
        alice, bob = self.peer(server, "Alice"), self.peer(server, "Bob")
        first = alice.ok("open_private", {"target_user_id": bob.user["id"]})
        second = bob.ok("open_private", {"target_user_id": alice.user["id"]})
        self.assertTrue(first["created"])
        self.assertFalse(second["created"])
        self.assertEqual(first["room"], second["room"])
        another = self.peer(server, "alice")
        room = another.ok("open_private", {"target_user_id": bob.user["id"]})["room"]
        self.assertEqual(room["members"], [alice.user, bob.user])

    def test_chat_actions_require_login(self):
        peer = self.peer(self.server())
        for action, payload in [
            ("open_private", {"target_user_id": 1}),
            ("send_message", {"room_key": "private:1:2", "content": "Hello"}),
            ("sync_room", initial_sync("private:1:2")),
        ]:
            self.assertEqual(peer.call(action, payload)["error"]["code"], "UNAUTHENTICATED")

    def test_private_target_validation(self):
        alice = self.peer(self.server(), "Alice")
        for target, code in [(alice.user["id"], "CANNOT_CHAT_SELF"), (999, "USER_NOT_FOUND"),
                             (True, "INVALID_PAYLOAD"), (1.0, "INVALID_PAYLOAD"), ("2", "INVALID_PAYLOAD")]:
            self.assertEqual(alice.call("open_private", {"target_user_id": target})["error"]["code"], code)

    def test_nonmember_cannot_read_send_or_spoof_sender(self):
        server = self.server()
        alice, bob, carol = [self.peer(server, name) for name in ["Alice", "Bob", "Carol"]]
        key = self.room(alice, bob)
        for action, payload in [("sync_room", initial_sync(key)),
                                ("send_message", {"room_key": key, "content": "Intrusion"})]:
            self.assertEqual(carol.call(action, payload)["error"]["code"], "NOT_ROOM_MEMBER")
        self.assertEqual(alice.call("send_message", {
            "room_key": key, "content": "Spoof", "sender_user_id": bob.user["id"],
        })["error"]["code"], "INVALID_PAYLOAD")
        self.assertEqual(alice.ok("sync_room", initial_sync(key))["latest_id"], 0)
        self.assertEqual(carol.call("sync_room", initial_sync("private:1:999"))["error"]["code"], "ROOM_NOT_FOUND")

    def test_empty_room_then_new_message(self):
        server = self.server()
        alice, bob = self.peer(server, "Alice"), self.peer(server, "Bob")
        key = self.room(alice, bob)
        empty = alice.ok("sync_room", initial_sync(key))
        self.assertEqual((empty["oldest_id"], empty["latest_id"]), (None, 0))
        self.assertEqual(alice.ok("sync_room", next_sync(empty))["new_messages"], [])
        message = bob.ok("send_message", {"room_key": key, "content": "Hello Alice"})["message"]
        updated = alice.ok("sync_room", next_sync(empty))
        self.assertEqual(updated["new_messages"], [message])
        self.assertEqual(updated["oldest_id"], message["id"])
        self.assertEqual(updated["latest_id"], message["id"])

    def test_history_pages_use_room_filtered_ids_with_gaps(self):
        server = self.server()
        alice, bob, carol = [self.peer(server, name) for name in ["Alice", "Bob", "Carol"]]
        key, other = self.room(alice, bob), self.room(alice, carol)
        sent = []
        for index in range(45):
            sent.append(alice.ok("send_message", {"room_key": key, "content": f"Message {index}"})["message"]["id"])
            if index % 3 == 0:
                alice.ok("send_message", {"room_key": other, "content": "Other room"})
        data = bob.ok("sync_room", initial_sync(key))
        self.assertEqual([m["id"] for m in data["older_messages"]], sent[-20:])
        self.assertTrue(data["has_more_older"])
        data = bob.ok("sync_room", next_sync(data, 20))
        self.assertEqual([m["id"] for m in data["older_messages"]], sent[-40:-20])
        data = bob.ok("sync_room", next_sync(data, 20))
        self.assertEqual([m["id"] for m in data["older_messages"]], sent[:5])
        self.assertFalse(data["has_more_older"])
        self.assertEqual(data["latest_id"], sent[-1])

    def test_new_history_catches_up_in_bounded_batches_without_skips(self):
        server = self.server()
        alice, bob = self.peer(server, "Alice"), self.peer(server, "Bob")
        key = self.room(alice, bob)
        data = bob.ok("sync_room", initial_sync(key))
        sent = [alice.ok("send_message", {"room_key": key, "content": f"Message {index}"})["message"]["id"]
                for index in range(205)]
        received = []
        for expected_count in [100, 100, 5]:
            data = bob.ok("sync_room", next_sync(data))
            self.assertEqual(len(data["new_messages"]), expected_count)
            received += [message["id"] for message in data["new_messages"]]
            self.assertEqual(data["latest_id"], received[-1])
            self.assertEqual(data["has_more_new"], len(received) < len(sent))
        self.assertEqual(received, sent)

    def test_content_normalization_and_validation(self):
        server = self.server()
        alice, bob = self.peer(server, "Alice"), self.peer(server, "Bob")
        key = self.room(alice, bob)
        content = alice.ok("send_message", {"room_key": key, "content": "  hello\r\nworld\t!  "})["message"]["content"]
        self.assertEqual(content, "  hello\nworld\t!  ")
        for content in [None, True, 12, "", " \n\t", "x" * 4001, "a\x00b", "a\rb", "a\x7fb"]:
            self.assertEqual(alice.call("send_message", {"room_key": key, "content": content})["error"]["code"], "INVALID_PAYLOAD")
        self.assertEqual(len(alice.ok("send_message", {"room_key": key, "content": "x" * 4000})["message"]["content"]), 4000)

    def test_offline_recipient_receives_stored_messages_after_rejoining(self):
        server = self.server()
        alice, bob = self.peer(server, "Alice"), self.peer(server, "Bob")
        key = self.room(alice, bob)
        bob.ok("logout")
        bob.close()
        self.assertEqual(len(alice.ok("get_directory")["online_users"]), 1)
        alice.ok("send_message", {"room_key": key, "content": "While offline"})
        returning = self.peer(server, "bob")
        self.assertEqual(returning.user, bob.user)
        self.assertEqual(returning.ok("sync_room", initial_sync(key))["older_messages"][0]["content"], "While offline")

    def test_rooms_membership_and_messages_survive_restart(self):
        folder = self.resources.enter_context(tempfile.TemporaryDirectory())
        path = str(Path(folder) / "chat.sqlite3")
        first = self.server(path)
        alice, bob = self.peer(first, "Alice"), self.peer(first, "Bob")
        key = self.room(alice, bob)
        original = alice.ok("send_message", {"room_key": key, "content": "Persist me"})["message"]
        first.stop()
        second = self.server(path)
        returning = self.peer(second, "Bob")
        opened = returning.ok("open_private", {"target_user_id": alice.user["id"]})
        self.assertFalse(opened["created"])
        self.assertEqual(opened["room"]["room_key"], key)
        self.assertEqual(returning.ok("sync_room", initial_sync(key))["older_messages"], [original])

    def test_revision_mismatch_does_not_advance_history(self):
        server = self.server()
        alice, bob = self.peer(server, "Alice"), self.peer(server, "Bob")
        key = self.room(alice, bob)
        alice.ok("send_message", {"room_key": key, "content": "Hello"})
        data = alice.ok("sync_room", initial_sync(key))
        request = next_sync(data)
        request["room_revision"] = 99
        mismatch = alice.ok("sync_room", request)
        self.assertTrue(mismatch["reload_required"])
        self.assertEqual(mismatch["oldest_id"], data["oldest_id"])
        self.assertEqual(mismatch["latest_id"], data["latest_id"])
        self.assertEqual(mismatch["older_messages"] + mismatch["new_messages"], [])

    def test_users_only_database_is_extended_without_losing_identity(self):
        folder = self.resources.enter_context(tempfile.TemporaryDirectory())
        path = str(Path(folder) / "chat.sqlite3")
        with contextlib.closing(sqlite3.connect(path)) as connection:
            connection.execute("CREATE TABLE users (id INTEGER PRIMARY KEY AUTOINCREMENT, "
                               "username TEXT NOT NULL COLLATE NOCASE UNIQUE, created_at TEXT NOT NULL)")
            connection.execute("INSERT INTO users VALUES (42, 'Alice', '2026-11-07T00:00:00.000Z')")
            connection.commit()
        server = self.server(path)
        alice, bob = self.peer(server, "alice"), self.peer(server, "Bob")
        self.assertEqual(alice.user, {"id": 42, "username": "Alice"})
        key = self.room(alice, bob)
        alice.ok("send_message", {"room_key": key, "content": "After upgrade"})
        self.assertEqual(bob.ok("sync_room", initial_sync(key))["older_messages"][0]["content"], "After upgrade")
