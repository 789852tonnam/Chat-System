"""Transient room sound counters, membership and restart behavior over real TCP."""

import concurrent.futures
import contextlib
import tempfile
import unittest
from pathlib import Path

from server import ChatServer, ServerConfig
from test_chat import ChatPeer, initial_sync


class EffectTests(unittest.TestCase):
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

    def test_only_members_can_trigger_with_exact_room_only_payload(self):
        server = self.server()
        alice, bob, carol, guest = [self.peer(server, name) for name in ("Alice", "Bob", "Carol", None)]
        key = self.room(alice, bob)
        self.assertEqual(guest.call("trigger_effect", {"room_key": key})["error"]["code"], "UNAUTHENTICATED")
        self.assertEqual(carol.call("trigger_effect", {"room_key": key})["error"]["code"], "NOT_ROOM_MEMBER")
        self.assertEqual(carol.call("sync_room", initial_sync(key))["error"]["code"], "NOT_ROOM_MEMBER")
        for payload in [{}, {"room_key": True}, {"room_key": key, "sound_id": "other"},
                        {"room_key": key, "message_id": 1}, {"room_key": key, "url": "https://other.example"}]:
            self.assertEqual(alice.call("trigger_effect", payload)["error"]["code"], "INVALID_PAYLOAD")
        missing = "group:00000000-0000-4000-8000-000000000000"
        self.assertEqual(alice.call("trigger_effect", {"room_key": missing})["error"]["code"], "ROOM_NOT_FOUND")
        self.assertEqual(alice.ok("sync_room", initial_sync(key))["effect_revision"], 0)

    def test_empty_room_sound_is_separate_from_messages_room_revision_and_other_rooms(self):
        server = self.server()
        alice, bob, carol = [self.peer(server, name) for name in ("Alice", "Bob", "Carol")]
        key, other = self.room(alice, bob), self.room(alice, carol)
        for _ in range(3):
            self.assertEqual(alice.ok("trigger_effect", {"room_key": key}), {"triggered": True})
        for peer in (alice, bob):
            data = peer.ok("sync_room", initial_sync(key))
            self.assertEqual((data["effect_revision"], data["effect"]), (3, {"sound_id": "rimshot"}))
            self.assertEqual((data["latest_id"], data["room_revision"]), (0, 0))
            self.assertEqual(data["older_messages"] + data["new_messages"], [])
        self.assertEqual(alice.ok("sync_room", initial_sync(other))["effect_revision"], 0)
        message_id = alice.ok("send_message", {"room_key": key, "content": "Hello"})["message"]["id"]
        alice.ok("set_heart", {"room_key": key, "message_id": message_id, "hearted": True})
        alice.ok("trigger_effect", {"room_key": key})
        data = bob.ok("sync_room", initial_sync(key))
        self.assertEqual((data["room_revision"], data["effect_revision"]), (1, 4))
        self.assertEqual(len(data["older_messages"]), 1)

    def test_group_members_share_sound_without_changing_membership(self):
        server = self.server()
        alice, bob, carol = [self.peer(server, name) for name in ("Alice", "Bob", "Carol")]
        key = alice.ok("create_group", {"name": "Sound"})["room"]["room_key"]
        bob.ok("join_group", {"room_key": key})
        bob.ok("trigger_effect", {"room_key": key})
        self.assertEqual(alice.ok("sync_room", initial_sync(key))["effect_revision"], 1)
        self.assertEqual(carol.call("trigger_effect", {"room_key": key})["error"]["code"], "NOT_ROOM_MEMBER")
        self.assertEqual(len(carol.ok("get_directory")["groups"][0]["members"]), 2)

    def test_restart_resets_transient_effect_and_keeps_history(self):
        folder = self.resources.enter_context(tempfile.TemporaryDirectory())
        path = str(Path(folder) / "sound.sqlite3")
        first = self.server(path)
        alice, bob = self.peer(first, "Alice"), self.peer(first, "Bob")
        key = self.room(alice, bob)
        alice.ok("send_message", {"room_key": key, "content": "Persistent"})
        alice.ok("trigger_effect", {"room_key": key})
        first.stop()
        restarted = self.server(path)
        returning = self.peer(restarted, "Bob")
        data = returning.ok("sync_room", initial_sync(key))
        self.assertEqual((data["effect_revision"], data["effect"]), (0, None))
        self.assertEqual(data["older_messages"][0]["content"], "Persistent")

    def test_concurrent_triggers_advance_one_room_counter(self):
        server = self.server()
        alice, bob = self.peer(server, "Alice"), self.peer(server, "Bob")
        key = self.room(alice, bob)
        def trigger(index):
            with ChatPeer(server.address, "Alice" if index % 2 else "Bob") as peer:
                return peer.ok("trigger_effect", {"room_key": key})
        with concurrent.futures.ThreadPoolExecutor(max_workers=5) as pool:
            self.assertTrue(all(item["triggered"] for item in pool.map(trigger, range(10))))
        self.assertEqual(alice.ok("sync_room", initial_sync(key))["effect_revision"], 10)
