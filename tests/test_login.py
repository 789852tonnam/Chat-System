"""Username-only login, connection identity, and SQLite persistence tests."""

import concurrent.futures
import contextlib
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from server import ChatServer, ServerConfig
from test_server import Peer


class LoginTests(unittest.TestCase):
    def setUp(self):
        self.resources = contextlib.ExitStack()
        self.addCleanup(self.resources.close)

    def server(self, database_path=":memory:"):
        server = ChatServer(ServerConfig(host="127.0.0.1", port=0, database_path=database_path))
        server.start()
        self.addCleanup(server.stop)
        return server

    def peer(self, server):
        return self.resources.enter_context(Peer(server.address))

    def login(self, peer, username, request_id=1):
        peer.send(request_id, "login", {"username": username})
        response = peer.read()
        self.assertTrue(response["ok"], response)
        return response["data"]["user"]

    def test_first_login_creates_user_without_registration(self):
        server = self.server()
        alice = self.login(self.peer(server), "Alice")
        bob = self.login(self.peer(server), "Bob")
        self.assertEqual(alice["username"], "Alice")
        self.assertGreater(alice["id"], 0)
        self.assertNotEqual(alice["id"], bob["id"])

    def test_concurrent_case_variants_reuse_one_identity(self):
        server = self.server()
        original = self.login(self.peer(server), "Alice")

        def join(username):
            with Peer(server.address) as peer:
                return self.login(peer, username)

        with concurrent.futures.ThreadPoolExecutor(max_workers=8) as pool:
            users = list(pool.map(join, ["alice", "ALICE", "aLiCe", "Alice"] * 2))
        self.assertTrue(all(user == original for user in users))

    def test_logout_affects_only_its_connection_and_allows_another_name(self):
        server = self.server()
        first, second = self.peer(server), self.peer(server)
        original = self.login(first, "Alice")
        self.assertEqual(self.login(second, "alice"), original)
        first.send(2, "login", {"username": "Bob"})
        self.assertEqual(first.read()["error"]["code"], "ALREADY_AUTHENTICATED")
        first.send(3, "logout")
        self.assertEqual(first.read()["data"], {"logged_out": True})
        self.assertNotEqual(self.login(first, "Bob", 4)["id"], original["id"])
        second.send(2, "login", {"username": "Bob"})
        self.assertEqual(second.read()["error"]["code"], "ALREADY_AUTHENTICATED")

    def test_user_id_survives_server_restart(self):
        directory = self.resources.enter_context(tempfile.TemporaryDirectory())
        path = str(Path(directory) / "nested" / "chat.sqlite3")
        first = self.server(path)
        original = self.login(self.peer(first), "Alice")
        first.stop()
        second = self.server(path)
        self.assertEqual(self.login(self.peer(second), "ALICE"), original)

    def test_invalid_username_or_extra_fields_do_not_create_an_identity(self):
        peer = self.peer(self.server())
        payloads = [
            {}, {"username": "Alice", "password": "unused"},
            {"username": "Alice", "user_id": 1},
            *({"username": name} for name in [
                None, True, 123, "", "ab", "a" * 33, " Alice", "Alice ",
                "two names", "a-b", "a\nb", "\u0e44\u0e17\u0e22", "';DROP TABLE users;--",
            ]),
        ]
        for request_id, payload in enumerate(payloads, 1):
            peer.send(request_id, "login", payload)
            self.assertEqual(peer.read()["error"]["code"], "INVALID_PAYLOAD", payload)
        self.assertEqual(self.login(peer, "Alice", len(payloads) + 1)["username"], "Alice")

    def test_logout_is_idempotent_and_requires_an_empty_payload(self):
        peer = self.peer(self.server())
        for request_id in [1, 2]:
            peer.send(request_id, "logout")
            self.assertEqual(peer.read()["data"], {"logged_out": True})
        peer.send(3, "logout", {"username": "Alice"})
        self.assertEqual(peer.read()["error"]["code"], "INVALID_PAYLOAD")

    def test_register_is_no_longer_an_action(self):
        peer = self.peer(self.server())
        peer.send(1, "register", {"username": "Alice"})
        self.assertEqual(peer.read()["error"]["code"], "UNKNOWN_ACTION")
        self.assertEqual(self.login(peer, "Alice", 2)["username"], "Alice")

    def test_database_is_owned_by_the_application_worker(self):
        server = self.server()
        self.login(self.peer(server), "Alice")
        with self.assertRaises(sqlite3.ProgrammingError):
            server._database.execute("SELECT 1")
        server.stop()
        self.assertIsNone(server._database)

    def test_database_startup_failure_never_starts_transport(self):
        directory = self.resources.enter_context(tempfile.TemporaryDirectory())
        server = ChatServer(ServerConfig(host="127.0.0.1", port=0, database_path=directory))
        self.addCleanup(server.stop)
        with self.assertRaises(sqlite3.OperationalError):
            server.start()
        self.assertIsNone(server._network._listener)
        self.assertFalse(server._worker.is_alive())
        self.assertIsNone(server._network._acceptor)

    def test_database_error_does_not_bind_identity_or_kill_worker(self):
        peer = self.peer(self.server())
        with patch("server.get_or_create_user", side_effect=sqlite3.OperationalError("simulated")), \
                self.assertLogs("socket_chat.server", level="ERROR"):
            peer.send(1, "login", {"username": "Alice"})
            self.assertEqual(peer.read()["error"]["code"], "INTERNAL_ERROR")
        self.assertEqual(self.login(peer, "Alice", 2)["username"], "Alice")
