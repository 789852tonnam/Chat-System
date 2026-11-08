"""Queued AI jobs, authorization, immutable sources and persisted AI messages."""

import contextlib
import sqlite3
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch

import database
from common.protocol import ProtocolError, validate_message, validate_summary_status
from server import ChatServer, ServerConfig, SummaryCompletion
from server_ai import AIConfig
from test_chat import ChatPeer, initial_sync

CONFIG = AIConfig("https://provider.example/v1", "test-model", "dummy-private-key")


class SummaryTests(unittest.TestCase):
    def setUp(self):
        self.resources = contextlib.ExitStack()
        self.addCleanup(self.resources.close)

    def server(self, path=":memory:", configured=True):
        server = ChatServer(ServerConfig(host="127.0.0.1", port=0, database_path=path), ai_config=CONFIG if configured else None)
        server.start()
        self.addCleanup(server.stop)
        return server

    def peer(self, server, name=None):
        return self.resources.enter_context(ChatPeer(server.address, name))

    def setup_room(self, server):
        alice, bob = self.peer(server, "Alice"), self.peer(server, "Bob")
        key = alice.ok("open_private", {"target_user_id": bob.user["id"]})["room"]["room_key"]
        return alice, bob, key

    def send(self, peer, key, content="Hello"):
        return peer.ok("send_message", {"room_key": key, "content": content})["message"]

    def wait_job(self, peer, job_id, expected="succeeded"):
        deadline = time.monotonic() + 3
        while time.monotonic() < deadline:
            data = peer.ok("get_summary_status", {"job_id": job_id})
            validate_summary_status(data, job_id)
            if data["job"]["state"] != "running":
                self.assertEqual(data["job"]["state"], expected, data)
                return data["job"]
            time.sleep(0.01)
        self.fail("AI job did not finish")

    def test_latest_fifty_humans_exclude_ai_and_do_not_change_after_acceptance(self):
        entered, release = threading.Event(), threading.Event()
        calls = []
        def generate(config, source):
            calls.append(source)
            if len(calls) > 1:
                entered.set()
                release.wait(3)
            return "Summary text", "actual-model"
        with patch("server.generate_summary", side_effect=generate):
            server = self.server()
            alice, bob, key = self.setup_room(server)
            sent = [self.send(alice, key, f"Human {i}") for i in range(75)]
            first = alice.ok("request_summary", {"room_key": key})["job_id"]
            self.wait_job(alice, first)
            second = alice.ok("request_summary", {"room_key": key})["job_id"]
            try:
                self.assertTrue(entered.wait(1))
                self.assertTrue(bob.ok("ping")["pong"])
                self.send(bob, key, "After acceptance")
                self.assertEqual([m["content"] for m in calls[1]], [f"Human {i}" for i in range(25, 75)])
                self.assertEqual({m["sender_kind"] for m in calls[1]}, {"human"})
            finally:
                release.set()
            job = self.wait_job(alice, second)
            history = alice.ok("sync_room", initial_sync(key))["older_messages"]
            result = next(message for message in history if message["id"] == job["result_message_id"])
            validate_message(result, key)
            self.assertIsNone(result["sender"])
            self.assertEqual(result["ai"], {"model": "actual-model", "summary_job_id": second, "source_last_message_id": sent[-1]["id"]})
            # Duplicate completions never persist a second message for the job.
            server._work.put(SummaryCompletion(second, result=("Duplicate", "actual-model")))
            alice.ok("ping")
            self.assertEqual(sum(m["ai"] is not None and m["ai"]["summary_job_id"] == second
                                 for m in alice.ok("sync_room", initial_sync(key))["older_messages"]), 1)

    def test_two_requests_run_concurrently_and_can_finish_out_of_order(self):
        entered = [threading.Event(), threading.Event()]
        release = [threading.Event(), threading.Event()]
        lock = threading.Lock()
        identities = []
        def generate(config, source):
            with lock:
                index = len(identities)
                identities.append(threading.get_ident())
            entered[index].set()
            release[index].wait(3)
            return f"Summary {index}", "actual-model"
        with patch("server.generate_summary", side_effect=generate):
            alice, bob, key = self.setup_room(self.server())
            self.send(alice, key)
            first = alice.ok("request_summary", {"room_key": key})["job_id"]
            alice.ok("logout")
            alice.ok("login", {"username": "Alice"})
            second = alice.ok("request_summary", {"room_key": key})["job_id"]
            try:
                self.assertTrue(all(event.wait(1) for event in entered))
                self.assertEqual(len(set(identities)), 2)
                release[1].set()
                self.wait_job(alice, second)
                self.assertEqual(alice.ok("get_summary_status", {"job_id": first})["job"]["state"], "running")
            finally:
                for event in release:
                    event.set()
            self.wait_job(alice, first)

    def test_membership_config_source_and_payload_validation(self):
        server = self.server(configured=False)
        alice, bob, key = self.setup_room(server)
        carol, guest = self.peer(server, "Carol"), self.peer(server)
        self.assertEqual(alice.call("request_summary", {"room_key": key})["error"]["code"], "AI_NOT_CONFIGURED")
        self.assertEqual(carol.call("request_summary", {"room_key": key})["error"]["code"], "NOT_ROOM_MEMBER")
        self.assertEqual(guest.call("request_summary", {"room_key": key})["error"]["code"], "UNAUTHENTICATED")
        configured = self.server()
        alice, bob, key = self.setup_room(configured)
        self.assertEqual(alice.call("request_summary", {"room_key": key})["error"]["code"], "NO_HUMAN_MESSAGES")
        for action, payload in [("request_summary", {"room_key": key, "model": "other"}),
                                ("get_summary_status", {"job_id": True}), ("get_summary_status", {})]:
            self.assertEqual(alice.call(action, payload)["error"]["code"], "INVALID_PAYLOAD")

    def test_failures_are_private_safe_statuses_and_do_not_create_chat_errors(self):
        for error, expected in [(ProtocolError("AI_RATE_LIMITED", "Try later", details={"retry_after_seconds": 5}), "AI_RATE_LIMITED"),
                                (RuntimeError(CONFIG.api_key), "AI_FAILED")]:
            with self.subTest(code=expected), patch("server.generate_summary", side_effect=error):
                server = self.server()
                alice, bob, key = self.setup_room(server)
                same_alice, carol = self.peer(server, "ALICE"), self.peer(server, "Carol")
                self.send(alice, key)
                job_id = alice.ok("request_summary", {"room_key": key})["job_id"]
                job = self.wait_job(same_alice, job_id, "failed")
                self.assertEqual(job["error"]["code"], expected)
                self.assertNotIn(CONFIG.api_key, str(job))
                for peer in (bob, carol):
                    self.assertEqual(peer.call("get_summary_status", {"job_id": job_id})["error"]["code"], "SUMMARY_JOB_NOT_FOUND")
                self.assertEqual(len(alice.ok("sync_room", initial_sync(key))["older_messages"]), 1)

    def test_deadline_expiry_and_late_completion_do_not_store_a_result(self):
        entered, release = threading.Event(), threading.Event()
        def generate(config, source):
            entered.set()
            release.wait(3)
            return "Late", "actual-model"
        with patch("server.generate_summary", side_effect=generate), patch("server.JOB_TIMEOUT", 0.04), patch("server.JOB_RETENTION", 0.02):
            server = self.server()
            alice, bob, key = self.setup_room(server)
            self.send(alice, key)
            job_id = alice.ok("request_summary", {"room_key": key})["job_id"]
            self.assertTrue(entered.wait(1))
            threads = list(server._ai_threads)
            try:
                job = self.wait_job(alice, job_id, "failed")
                self.assertEqual(job["error"]["code"], "AI_TIMEOUT")
                time.sleep(0.03)
                self.assertEqual(alice.call("get_summary_status", {"job_id": job_id})["error"]["code"], "SUMMARY_JOB_NOT_FOUND")
            finally:
                release.set()
                for thread in threads:
                    thread.join(2)
            alice.ok("ping")
            self.assertEqual(len(alice.ok("sync_room", initial_sync(key))["older_messages"]), 1)

    def legacy_database(self, path, corrupt=False):
        with contextlib.closing(sqlite3.connect(path)) as connection:
            connection.executescript("""
                CREATE TABLE users(id INTEGER PRIMARY KEY AUTOINCREMENT, username TEXT UNIQUE COLLATE NOCASE, created_at TEXT);
                CREATE TABLE rooms(room_key TEXT PRIMARY KEY, kind TEXT, name TEXT, created_by INTEGER REFERENCES users(id), room_revision INTEGER DEFAULT 0, pinned_message_id INTEGER REFERENCES messages(id), created_at TEXT);
                CREATE TABLE room_members(room_key TEXT REFERENCES rooms(room_key), user_id INTEGER REFERENCES users(id), joined_at TEXT, PRIMARY KEY(room_key,user_id));
                CREATE TABLE messages(id INTEGER PRIMARY KEY AUTOINCREMENT, room_key TEXT NOT NULL, sender_user_id INTEGER NOT NULL, content TEXT NOT NULL, created_at TEXT NOT NULL, FOREIGN KEY(room_key,sender_user_id) REFERENCES room_members(room_key,user_id));
                CREATE INDEX messages_room_id ON messages(room_key,id);
                CREATE TABLE message_hearts(message_id INTEGER REFERENCES messages(id), user_id INTEGER REFERENCES users(id), created_at TEXT, PRIMARY KEY(message_id,user_id));
                INSERT INTO users VALUES(1,'Alice','old'),(2,'Bob','old');
                INSERT INTO rooms VALUES('private:1:2','private',NULL,1,2,NULL,'old');
                INSERT INTO room_members VALUES('private:1:2',1,'old'),('private:1:2',2,'old');
                INSERT INTO messages VALUES(7,'private:1:2',1,'Preserved','old'),(100,'private:1:2',1,'Deleted','old');
                DELETE FROM messages WHERE id=100;
                INSERT INTO message_hearts VALUES(7,2,'old');
                UPDATE rooms SET pinned_message_id=7;
            """)
            if corrupt:
                connection.execute("UPDATE rooms SET pinned_message_id=999")
                connection.commit()

    def test_upgrade_preserves_fk_rows_ids_and_ai_hearts_pin_after_restart(self):
        folder = self.resources.enter_context(tempfile.TemporaryDirectory())
        path = str(Path(folder) / "legacy.sqlite3")
        self.legacy_database(path)
        with patch("server.generate_summary", return_value=("Persisted AI", "actual-model")):
            first = self.server(path)
            alice, bob = self.peer(first, "Alice"), self.peer(first, "Bob")
            key = "private:1:2"
            self.assertEqual(alice.ok("sync_room", initial_sync(key))["pinned_message"]["id"], 7)
            job_id = alice.ok("request_summary", {"room_key": key})["job_id"]
            job = self.wait_job(alice, job_id)
            self.assertGreater(job["result_message_id"], 100)
            bob.ok("set_heart", {"room_key": key, "message_id": job["result_message_id"], "hearted": True})
            bob.ok("set_pin", {"room_key": key, "message_id": job["result_message_id"]})
            first.stop()
        restarted = self.server(path, configured=False)
        returning = self.peer(restarted, "Alice")
        history = returning.ok("sync_room", initial_sync(key))
        self.assertEqual(history["older_messages"][0]["content"], "Preserved")
        self.assertEqual(history["older_messages"][0]["heart_count"], 1)
        ai = history["pinned_message"]
        validate_message(ai, key)
        self.assertEqual(ai["ai"]["model"], "actual-model")
        self.assertEqual(ai["ai"]["summary_job_id"], job_id)
        self.assertEqual(ai["heart_count"], 1)
        self.assertEqual(returning.call("get_summary_status", {"job_id": job_id})["error"]["code"], "SUMMARY_JOB_NOT_FOUND")
        restarted.stop()
        with contextlib.closing(sqlite3.connect(path)) as connection:
            self.assertEqual(connection.execute("PRAGMA foreign_key_check").fetchall(), [])

    def test_failed_migration_restores_original_table_and_data(self):
        folder = self.resources.enter_context(tempfile.TemporaryDirectory())
        path = str(Path(folder) / "corrupt.sqlite3")
        self.legacy_database(path, corrupt=True)
        with self.assertRaises(sqlite3.IntegrityError):
            database.open_database(path)
        with contextlib.closing(sqlite3.connect(path)) as connection:
            column = next(row for row in connection.execute("PRAGMA table_info(messages)") if row[1] == "sender_user_id")
            self.assertEqual(column[3], 1)
            self.assertEqual(connection.execute("SELECT content FROM messages WHERE id=7").fetchone()[0], "Preserved")
            self.assertIsNone(connection.execute("SELECT name FROM sqlite_master WHERE name='messages_upgrade'").fetchone())

    def test_shutdown_drops_pending_completion_without_waiting_for_provider(self):
        entered, release = threading.Event(), threading.Event()
        def generate(config, source):
            entered.set()
            release.wait(3)
            return "Too late", "actual-model"
        folder = self.resources.enter_context(tempfile.TemporaryDirectory())
        path = str(Path(folder) / "shutdown.sqlite3")
        with patch("server.generate_summary", side_effect=generate):
            server = self.server(path)
            alice, bob, key = self.setup_room(server)
            self.send(alice, key)
            alice.ok("request_summary", {"room_key": key})
            self.assertTrue(entered.wait(1))
            threads = list(server._ai_threads)
            try:
                start = time.monotonic()
                server.stop()
                self.assertLess(time.monotonic() - start, 1)
                self.assertIsNone(server._database)
            finally:
                release.set()
                for thread in threads:
                    thread.join(2)
        restarted = self.server(path, configured=False)
        returning = self.peer(restarted, "Bob")
        self.assertEqual(len(returning.ok("sync_room", initial_sync(key))["older_messages"]), 1)
