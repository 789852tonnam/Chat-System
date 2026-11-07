"""Group discovery, explicit membership, authorization and persistent history."""

import concurrent.futures
import contextlib
import tempfile
import unittest
import uuid
from pathlib import Path

from common.protocol import validate_directory
from server import ChatServer, ServerConfig
from test_chat import ChatPeer, initial_sync, next_sync


class GroupTests(unittest.TestCase):
    def setUp(self):
        self.resources = contextlib.ExitStack()
        self.addCleanup(self.resources.close)

    def server(self, path=":memory:"):
        server = ChatServer(ServerConfig(host="127.0.0.1", port=0, database_path=path))
        server.start()
        self.addCleanup(server.stop)
        return server

    def peer(self, server, username=None):
        return self.resources.enter_context(ChatPeer(server.address, username))

    def test_create_normalizes_name_and_starts_with_creator_only(self):
        server = self.server()
        alice, bob = self.peer(server, "Alice"), self.peer(server, "Bob")
        name = "\u0e01\u0e25\u0e38\u0e48\u0e21 Study"
        group = alice.ok("create_group", {"name": f"  {name}  "})["room"]
        self.assertEqual(group["name"], name)
        self.assertEqual(group["kind"], "group")
        self.assertEqual(uuid.UUID(group["room_key"].removeprefix("group:")).version, 4)
        self.assertEqual(group["members"], [alice.user])
        self.assertEqual(group["created_by_user_id"], alice.user["id"])
        snapshot = bob.ok("get_directory")
        validate_directory(snapshot)
        self.assertEqual(snapshot["groups"], [group])
        self.assertNotIn("messages", group)
        data = alice.ok("sync_room", initial_sync(group["room_key"]))
        self.assertEqual((data["room_revision"], data["effect_revision"], data["latest_id"]), (0, 0, 0))
        self.assertEqual(data["older_messages"], [])

    def test_duplicate_names_create_distinct_keys_and_directory_excludes_private_rooms(self):
        server = self.server()
        alice, bob = self.peer(server, "Alice"), self.peer(server, "Bob")
        alice.ok("open_private", {"target_user_id": bob.user["id"]})
        first = alice.ok("create_group", {"name": "Study"})["room"]
        second = bob.ok("create_group", {"name": "Study"})["room"]
        self.assertNotEqual(first["room_key"], second["room_key"])
        expected = sorted([first, second], key=lambda room: (room["created_at"], room["room_key"]))
        self.assertEqual(alice.ok("get_directory")["groups"], expected)
        self.assertEqual(bob.ok("get_directory")["groups"], expected)

    def test_nonmembers_can_discover_but_cannot_read_or_send(self):
        server = self.server()
        alice, bob, carol = [self.peer(server, name) for name in ("Alice", "Bob", "Carol")]
        room = alice.ok("create_group", {"name": "Study"})["room"]
        key = room["room_key"]
        alice.ok("send_message", {"room_key": key, "content": "Members only"})
        for peer in (bob, carol):
            self.assertEqual(peer.ok("get_directory")["groups"], [room])
            for action, payload in [("sync_room", initial_sync(key)),
                                    ("send_message", {"room_key": key, "content": "Forbidden"})]:
                self.assertEqual(peer.call(action, payload)["error"]["code"], "NOT_ROOM_MEMBER")
            self.assertTrue(peer.ok("ping")["pong"])

    def test_explicit_join_reads_older_history_and_repeated_join_is_noop(self):
        server = self.server()
        alice, bob = self.peer(server, "Alice"), self.peer(server, "Bob")
        key = alice.ok("create_group", {"name": "Study"})["room"]["room_key"]
        for index in range(25):
            alice.ok("send_message", {"room_key": key, "content": f"History {index}"})
        joined = bob.ok("join_group", {"room_key": key})
        self.assertTrue(joined["joined"])
        self.assertEqual(joined["room"]["members"], [alice.user, bob.user])
        self.assertFalse(bob.ok("join_group", {"room_key": key})["joined"])
        self.assertFalse(alice.ok("join_group", {"room_key": key})["joined"])
        latest = bob.ok("sync_room", initial_sync(key))
        self.assertEqual(len(latest["older_messages"]), 20)
        self.assertEqual(latest["room_revision"], 0)
        older = bob.ok("sync_room", next_sync(latest, 20))
        self.assertEqual([m["content"] for m in older["older_messages"]], [f"History {i}" for i in range(5)])
        self.assertFalse(older["has_more_older"])
        bob.ok("send_message", {"room_key": key, "content": "Joined now"})
        self.assertEqual(alice.ok("sync_room", initial_sync(key))["older_messages"][-1]["sender"], bob.user)

    def test_group_membership_is_per_account_across_simultaneous_connections(self):
        server = self.server()
        alice, bob, another_bob = [self.peer(server, name) for name in ("Alice", "Bob", "BOB")]
        key = alice.ok("create_group", {"name": "Study"})["room"]["room_key"]
        self.assertTrue(bob.ok("join_group", {"room_key": key})["joined"])
        result = another_bob.ok("join_group", {"room_key": key})
        self.assertFalse(result["joined"])
        self.assertEqual(result["room"]["members"], [alice.user, bob.user])
        bob.ok("logout")
        self.assertEqual(another_bob.ok("send_message", {"room_key": key, "content": "Other connection"})["message"]["sender"], bob.user)

    def test_create_and_join_require_login_and_exact_payloads(self):
        server = self.server()
        alice = self.peer(server, "Alice")
        key = alice.ok("create_group", {"name": "Study"})["room"]["room_key"]
        guest = self.peer(server)
        for action, payload in [("create_group", {"name": "Study"}), ("join_group", {"room_key": key})]:
            self.assertEqual(guest.call(action, payload)["error"]["code"], "UNAUTHENTICATED")
        for action, payload in [
            ("create_group", {}), ("create_group", {"name": "Study", "members": [alice.user["id"]]}),
            ("join_group", {}), ("join_group", {"room_key": key, "user_id": alice.user["id"]}),
            ("join_group", {"room_key": True}),
        ]:
            self.assertEqual(alice.call(action, payload)["error"]["code"], "INVALID_PAYLOAD")
        self.assertEqual(len(alice.ok("get_directory")["groups"]), 1)

    def test_invalid_group_names_are_rejected_without_creating_rooms(self):
        alice = self.peer(self.server(), "Alice")
        for name in [None, True, 5, "", "   ", "x" * 81, "line\nbreak", "tab\tname", "bad\x00"]:
            with self.subTest(name=repr(name)):
                self.assertEqual(alice.call("create_group", {"name": name})["error"]["code"], "INVALID_PAYLOAD")
        self.assertEqual(alice.ok("get_directory")["groups"], [])
        self.assertEqual(alice.ok("create_group", {"name": "x" * 80})["room"]["name"], "x" * 80)

    def test_join_private_room_and_unknown_group_are_rejected(self):
        server = self.server()
        alice, bob, carol = [self.peer(server, name) for name in ("Alice", "Bob", "Carol")]
        private_key = alice.ok("open_private", {"target_user_id": bob.user["id"]})["room"]["room_key"]
        for peer in (alice, carol):
            self.assertEqual(peer.call("join_group", {"room_key": private_key})["error"]["code"], "GROUP_REQUIRED")
        missing = "group:00000000-0000-4000-8000-000000000000"
        self.assertEqual(carol.call("join_group", {"room_key": missing})["error"]["code"], "ROOM_NOT_FOUND")
        self.assertEqual(carol.call("sync_room", initial_sync(private_key))["error"]["code"], "NOT_ROOM_MEMBER")

    def test_group_history_and_membership_survive_restart_and_offline_members(self):
        folder = self.resources.enter_context(tempfile.TemporaryDirectory())
        path = str(Path(folder) / "chat.sqlite3")
        server = self.server(path)
        alice, bob = self.peer(server, "Alice"), self.peer(server, "Bob")
        key = alice.ok("create_group", {"name": "Persistent"})["room"]["room_key"]
        bob.ok("join_group", {"room_key": key})
        private_key = alice.ok("open_private", {"target_user_id": bob.user["id"]})["room"]["room_key"]
        alice.ok("send_message", {"room_key": private_key, "content": "Private stays private"})
        bob.ok("logout")
        alice.ok("send_message", {"room_key": key, "content": "While Bob was offline"})
        server.stop()
        restarted = self.server(path)
        returning_bob, carol = self.peer(restarted, "bob"), self.peer(restarted, "Carol")
        group = carol.ok("get_directory")["groups"][0]
        self.assertEqual(group["members"], [alice.user, bob.user])
        self.assertFalse(returning_bob.ok("join_group", {"room_key": key})["joined"])
        history = returning_bob.ok("sync_room", initial_sync(key))
        self.assertEqual([m["content"] for m in history["older_messages"]], ["While Bob was offline"])
        self.assertEqual(returning_bob.ok("sync_room", initial_sync(private_key))["older_messages"][0]["content"], "Private stays private")
        self.assertEqual(carol.call("sync_room", initial_sync(key))["error"]["code"], "NOT_ROOM_MEMBER")

    def test_concurrent_joins_keep_one_membership_per_account(self):
        server = self.server()
        alice = self.peer(server, "Alice")
        key = alice.ok("create_group", {"name": "Concurrent"})["room"]["room_key"]

        def join(_):
            with ChatPeer(server.address, "Bob") as peer:
                return peer.ok("join_group", {"room_key": key})

        with concurrent.futures.ThreadPoolExecutor(max_workers=5) as pool:
            results = list(pool.map(join, range(5)))
        self.assertEqual(sum(result["joined"] for result in results), 1)
        group = alice.ok("get_directory")["groups"][0]
        self.assertEqual([member["username"] for member in group["members"]], ["Alice", "Bob"])
