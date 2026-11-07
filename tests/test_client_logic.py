"""Headless application tests: fake network queues, no sockets or Tkinter."""

import queue
import unittest
from unittest.mock import Mock, patch

from client_logic import ClientLogic


class ClientLogicTests(unittest.TestCase):
    def setUp(self):
        self.events = []
        self.logic = ClientLogic(lambda kind, value: self.events.append((kind, value)))
        self.network = Mock()
        self.network.requests = queue.Queue()
        self.network.events = queue.Queue()
        self.network.is_alive.return_value = True
        self.factory = self.enterContext(patch("client_logic.NetworkClient", return_value=self.network))
        self.clock = self.enterContext(patch("client_logic.time.monotonic", return_value=100.0))
        self.response_id = 0
        self.alice = {"id": 1, "username": "Alice"}

    def take(self, action):
        actual_action, payload = self.network.requests.get_nowait()
        self.assertEqual(actual_action, action)
        self.assertTrue(self.network.requests.empty())
        return payload

    def reply(self, data=None, error=None):
        self.response_id += 1
        response = {"request_id": self.response_id, "ok": error is None}
        response["data" if error is None else "error"] = data if error is None else error
        self.network.events.put(("response", response))
        self.logic.poll()

    def join(self):
        self.logic.connect("127.0.0.1", "5000", "Alice")
        self.factory.assert_called_once_with("127.0.0.1", 5000)
        self.network.start.assert_called_once()
        self.network.events.put(("connected", None))
        self.logic.poll()
        self.assertEqual(self.take("login"), {"username": "Alice"})
        self.reply({"user": self.alice})
        self.assertEqual(self.take("get_directory"), {})
        self.reply({"online_users": [self.alice], "groups": []})
        self.assertEqual(self.logic.state, "connected")

    def message(self, message_id, room_key="private:1:2"):
        return {"id": message_id, "room_key": room_key, "sender_kind": "human",
                "sender": self.alice, "content": f"Message {message_id}",
                "created_at": "2026-11-08T00:00:00Z", "heart_count": 0,
                "hearted_by_me": False, "ai": None}

    def sync(self, older=(), newer=(), oldest=0, latest=0, has_older=False):
        return {"room_key": "private:1:2", "older_messages": list(older), "new_messages": list(newer),
                "oldest_id": oldest, "latest_id": latest, "room_revision": 0,
                "has_more_older": has_older, "has_more_new": False, "reload_required": False,
                "pinned_message": None, "effect_revision": 0, "effect": None}

    def open_room(self, initial=None):
        self.logic.open_chat(2, "Bob")
        self.assertEqual(self.take("open_private"), {"target_user_id": 2})
        self.reply({"room": {"room_key": "private:1:2", "kind": "private", "name": None,
                             "members": [self.alice, {"id": 2, "username": "Bob"}]}, "created": True})
        self.assertEqual(self.take("sync_room"), {
            "room_key": "private:1:2", "old_count": 20,
            "oldest_id": None, "latest_id": None, "room_revision": None,
        })
        self.reply(self.sync() if initial is None else initial)

    def test_invalid_form_values_never_allocate_a_network_worker(self):
        for host, port, username, field in [
            ("", "5000", "Alice", "host"),
            ("localhost", "0", "Alice", "port"),
            ("localhost", "invalid", "Alice", "port"),
            ("localhost", "5000", "invalid name", "username"),
        ]:
            with self.subTest(field=field, port=port):
                self.logic.connect(host, port, username)
                self.assertEqual(self.events[-1][0], "input_error")
                self.assertEqual(self.events[-1][1][0], field)
                self.assertEqual(self.logic.state, "disconnected")
        self.factory.assert_not_called()

    def test_login_directory_and_ping_are_serialized_without_stacked_polls(self):
        self.join()
        self.assertEqual(self.logic.user, self.alice)
        self.assertIn(("directory", [self.alice]), self.events)
        self.logic.ping()
        self.assertEqual(self.take("ping"), {})
        self.clock.return_value = 104.0
        for _ in range(3):
            self.logic.poll()
            self.logic.ping()
        self.assertTrue(self.network.requests.empty())
        self.reply({"pong": True})
        self.assertEqual(self.take("get_directory"), {})
        self.assertEqual(self.logic.state, "refreshing")
        self.logic.poll()
        self.assertTrue(self.network.requests.empty())
        self.reply({"online_users": [self.alice], "groups": []})
        self.assertEqual(self.logic.state, "connected")
        self.assertTrue(self.network.requests.empty())

    def test_ack_keeps_delivered_cursor_and_history_handles_global_id_gaps(self):
        self.join()
        self.open_room(self.sync(newer=[self.message(40), self.message(45)],
                                 oldest=40, latest=45, has_older=True))
        self.logic.send("My message")
        self.assertEqual(self.take("send_message"), {"room_key": "private:1:2", "content": "My message"})
        self.reply({"message": self.message(77)})
        self.assertEqual(self.logic.latest_id, 45)
        self.assertEqual(set(self.logic.messages), {40, 45})
        self.assertIn(("sent", None), self.events)
        self.assertEqual(self.take("sync_room")["latest_id"], 45)
        self.reply(self.sync(newer=[self.message(56), self.message(77)],
                             oldest=40, latest=77, has_older=True))
        self.logic.load_older()
        request = self.take("sync_room")
        self.assertEqual((request["old_count"], request["oldest_id"], request["latest_id"]), (20, 40, 77))
        self.reply(self.sync(older=[self.message(3), self.message(9)],
                             oldest=3, latest=77))
        self.assertEqual(set(self.logic.messages), {3, 9, 40, 45, 56, 77})
        self.assertEqual((self.logic.oldest_id, self.logic.latest_id), (3, 77))
        self.assertFalse(self.logic.has_more_older)

    def test_latest_room_switch_wins_and_old_history_never_enters_new_cache(self):
        self.join()
        self.open_room()
        self.clock.return_value = 101.0
        self.logic.poll()
        self.take("sync_room")
        self.logic.open_chat(2, "Bob")
        self.logic.open_chat(3, "Carol")
        self.assertTrue(self.network.requests.empty())
        self.reply(self.sync(newer=[self.message(10)], oldest=10, latest=10))
        self.assertEqual(self.logic.messages, {})
        self.assertIsNone(self.logic.room)
        self.assertIsNone(self.logic.latest_id)
        self.assertEqual(self.take("open_private"), {"target_user_id": 3})

    def test_invalid_content_and_definite_send_error_do_not_clear_composer(self):
        self.join()
        self.open_room()
        self.events.clear()
        self.logic.send("   ")
        self.assertTrue(self.network.requests.empty())
        self.assertEqual(self.events[-1][0], "log")
        self.logic.send("Retain this text")
        self.take("send_message")
        self.reply(error={"code": "SERVER_BUSY", "message": "Try later"})
        self.assertNotIn(("sent", None), self.events)
        self.assertEqual(self.logic.state, "connected")

    def test_close_ignores_late_login_and_emits_closed_only_after_worker_exit(self):
        self.logic.connect("localhost", "5000", "Alice")
        self.network.events.put(("connected", None))
        self.logic.poll()
        self.take("login")
        self.logic.close()
        self.network.stop.assert_called_once()
        self.reply({"user": self.alice})
        self.assertIsNone(self.logic.user)
        self.assertEqual(self.logic.state, "closing")
        self.assertTrue(self.network.requests.empty())
        self.assertNotIn(("closed", None), self.events)
        self.logic.connect("localhost", "5000", "Bob")
        self.factory.assert_called_once()
        self.network.is_alive.return_value = False
        self.logic.poll()
        self.network.join.assert_called_once()
        self.assertIsNone(self.logic.network)
        self.assertEqual(self.events[-1], ("closed", None))

    def test_final_shutdown_joins_worker_without_calling_the_view(self):
        self.logic.connect("localhost", "5000", "Alice")
        self.events.clear()
        self.logic.shutdown()
        self.network.stop.assert_called_once()
        self.network.join.assert_called_once()
        self.assertIsNone(self.logic.network)
        self.assertTrue(self.logic.closing)
        self.assertEqual(self.events, [])
