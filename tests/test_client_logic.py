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

    def join(self, groups=()):
        self.logic.connect("127.0.0.1", "5000", "Alice")
        self.factory.assert_called_once_with("127.0.0.1", 5000)
        self.network.start.assert_called_once()
        self.network.events.put(("connected", None))
        self.logic.poll()
        self.assertEqual(self.take("login"), {"username": "Alice"})
        self.reply({"user": self.alice})
        self.assertEqual(self.take("get_directory"), {})
        self.reply({"online_users": [self.alice], "groups": list(groups)})
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

    # Status presentation must distinguish automatic sync from initial or manual
    # history loads without changing the busy state used by request guards.
    def test_display_state_hides_background_progress_only(self):
        for state in ("refreshing", "reloading", "checking_summary"):
            with self.subTest(state=state):
                self.logic.state = state
                self.assertEqual(self.logic.display_state, "connected")
                self.assertEqual(self.logic.state, state)
        self.logic.state = "syncing"
        self.logic._pending_payload = {"old_count": 20}
        self.assertEqual(self.logic.display_state, "syncing")
        self.logic.latest_id = 0
        self.assertEqual(self.logic.display_state, "syncing")
        self.logic._pending_payload = {"old_count": 0}
        self.assertEqual(self.logic.display_state, "connected")
        self.logic.latest_id = None
        self.assertEqual(self.logic.display_state, "syncing")
        for state in ("sending", "opening", "waiting", "disconnecting", "disconnected"):
            self.logic.state = state
            self.assertEqual(self.logic.display_state, state)

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

    # Accept one foreground action during polling, without overwriting the
    # outstanding reply context or sending another request before it completes.
    def test_send_during_poll_waits_then_runs_before_more_polling(self):
        self.join()
        self.open_room()
        self.clock.return_value = 104.0
        self.logic.poll()
        self.take("get_directory")
        self.logic.send("Queued while polling")
        self.assertEqual(self.logic.display_state, "sending")
        self.assertEqual(self.logic._pending_action, "get_directory")
        self.assertTrue(self.network.requests.empty())
        self.logic.send("Ignore a repeated click")
        self.reply({"online_users": [self.alice], "groups": []})
        self.assertEqual(self.take("send_message"), {
            "room_key": "private:1:2", "content": "Queued while polling"})
        self.reply(error={"code": "SERVER_BUSY", "message": "Try later"})
        self.assertNotIn(("sent", None), self.events)

    # Older history is an intent: build its cursor payload after the running
    # room poll completes, so a new message cannot make the queued bounds stale.
    def test_queued_older_history_uses_completed_poll_cursors(self):
        self.join()
        self.open_room(self.sync(newer=[self.message(10)], oldest=10, latest=10, has_older=True))
        self.clock.return_value = 101.0
        self.logic.poll()
        self.take("sync_room")
        self.logic.load_older()
        self.assertEqual(self.logic.display_state, "syncing")
        self.assertTrue(self.network.requests.empty())
        self.reply(self.sync(newer=[self.message(15)], oldest=10, latest=15, has_older=True))
        request = self.take("sync_room")
        self.assertEqual((request["old_count"], request["oldest_id"], request["latest_id"]), (20, 10, 15))

    # Every foreground button uses the same waiting slot. Reject duplicate
    # clicks while the command waits and preserve the original poll context.
    def test_all_foreground_actions_can_wait_behind_directory_poll(self):
        self.join()
        self.open_room(self.sync(newer=[self.message(1)], oldest=1, latest=1, has_older=True))
        actions = [
            (self.logic.ping, "ping", {}),
            (lambda: self.logic.toggle_heart(1), "set_heart", {"message_id": 1, "hearted": True}),
            (lambda: self.logic.set_pin(1), "set_pin", {"message_id": 1}),
            (self.logic.request_summary, "request_summary", {}),
            (self.logic.trigger_effect, "trigger_effect", {}),
            (lambda: self.logic.create_group("Queued group"), "create_group", {"name": "Queued group"}),
        ]
        for invoke, action, expected in actions:
            with self.subTest(action=action):
                self.logic._next_directory_poll = 99
                self.logic.poll()
                self.take("get_directory")
                invoke()
                invoke()
                self.assertTrue(self.network.requests.empty())
                self.assertEqual(self.logic._pending_action, "get_directory")
                self.reply({"online_users": [self.alice], "groups": []})
                payload = self.take(action)
                if action not in {"ping", "create_group"}:
                    expected = {"room_key": "private:1:2", **expected}
                self.assertEqual(payload, expected)
                self.reply(error={"code": "SERVER_BUSY", "message": "Try later"})

    # Complete a pending revision snapshot before rebuilding older-history bounds.
    def test_queued_older_history_waits_for_revision_snapshot(self):
        self.join()
        self.open_room(self.sync(newer=[self.message(10)], oldest=10, latest=10, has_older=True))
        self.clock.return_value = 101.0
        self.logic.poll()
        self.take("sync_room")
        self.logic.load_older()
        self.reply({**self.sync(oldest=10, latest=10, has_older=True),
                    "room_revision": 1, "reload_required": True})
        self.assertEqual(self.take("get_room_snapshot")["room_revision"], 1)
        self.assertEqual(self.logic.display_state, "syncing")
        self.reply({"messages": [self.message(10)], "room_revision": 1,
                    "pinned_message": None, "has_more": False, "next_after_id": None})
        payload = self.take("sync_room")
        self.assertEqual((payload["old_count"], payload["latest_id"], payload["room_revision"]), (20, 10, 1))

    # A room switch cancels a waiting room command instead of sending it into
    # the old conversation or applying its eventual acknowledgement to a new one.
    def test_room_switch_cancels_command_waiting_behind_poll(self):
        self.join()
        self.open_room()
        self.clock.return_value = 101.0
        self.logic.poll()
        self.take("sync_room")
        self.logic.send("Cancel on room switch")
        self.logic.open_chat(3, "Carol")
        self.reply(self.sync())
        self.assertEqual(self.take("open_private"), {"target_user_id": 3})
        self.assertIsNone(self.logic._queued_request)

    # Closing or disconnecting drops waiting commands even when a late poll
    # response is drained before the worker actually finishes shutting down.
    def test_disconnect_cancels_waiting_command(self):
        self.join()
        self.open_room()
        self.logic._next_directory_poll = 99
        self.logic.poll()
        self.take("get_directory")
        self.logic.send("Do not send after disconnect")
        self.logic.disconnect()
        self.assertIsNone(self.logic._queued_request)
        self.reply({"online_users": [self.alice], "groups": []})
        self.assertTrue(self.network.requests.empty())
        self.assertEqual(self.logic.state, "disconnecting")

    # Window close cancels a queued command before draining a late poll reply.
    def test_close_cancels_waiting_command(self):
        self.join()
        self.open_room()
        self.logic._next_directory_poll = 99
        self.logic.poll()
        self.take("get_directory")
        self.logic.send("Do not send after close")
        self.logic.close()
        self.assertIsNone(self.logic._queued_request)
        self.reply({"online_users": [self.alice], "groups": []})
        self.assertTrue(self.network.requests.empty())
        self.assertEqual(self.logic.state, "closing")

    def test_login_directory_and_ping_are_serialized_without_stacked_polls(self):
        self.join()
        self.assertEqual(self.logic.user, self.alice)
        self.assertIn(("directory", {"online_users": [self.alice], "groups": []}), self.events)
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

    def test_group_creation_refreshes_directory_before_initial_history(self):
        self.join()
        self.logic.create_group("  Study  ")
        self.assertEqual(self.take("create_group"), {"name": "Study"})
        group = {"room_key": "group:00000000-0000-4000-8000-000000000000", "kind": "group",
                 "name": "Study", "created_by_user_id": 1, "created_at": "2026-11-08T00:00:00Z",
                 "members": [self.alice]}
        self.reply({"room": group})
        self.assertEqual(self.take("get_directory"), {})
        self.assertIn(("group_created", None), self.events)
        self.assertIn(("room_title", "Group: Study"), self.events)
        self.reply({"online_users": [self.alice], "groups": [group]})
        request = self.take("sync_room")
        self.assertEqual((request["room_key"], request["latest_id"], request["old_count"]), (group["room_key"], None, 20))
        self.reply({**self.sync(), "room_key": group["room_key"]})
        self.assertEqual(self.logic.groups, {group["room_key"]: group})
        self.assertEqual(self.logic.latest_id, 0)

    def test_stale_group_join_refreshes_discovery_without_replacing_new_private_target(self):
        bob = {"id": 2, "username": "Bob"}
        group = {"room_key": "group:00000000-0000-4000-8000-000000000000", "kind": "group",
                 "name": "Study", "created_by_user_id": 2, "created_at": "2026-11-08T00:00:00Z", "members": [bob]}
        self.join([group])
        self.logic.open_group(group["room_key"])
        self.assertEqual(self.take("join_group"), {"room_key": group["room_key"]})
        self.logic.open_chat(3, "Carol")
        self.reply({"room": {**group, "members": [self.alice, bob]}, "joined": True})
        self.assertEqual(self.take("open_private"), {"target_user_id": 3})
        self.assertIsNone(self.logic.room)
        private = {"room_key": "private:1:3", "kind": "private", "name": None,
                   "members": [self.alice, {"id": 3, "username": "Carol"}]}
        self.reply({"room": private, "created": True})
        self.assertEqual(self.take("get_directory"), {})
        self.assertEqual(self.logic.room, private)
        self.assertEqual(self.logic.messages, {})

    def test_mutation_ack_stages_all_snapshot_pages_before_replacing_loaded_history(self):
        self.join()
        self.open_room(self.sync(older=[self.message(i) for i in range(86, 106)], oldest=86, latest=105, has_older=True))
        for first in (66, 46, 26, 6, 1):
            previous = self.logic.oldest_id
            self.logic.load_older()
            self.take("sync_room")
            self.reply(self.sync(older=[self.message(i) for i in range(first, previous)], oldest=first,
                                 latest=105, has_older=first > 1))
        self.logic.toggle_heart(1)
        self.assertEqual(self.take("set_heart")["hearted"], True)
        self.reply({"room_key": "private:1:2", "message_id": 1, "hearted_by_me": True,
                    "heart_count": 1, "room_revision": 2})
        self.assertEqual(self.logic.room_revision, 0)
        self.assertFalse(self.logic.messages[1]["hearted_by_me"])
        self.assertEqual(self.take("sync_room")["latest_id"], 105)
        self.reply({**self.sync(oldest=1, latest=105), "room_revision": 2, "reload_required": True})
        request = self.take("get_room_snapshot")
        self.assertEqual((request["from_id"], request["through_id"], request["room_revision"]), (1, 105, 2))
        self.events.clear()
        updated = {**self.message(1), "heart_count": 1, "hearted_by_me": True}
        page = {"room_key": "private:1:2", "room_revision": 2, "pinned_message": self.message(105),
                "messages": [updated] + [self.message(i) for i in range(2, 101)],
                "has_more": True, "next_after_id": 100}
        self.reply(page)
        self.assertEqual(self.take("get_room_snapshot")["after_id"], 100)
        self.assertFalse(self.logic.messages[1]["hearted_by_me"])
        self.assertIsNone(self.logic.pinned_message)
        self.assertFalse(any(kind == "messages" for kind, value in self.events))
        self.reply({**page, "messages": [self.message(i) for i in range(101, 106)], "has_more": False, "next_after_id": None})
        self.assertEqual(len(self.logic.messages), 105)
        self.assertTrue(self.logic.messages[1]["hearted_by_me"])
        self.assertEqual((self.logic.oldest_id, self.logic.latest_id, self.logic.room_revision), (1, 105, 2))
        self.assertEqual(self.logic.pinned_message["id"], 105)
        self.assertEqual(self.take("sync_room")["room_revision"], 2)
        self.reply({**self.sync(newer=[self.message(106)], oldest=1, latest=106),
                    "room_revision": 2, "pinned_message": self.message(105)})
        self.assertEqual(len(self.logic.messages), 106)

    def test_two_stale_snapshots_keep_cache_and_defer_retry_until_next_poll(self):
        self.join()
        self.open_room(self.sync(older=[self.message(1)], oldest=1, latest=1))
        self.clock.return_value = 101.0
        self.logic.poll()
        self.take("sync_room")
        self.reply({**self.sync(oldest=1, latest=1), "room_revision": 1, "reload_required": True})
        self.take("get_room_snapshot")
        self.reply(error={"code": "SNAPSHOT_STALE", "message": "Changed", "details": {"room_revision": 2}})
        self.assertEqual(self.take("get_room_snapshot")["room_revision"], 2)
        self.reply(error={"code": "SNAPSHOT_STALE", "message": "Changed", "details": {"room_revision": 3}})
        self.assertTrue(self.network.requests.empty())
        self.assertEqual(self.logic.room_revision, 0)
        self.assertEqual(set(self.logic.messages), {1})
        self.clock.return_value = 102.1
        self.logic.poll()
        self.assertEqual(self.take("sync_room")["room_revision"], 0)

    def test_switching_rooms_discards_snapshot_result_and_pin(self):
        self.join()
        self.open_room(self.sync(older=[self.message(1)], oldest=1, latest=1))
        self.clock.return_value = 101.0
        self.logic.poll()
        self.take("sync_room")
        self.reply({**self.sync(oldest=1, latest=1), "room_revision": 1, "reload_required": True})
        self.take("get_room_snapshot")
        self.logic.open_chat(3, "Carol")
        self.reply({"room_key": "private:1:2", "room_revision": 1, "messages": [self.message(1)],
                    "pinned_message": self.message(1), "has_more": False, "next_after_id": None})
        self.assertEqual(self.take("open_private"), {"target_user_id": 3})
        self.assertEqual(self.logic.messages, {})
        self.assertIsNone(self.logic.pinned_message)
        self.assertIsNone(self.logic._snapshot)

    def test_incomplete_snapshot_never_drops_existing_messages(self):
        self.join()
        self.open_room(self.sync(older=[self.message(1)], oldest=1, latest=1))
        self.clock.return_value = 101.0
        self.logic.poll()
        self.take("sync_room")
        self.reply({**self.sync(oldest=1, latest=1), "room_revision": 1, "reload_required": True})
        self.take("get_room_snapshot")
        self.reply({"room_key": "private:1:2", "room_revision": 1, "messages": [],
                    "pinned_message": None, "has_more": False, "next_after_id": None})
        self.assertEqual(set(self.logic.messages), {1})
        self.assertEqual(self.logic.room_revision, 0)
        self.assertTrue(self.network.requests.empty())

    def test_summary_status_poll_is_independent_and_result_arrives_only_via_sync(self):
        self.join()
        self.open_room(self.sync(older=[self.message(1)], oldest=1, latest=1))
        self.logic._next_directory_poll = self.logic._next_room_poll = 200
        job_id = "00000000-0000-4000-8000-000000000001"
        self.logic.request_summary()
        self.assertEqual(self.take("request_summary"), {"room_key": "private:1:2"})
        self.reply({"job_id": job_id, "state": "running"})
        self.assertEqual(set(self.logic.messages), {1})
        self.assertEqual(self.logic.state, "connected")
        self.clock.return_value = 104
        self.logic.poll()
        self.assertEqual(self.take("get_summary_status"), {"job_id": job_id})
        job = {"job_id": job_id, "room_key": "private:1:2", "state": "running"}
        self.reply({"job": job})
        self.assertTrue(self.network.requests.empty())
        self.clock.return_value = 108
        self.logic.poll()
        self.take("get_summary_status")
        self.reply({"job": {**job, "state": "succeeded", "result_message_id": 2}})
        self.assertEqual(set(self.logic.messages), {1})
        self.assertEqual(self.take("sync_room")["latest_id"], 1)
        ai = {**self.message(2), "sender_kind": "ai", "sender": None,
              "ai": {"model": "actual-model", "summary_job_id": job_id, "source_last_message_id": 1}}
        self.reply(self.sync(newer=[ai], oldest=1, latest=2))
        self.assertEqual(self.logic.messages[2]["sender_kind"], "ai")
        self.assertFalse(self.logic._summary_jobs)

    def test_summary_reply_after_room_switch_does_not_show_notice_in_new_room(self):
        self.join()
        self.open_room()
        job_id = "00000000-0000-4000-8000-000000000001"
        self.logic.request_summary()
        self.take("request_summary")
        self.logic.open_chat(3, "Carol")
        self.events.clear()
        self.reply({"job_id": job_id, "state": "running"})
        self.take("open_private")
        self.assertFalse(any(kind == "log" for kind, value in self.events))
        private = {"room_key": "private:1:3", "kind": "private", "name": None,
                   "members": [self.alice, {"id": 3, "username": "Carol"}]}
        self.reply({"room": private, "created": True})
        self.take("sync_room")
        self.reply({**self.sync(), "room_key": private["room_key"]})
        self.logic._next_directory_poll = self.logic._next_room_poll = 200
        self.clock.return_value = 104
        self.logic.poll()
        self.take("get_summary_status")
        self.events.clear()
        self.reply({"job": {"job_id": job_id, "room_key": "private:1:2", "state": "failed",
                            "error": {"code": "AI_FAILED", "message": "Old room failure"}}})
        self.assertFalse(any(kind == "log" for kind, value in self.events))
        self.assertEqual(self.logic.room, private)
        self.assertEqual(self.logic.messages, {})

    def test_status_busy_retries_without_losing_job_then_missing_job_stops_polling(self):
        self.join()
        self.open_room()
        self.logic._next_directory_poll = self.logic._next_room_poll = 200
        job_id = "00000000-0000-4000-8000-000000000001"
        self.logic.request_summary()
        self.take("request_summary")
        self.reply({"job_id": job_id, "state": "running"})
        self.clock.return_value = 104
        self.logic.poll()
        self.take("get_summary_status")
        self.reply(error={"code": "SERVER_BUSY", "message": "Busy"})
        self.assertIn(job_id, self.logic._summary_jobs)
        self.clock.return_value = 108
        self.logic.poll()
        self.take("get_summary_status")
        self.reply(error={"code": "SUMMARY_JOB_NOT_FOUND", "message": "Expired"})
        self.assertNotIn(job_id, self.logic._summary_jobs)
        self.assertTrue(self.network.requests.empty())

    def test_sound_plays_from_sync_once_for_requester_not_from_ack(self):
        self.join()
        self.open_room()
        self.events.clear()
        self.logic.trigger_effect()
        self.assertEqual(self.take("trigger_effect"), {"room_key": "private:1:2"})
        self.reply({"triggered": True})
        self.assertEqual(self.logic._effect_revision, 0)
        self.assertFalse(any(kind == "sound" for kind, value in self.events))
        self.take("sync_room")
        self.reply({**self.sync(), "effect_revision": 1, "effect": {"sound_id": "rimshot"}})
        self.assertEqual([value for kind, value in self.events if kind == "sound"], ["rimshot"])
        self.clock.return_value = 101.1
        self.logic.poll()
        self.take("sync_room")
        self.reply({**self.sync(), "effect_revision": 1, "effect": {"sound_id": "rimshot"}})
        self.assertEqual(sum(kind == "sound" for kind, value in self.events), 1)
        self.assertEqual(self.logic.messages, {})
        self.assertEqual((self.logic.latest_id, self.logic.room_revision), (0, 0))

    def test_initial_or_reopened_room_is_silent_and_revision_jumps_play_once(self):
        self.join()
        self.open_room({**self.sync(), "effect_revision": 7, "effect": {"sound_id": "rimshot"}})
        self.assertFalse(any(kind == "sound" for kind, value in self.events))
        self.open_room({**self.sync(), "effect_revision": 8, "effect": {"sound_id": "rimshot"}})
        self.assertFalse(any(kind == "sound" for kind, value in self.events))
        self.clock.return_value = 101.1
        self.logic.poll()
        self.take("sync_room")
        self.reply({**self.sync(), "effect_revision": 11, "effect": {"sound_id": "rimshot"}})
        self.assertEqual(sum(kind == "sound" for kind, value in self.events), 1)
        self.assertEqual(self.logic._effect_revision, 11)

    def test_sound_baseline_survives_heart_pin_snapshot_reload(self):
        self.join()
        self.open_room()
        self.clock.return_value = 101.1
        self.logic.poll()
        self.take("sync_room")
        self.reply({**self.sync(), "room_revision": 1, "reload_required": True,
                    "effect_revision": 2, "effect": {"sound_id": "rimshot"}})
        self.take("get_room_snapshot")
        self.assertEqual(sum(kind == "sound" for kind, value in self.events), 1)
        self.reply({"room_key": "private:1:2", "room_revision": 1, "messages": [],
                    "pinned_message": None, "has_more": False, "next_after_id": None})
        self.take("sync_room")
        self.reply({**self.sync(), "room_revision": 1, "effect_revision": 2, "effect": {"sound_id": "rimshot"}})
        self.assertEqual(sum(kind == "sound" for kind, value in self.events), 1)
        self.assertEqual(self.logic._effect_revision, 2)

    def test_old_room_sound_response_is_discarded_and_new_room_sets_baseline(self):
        self.join()
        self.open_room()
        self.clock.return_value = 101.1
        self.logic.poll()
        self.take("sync_room")
        self.logic.open_chat(3, "Carol")
        self.reply({**self.sync(), "effect_revision": 99, "effect": {"sound_id": "rimshot"}})
        self.take("open_private")
        self.assertIsNone(self.logic._effect_revision)
        self.assertFalse(any(kind == "sound" for kind, value in self.events))
        self.reply({"room": {"room_key": "private:1:3", "kind": "private", "name": None,
                             "members": [self.alice, {"id": 3, "username": "Carol"}]}, "created": True})
        self.take("sync_room")
        self.reply({**self.sync(), "room_key": "private:1:3", "effect_revision": 50, "effect": {"sound_id": "rimshot"}})
        self.assertEqual(self.logic._effect_revision, 50)
        self.assertFalse(any(kind == "sound" for kind, value in self.events))

    def test_failed_trigger_does_not_play_or_change_baseline(self):
        self.join()
        self.open_room()
        self.logic.trigger_effect()
        self.take("trigger_effect")
        self.reply(error={"code": "SERVER_BUSY", "message": "Busy"})
        self.assertEqual(self.logic._effect_revision, 0)
        self.assertFalse(any(kind == "sound" for kind, value in self.events))
        self.assertEqual(self.logic.state, "connected")
