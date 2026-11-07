"""Online directory snapshots across login, logout, disconnect and restart."""

import contextlib
import tempfile
import time
import unittest
from pathlib import Path

from server import ChatServer, ServerConfig
from test_server import Peer


class DirectoryTests(unittest.TestCase):
    def setUp(self):
        self.resources = contextlib.ExitStack()
        self.addCleanup(self.resources.close)

    def server(self, **options):
        options.setdefault("database_path", ":memory:")
        server = ChatServer(ServerConfig(host="127.0.0.1", port=0, **options))
        server.start()
        self.addCleanup(server.stop)
        return server

    def peer(self, server, username=None):
        peer = self.resources.enter_context(Peer(server.address))
        if username is not None:
            peer.send(1, "login", {"username": username})
            self.assertTrue(peer.read()["ok"])
        return peer

    def directory(self, peer, request_id):
        peer.send(request_id, "get_directory")
        response = peer.read()
        self.assertTrue(response["ok"], response)
        self.assertEqual(response["data"]["groups"], [])
        return response["data"]["online_users"]

    def wait_for_names(self, peer, expected):
        deadline = time.monotonic() + 2
        request_id = 2
        while time.monotonic() < deadline:
            names = [user["username"] for user in self.directory(peer, request_id)]
            if names == expected:
                return
            request_id += 1
            time.sleep(0.02)
        self.fail(f"Directory did not become {expected}; last snapshot: {names}")

    def test_requires_login_and_rejects_extra_payload(self):
        peer = self.peer(self.server())
        peer.send(1, "get_directory")
        self.assertEqual(peer.read()["error"]["code"], "UNAUTHENTICATED")
        peer.send(2, "login", {"username": "Alice"})
        self.assertTrue(peer.read()["ok"])
        peer.send(3, "get_directory", {"user_id": 99})
        self.assertEqual(peer.read()["error"]["code"], "INVALID_PAYLOAD")
        self.assertEqual([user["username"] for user in self.directory(peer, 4)], ["Alice"])

    def test_directory_is_sorted_deduplicated_and_includes_requester(self):
        server = self.server()
        bob = self.peer(server, "bob")
        self.peer(server, "Zulu")
        self.peer(server, "Alice")
        self.peer(server, "ALICE")
        users = self.directory(bob, 2)
        self.assertEqual([user["username"] for user in users], ["Alice", "bob", "Zulu"])
        self.assertEqual(len({user["id"] for user in users}), 3)

    def test_logout_keeps_user_online_until_its_last_session_logs_out(self):
        server = self.server()
        bob = self.peer(server, "Bob")
        first = self.peer(server, "Alice")
        second = self.peer(server, "alice")
        first.send(2, "logout")
        self.assertTrue(first.read()["ok"])
        self.assertEqual([user["username"] for user in self.directory(bob, 2)], ["Alice", "Bob"])
        second.send(2, "logout")
        self.assertTrue(second.read()["ok"])
        self.assertEqual([user["username"] for user in self.directory(bob, 3)], ["Bob"])

    def test_disconnect_removes_user_from_next_snapshots(self):
        server = self.server()
        bob = self.peer(server, "Bob")
        alice = self.peer(server, "Alice")
        self.assertEqual(len(self.directory(bob, 2)), 2)
        alice.close()
        # Use a fresh observer so retry IDs start independently of the earlier query.
        observer = self.peer(server, "bob")
        self.wait_for_names(observer, ["Bob"])

    def test_idle_connection_is_removed_while_polling_user_stays_online(self):
        server = self.server(idle_timeout=0.15)
        bob = self.peer(server, "Bob")
        alice = self.peer(server, "Alice")
        self.wait_for_names(bob, ["Bob"])
        self.assertTrue(alice.is_closed())

    def test_restart_does_not_mark_stored_users_online(self):
        directory = self.resources.enter_context(tempfile.TemporaryDirectory())
        path = str(Path(directory) / "chat.sqlite3")
        first = self.server(database_path=path)
        self.peer(first, "Alice")
        first.stop()
        second = self.server(database_path=path)
        bob = self.peer(second, "Bob")
        self.assertEqual([user["username"] for user in self.directory(bob, 2)], ["Bob"])
