"""Tkinter interaction tests; skip when a graphical display is unavailable."""

import gc
import threading
import time
import tkinter as tk
import unittest
from unittest.mock import Mock, patch

import client
from client import ClientWindow
from common.protocol import error_response
from server import ChatServer, ServerConfig
from test_chat import ChatPeer


class ClientUITests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        # Collect destroyed Tk windows on this thread before later network tests
        # can trigger cyclic GC from a worker and run Tk finalizers off-thread.
        if gc.isenabled():
            cls.addClassCleanup(gc.enable)
        gc.disable()
        cls.addClassCleanup(gc.collect)
        try:
            root = tk.Tk()
        except tk.TclError as exc:
            raise unittest.SkipTest(f"Tk display unavailable: {exc}")
        root.withdraw()
        root.destroy()

    def window(self, server=None, username="Alice"):
        window = ClientWindow(*(server.address if server else ("127.0.0.1", 5000)))
        window.username.set(username)
        window.withdraw()
        self.addCleanup(self.close_window, window)
        return window

    def close_window(self, window):
        if window.logic.network is not None:
            window.logic.network.stop()
            window.logic.network.join(3)
            self.assertFalse(window.logic.network.is_alive())
            window.logic.network = None
        try:
            window.close()
        except tk.TclError:
            pass

    def server(self, server_class=ChatServer):
        server = server_class(ServerConfig(host="127.0.0.1", port=0, database_path=":memory:"))
        server.start()
        self.addCleanup(server.stop)
        return server

    def pump_until(self, window, predicate):
        deadline = time.monotonic() + 3
        while not predicate() and time.monotonic() < deadline:
            window.update()
            time.sleep(0.005)
        self.assertTrue(predicate(), "UI did not reach the expected state")

    def raw_peer(self, server, username):
        peer = ChatPeer(server.address, username)
        self.addCleanup(peer.close)
        return peer

    def open_room(self, window, target_id):
        key = f"user-{target_id}"
        self.pump_until(window, lambda: window.online_users.exists(key))
        window.online_users.selection_set(key)
        window.open_button.invoke()
        self.pump_until(window, lambda: window.logic.room is not None and window.logic.latest_id is not None
                        and window.logic.state == "connected")

    def test_invalid_address_does_not_start_a_worker(self):
        window = self.window()
        window.port.set("0")
        window.connect_button.invoke()
        self.assertIsNone(window.logic.network)
        self.assertIn("between 1 and 65535", window.status.get())
        self.assertEqual(window.host.get(), "127.0.0.1")
        self.assertTrue(window.ping_button.instate(["disabled"]))

    def test_connect_ping_disconnect_and_reconnect(self):
        window = self.window(self.server())
        users = []
        for _ in range(2):
            window.connect_button.invoke()
            self.assertTrue(window.connect_button.instate(["disabled"]))
            self.pump_until(window, lambda: window.logic.state == "connected")
            self.assertTrue(window.host_entry.instate(["readonly"]))
            self.assertTrue(window.username_entry.instate(["readonly"]))
            self.assertEqual(window.status.get(), "Joined as Alice")
            users.append(window.logic.user)
            window.ping_button.invoke()
            self.assertTrue(window.ping_button.instate(["disabled"]))
            self.pump_until(window, lambda: window.logic.state == "connected")
            self.assertIn("{'pong': True}", window.activity.get("1.0", "end"))
            worker = window.logic.network
            window.disconnect_button.invoke()
            self.pump_until(window, lambda: window.logic.network is None)
            self.assertFalse(worker.is_alive())
            self.assertFalse(window.connect_button.instate(["disabled"]))
            self.assertIsNone(window.logic.user)
        self.assertEqual(users[0], users[1])

    def test_invalid_username_does_not_start_a_worker(self):
        window = self.window()
        window.username.set("invalid name")
        window.connect_button.invoke()
        self.assertIsNone(window.logic.network)
        self.assertIn("Username must", window.status.get())

    def test_rejected_login_returns_to_the_join_form(self):
        class RejectingServer(ChatServer):
            def _handle_request(self, request, session):
                return error_response(request.request_id, "INVALID_PAYLOAD", "Name rejected")

        window = self.window(self.server(RejectingServer))
        window.connect()
        self.pump_until(window, lambda: window.logic.network is None)
        self.assertIsNone(window.logic.user)
        self.assertIn("Name rejected", window.activity.get("1.0", "end"))
        self.assertFalse(window.username_entry.instate(["readonly"]))

    def test_close_during_join_stops_the_network_worker(self):
        entered = threading.Event()
        release = threading.Event()

        class JoiningServer(ChatServer):
            def _handle_request(self, request, session):
                entered.set()
                release.wait(3)
                return super()._handle_request(request, session)

        window = self.window(self.server(JoiningServer))
        try:
            window.connect()
            self.pump_until(window, entered.is_set)
            self.assertEqual(window.logic.state, "joining")
            worker = window.logic.network
            window.close()
            self.pump_until(window, lambda: window.logic.network is None)
            self.assertFalse(worker.is_alive())
        finally:
            release.set()

    def test_ui_remains_responsive_and_closes_while_waiting(self):
        entered = threading.Event()
        release = threading.Event()

        class WaitingServer(ChatServer):
            def _handle_request(self, request, session):
                if request.action == "ping":
                    entered.set()
                    release.wait(3)
                return super()._handle_request(request, session)

        window = self.window(self.server(WaitingServer))
        try:
            window.connect()
            self.pump_until(window, lambda: window.logic.state == "connected")
            window.ping()
            self.pump_until(window, entered.is_set)
            heartbeat = threading.Event()
            window.after(10, heartbeat.set)
            self.pump_until(window, heartbeat.is_set)
            self.assertEqual(window.logic.state, "waiting")
            window.logic._next_directory_poll = time.monotonic() - 1
            window.logic._poll_directory()
            self.assertTrue(window.logic.network.requests.empty())
            worker = window.logic.network
            window.close()
            self.pump_until(window, lambda: window.logic.network is None)
            self.assertFalse(worker.is_alive())
        finally:
            release.set()

    def test_terminal_interrupt_stops_the_network_worker(self):
        window = self.window(self.server())
        window.connect()
        self.pump_until(window, lambda: window.logic.state == "connected")
        worker = window.logic.network
        with patch("sys.argv", ["client.py"]), patch("client.ClientWindow", return_value=window), \
                patch.object(window, "mainloop", side_effect=KeyboardInterrupt):
            client.main()
        self.assertFalse(worker.is_alive())
        self.assertIsNone(window.logic.network)

    def test_directory_poll_updates_two_windows_and_preserves_selection(self):
        with patch("client_logic.DIRECTORY_POLL_INTERVAL", 0.05):
            server = self.server()
            alice = self.window(server)
            bob = self.window(server, "Bob")
            alice.connect()
            self.pump_until(alice, lambda: len(alice.online_users.get_children()) == 1)
            bob.connect()
            self.pump_until(alice, lambda: len(alice.online_users.get_children()) == 2
                            and len(bob.online_users.get_children()) == 2)
            texts = [alice.online_users.item(key, "text") for key in alice.online_users.get_children()]
            self.assertEqual(texts, ["Alice (you)", "Bob"])
            self.assertEqual(alice.directory_status.get(), "Online users (2)")
            selected = f"user-{bob.logic.user['id']}"
            alice.online_users.selection_set(selected)
            alice.logic._next_directory_poll = time.monotonic() - 1
            self.pump_until(alice, lambda: alice.logic.state == "refreshing")
            self.pump_until(alice, lambda: alice.logic.state == "connected")
            self.assertEqual(alice.online_users.selection(), (selected,))
            bob.disconnect()
            self.pump_until(alice, lambda: bob.logic.network is None and len(alice.online_users.get_children()) == 1)
            self.assertEqual(bob.online_users.get_children(), ())
            self.assertEqual(alice.online_users.item(alice.online_users.get_children()[0], "text"), "Alice (you)")

    # Routine directory/history checks must not replace the joined status with
    # loading text; explicit initial/older history requests still show progress.
    def test_background_polls_keep_status_stable_without_redrawing_history(self):
        server = self.server()
        bob = self.raw_peer(server, "Bob")
        window = self.window(server)
        window.connect()
        self.pump_until(window, lambda: window.logic.state == "connected")
        self.open_room(window, bob.user["id"])
        expected = "Joined as Alice"
        window.logic._next_directory_poll = time.monotonic() - 1
        window.logic._poll_directory()
        self.assertEqual(window.logic.state, "refreshing")
        self.assertEqual(window.status.get(), expected)
        for button in (window.ping_button, window.send_button, window.create_group_button,
                       window.summary_button, window.sound_button):
            self.assertFalse(button.instate(["disabled"]))
        self.pump_until(window, lambda: window.logic.state == "connected")
        with patch.object(window, "_render_messages", wraps=window._render_messages) as render:
            window.logic._sync_room(0)
            self.assertEqual(window.status.get(), expected)
            self.assertFalse(window.send_button.instate(["disabled"]))
            self.pump_until(window, lambda: window.logic.state == "connected")
            render.assert_not_called()
        window.logic._sync_room(20)
        self.assertEqual(window.status.get(), "Loading messages...")
        self.pump_until(window, lambda: window.logic.state == "connected")

    def test_slow_directory_does_not_stack_polls_or_block_window_close(self):
        entered = threading.Event()
        release = threading.Event()

        class SlowDirectoryServer(ChatServer):
            def _handle_request(self, request, session):
                if request.action == "get_directory":
                    entered.set()
                    release.wait(3)
                return super()._handle_request(request, session)

        window = self.window(self.server(SlowDirectoryServer))
        try:
            window.connect()
            self.pump_until(window, entered.is_set)
            self.assertEqual(window.logic.state, "refreshing")
            self.assertFalse(window.ping_button.instate(["disabled"]))
            window.logic._next_directory_poll = time.monotonic() - 1
            for _ in range(3):
                window.logic._poll_directory()
            self.assertTrue(window.logic.network.requests.empty())
            heartbeat = threading.Event()
            window.after(10, heartbeat.set)
            self.pump_until(window, heartbeat.is_set)
            window.close()
            self.pump_until(window, lambda: window.logic.network is None)
        finally:
            release.set()

    # A delayed poll leaves real buttons enabled. Clicking Send waits without
    # blocking Tk, then dispatches exactly once after the poll response arrives.
    def test_send_button_queues_behind_slow_room_poll(self):
        entered, release = threading.Event(), threading.Event()

        class SlowPollServer(ChatServer):
            pause = False

            def _handle_request(self, request, session):
                if request.action == "sync_room" and self.pause:
                    self.pause = False
                    entered.set()
                    release.wait(3)
                return super()._handle_request(request, session)

        server = self.server(SlowPollServer)
        alice, bob = self.raw_peer(server, "Alice"), self.raw_peer(server, "Bob")
        room = alice.ok("open_private", {"target_user_id": bob.user["id"]})["room"]["room_key"]
        for index in range(21):
            alice.ok("send_message", {"room_key": room, "content": f"History {index}"})
        window = self.window(server)
        window.connect()
        self.open_room(window, bob.user["id"])
        window._select_message(window.logic.latest_id)
        window.message_input.insert("1.0", "Sent during poll")
        server.pause = True
        window.logic._next_room_poll = time.monotonic() - 1
        try:
            self.pump_until(window, entered.is_set)
            for button in (window.ping_button, window.send_button, window.heart_button,
                           window.pin_button, window.older_button, window.summary_button,
                           window.sound_button, window.create_group_button):
                self.assertFalse(button.instate(["disabled"]))
            self.assertEqual(window.status.get(), "Joined as Alice")
            window.send_button.invoke()
            self.assertEqual(window.logic._pending_action, "sync_room")
            self.assertEqual(window.logic.display_state, "sending")
            self.assertTrue(window.logic.network.requests.empty())
            self.assertTrue(window.send_button.instate(["disabled"]))
            heartbeat = threading.Event()
            window.after(10, heartbeat.set)
            self.pump_until(window, heartbeat.is_set)
            release.set()
            self.pump_until(window, lambda: window.message_input.get("1.0", "end-1c") == ""
                            and len(window.logic.messages) == 21)
            self.assertEqual(sum(message["content"] == "Sent during poll"
                                 for message in window.logic.messages.values()), 1)
        finally:
            release.set()

    def test_two_windows_exchange_private_messages_without_duplicates(self):
        # Shortened intervals exercise alternating directory and room polls.
        with patch("client_logic.DIRECTORY_POLL_INTERVAL", 0.2), patch("client_logic.ROOM_POLL_INTERVAL", 0.1):
            server = self.server()
            alice, bob = self.window(server), self.window(server, "Bob")
            alice.connect()
            bob.connect()
            self.pump_until(alice, lambda: alice.logic.user is not None and bob.logic.user is not None)
            self.open_room(alice, bob.logic.user["id"])
            self.open_room(bob, alice.logic.user["id"])
            self.assertEqual(alice.logic.room["room_key"], bob.logic.room["room_key"])
            self.pump_until(alice, lambda: not alice.send_button.instate(["disabled"]))
            alice.message_input.insert("1.0", "Hello Bob")
            alice.send_button.invoke()
            self.pump_until(alice, lambda: len(alice.logic.messages) == 1 and len(bob.logic.messages) == 1)
            self.assertIn("Alice: Hello Bob", alice.chat.get("1.0", "end"))
            self.assertIn("Alice: Hello Bob", bob.chat.get("1.0", "end"))
            self.assertEqual(alice.message_input.get("1.0", "end-1c"), "")
            self.assertEqual(set(alice.logic.messages), set(bob.logic.messages))

    def test_history_loads_older_and_room_switch_discards_other_cache(self):
        server = self.server()
        raw_alice, raw_bob, raw_carol = [self.raw_peer(server, name) for name in ["Alice", "Bob", "Carol"]]
        ab = raw_alice.ok("open_private", {"target_user_id": raw_bob.user["id"]})["room"]["room_key"]
        ac = raw_alice.ok("open_private", {"target_user_id": raw_carol.user["id"]})["room"]["room_key"]
        for index in range(35):
            raw_alice.ok("send_message", {"room_key": ab, "content": f"Bob history {index}"})
        raw_carol.ok("send_message", {"room_key": ac, "content": "Carol only"})
        window = self.window(server)
        window.connect()
        self.open_room(window, raw_bob.user["id"])
        self.assertEqual(len(window.logic.messages), 20)
        window.older_button.invoke()
        self.pump_until(window, lambda: len(window.logic.messages) == 35 and window.logic.state == "connected")
        self.assertFalse(window.logic.has_more_older)
        self.open_room(window, raw_carol.user["id"])
        self.assertEqual([message["content"] for message in window.logic.messages.values()], ["Carol only"])
        self.assertNotIn("Bob history", window.chat.get("1.0", "end"))
        self.open_room(window, raw_bob.user["id"])
        self.assertEqual(len(window.logic.messages), 20)

    def test_old_room_response_is_discarded_when_switching_during_sync(self):
        entered, release = threading.Event(), threading.Event()

        class DelayedRoomServer(ChatServer):
            delayed_room = None

            def _handle_request(self, request, session):
                if request.action == "sync_room" and request.payload["room_key"] == self.delayed_room:
                    self.delayed_room = None
                    entered.set()
                    release.wait(3)
                return super()._handle_request(request, session)

        server = self.server(DelayedRoomServer)
        raw_alice, raw_bob, raw_carol = [self.raw_peer(server, name) for name in ["Alice", "Bob", "Carol"]]
        ab = raw_alice.ok("open_private", {"target_user_id": raw_bob.user["id"]})["room"]["room_key"]
        ac = raw_alice.ok("open_private", {"target_user_id": raw_carol.user["id"]})["room"]["room_key"]
        raw_bob.ok("send_message", {"room_key": ab, "content": "Old Bob room"})
        raw_carol.ok("send_message", {"room_key": ac, "content": "Current Carol room"})
        server.delayed_room = ab
        window = self.window(server)
        try:
            window.connect()
            self.pump_until(window, lambda: len(window.online_users.get_children()) == 3)
            window.online_users.selection_set(f"user-{raw_bob.user['id']}")
            window.open_chat()
            self.pump_until(window, entered.is_set)
            window.online_users.selection_set(f"user-{raw_carol.user['id']}")
            window.open_chat()
            release.set()
            self.pump_until(window, lambda: window.logic.room is not None and window.logic.room["room_key"] == ac
                            and len(window.logic.messages) == 1)
            self.assertIn("Current Carol room", window.chat.get("1.0", "end"))
            self.assertNotIn("Old Bob room", window.chat.get("1.0", "end"))
        finally:
            release.set()

    def test_send_ack_does_not_skip_an_earlier_unseen_message(self):
        entered, release = threading.Event(), threading.Event()

        class DelayedSyncServer(ChatServer):
            pause_sync = False

            def _handle_request(self, request, session):
                if request.action == "sync_room" and self.pause_sync:
                    self.pause_sync = False
                    entered.set()
                    release.wait(3)
                return super()._handle_request(request, session)

        server = self.server(DelayedSyncServer)
        bob = self.raw_peer(server, "Bob")
        window = self.window(server)
        try:
            window.connect()
            self.open_room(window, bob.user["id"])
            window.logic._next_room_poll = time.monotonic() + 60
            key = window.logic.room["room_key"]
            bob.ok("send_message", {"room_key": key, "content": "Earlier unseen message"})
            server.pause_sync = True
            window.message_input.insert("1.0", "My own message")
            window.send()
            self.pump_until(window, entered.is_set)
            self.assertEqual(window.logic.latest_id, 0)
            self.assertEqual(window.logic.messages, {})
            release.set()
            self.pump_until(window, lambda: len(window.logic.messages) == 2)
            self.assertEqual([m["content"] for m in window.logic.messages.values()],
                             ["Earlier unseen message", "My own message"])
        finally:
            release.set()

    def test_groups_show_members_before_join_and_two_windows_exchange_messages(self):
        with patch("client_logic.DIRECTORY_POLL_INTERVAL", 0.2), patch("client_logic.ROOM_POLL_INTERVAL", 0.1):
            server = self.server()
            alice, bob = self.window(server), self.window(server, "Bob")
            alice.connect()
            bob.connect()
            self.pump_until(alice, lambda: alice.logic.state == "connected" and bob.logic.user is not None)
            alice.group_name.set("  Study  ")
            alice.create_group_button.invoke()
            self.pump_until(alice, lambda: len(alice.groups.get_children()) == 1 and len(bob.groups.get_children()) == 1)
            key = alice.groups.get_children()[0]
            self.assertEqual(alice.group_name.get(), "")
            bob.groups.selection_set(key)
            self.pump_until(bob, lambda: "Alice" in bob.group_members.get("1.0", "end"))
            self.assertEqual(bob.logic.groups[key]["members"], [alice.logic.user])
            self.assertIsNone(bob.logic.room)
            self.assertEqual(bob.logic.messages, {})
            bob.join_group_button.invoke()
            self.pump_until(bob, lambda: bob.logic.room is not None and bob.logic.latest_id == 0
                            and not bob.send_button.instate(["disabled"]))
            self.assertEqual(bob.room_title.get(), "Group: Study")
            bob.message_input.insert("1.0", "Hello group")
            bob.send_button.invoke()
            self.pump_until(alice, lambda: len(alice.logic.messages) == 1 and len(bob.logic.messages) == 1
                            and len(alice.logic.groups[key]["members"]) == 2)
            self.assertIn("Bob: Hello group", alice.chat.get("1.0", "end"))
            self.assertIn("Bob: Hello group", bob.chat.get("1.0", "end"))
            self.assertEqual(alice.groups.selection(), (key,))
            self.assertIn("Bob", alice.group_members.get("1.0", "end"))
            self.assertEqual(bob.groups.item(key, "text"), "Study (joined)")
            bob.disconnect()
            self.pump_until(bob, lambda: bob.logic.network is None)
            self.assertEqual(bob.groups.get_children(), ())

    def test_group_history_pages_and_switching_to_private_room_keep_caches_separate(self):
        server = self.server()
        alice, bob = self.raw_peer(server, "Alice"), self.raw_peer(server, "Bob")
        group = alice.ok("create_group", {"name": "History"})["room"]["room_key"]
        for index in range(35):
            alice.ok("send_message", {"room_key": group, "content": f"Group history {index}"})
        private = alice.ok("open_private", {"target_user_id": bob.user["id"]})["room"]["room_key"]
        bob.ok("send_message", {"room_key": private, "content": "Private only"})
        window = self.window(server, "Bob")
        window.connect()
        self.pump_until(window, lambda: window.groups.exists(group))
        window.groups.selection_set(group)
        window.join_group_button.invoke()
        self.pump_until(window, lambda: len(window.logic.messages) == 20 and window.logic.state == "connected")
        window.older_button.invoke()
        self.pump_until(window, lambda: len(window.logic.messages) == 35 and window.logic.state == "connected")
        self.open_room(window, alice.user["id"])
        self.assertEqual([message["content"] for message in window.logic.messages.values()], ["Private only"])
        self.assertNotIn("Group history", window.chat.get("1.0", "end"))
        window.groups.selection_set(group)
        window.join_group_button.invoke()
        self.pump_until(window, lambda: len(window.logic.messages) == 20 and window.logic.state == "connected")
        self.assertNotIn("Private only", window.chat.get("1.0", "end"))

    def test_two_windows_share_hearts_and_allow_any_member_to_replace_or_remove_pin(self):
        with patch("client_logic.DIRECTORY_POLL_INTERVAL", 0.3), patch("client_logic.ROOM_POLL_INTERVAL", 0.15):
            server = self.server()
            raw_alice, raw_bob = self.raw_peer(server, "Alice"), self.raw_peer(server, "Bob")
            key = raw_alice.ok("open_private", {"target_user_id": raw_bob.user["id"]})["room"]["room_key"]
            first, second = [raw_alice.ok("send_message", {"room_key": key, "content": text})["message"]["id"]
                             for text in ("First", "Second")]
            alice, bob = self.window(server), self.window(server, "Bob")
            alice.connect()
            bob.connect()
            self.open_room(alice, raw_bob.user["id"])
            self.open_room(bob, raw_alice.user["id"])
            # Enter selects the message under the read-only text cursor.
            alice.chat.mark_set("insert", alice.chat.tag_ranges(f"message-{first}")[0])
            alice._select_at_cursor()
            self.pump_until(alice, lambda: not alice.heart_button.instate(["disabled"]))
            alice.heart_button.invoke()
            self.pump_until(alice, lambda: alice.logic.messages[first]["hearted_by_me"]
                            and bob.logic.messages[first]["heart_count"] == 1)
            self.assertFalse(bob.logic.messages[first]["hearted_by_me"])
            self.assertEqual(alice.heart_button.cget("text"), "Unheart")
            bob._select_message(second)
            self.pump_until(bob, lambda: not bob.pin_button.instate(["disabled"]))
            bob.pin_button.invoke()
            self.pump_until(alice, lambda: alice.logic.pinned_message is not None and bob.logic.pinned_message is not None
                            and alice.logic.pinned_message["id"] == second and bob.logic.pinned_message["id"] == second)
            self.assertIn("Second", alice.pin_text.get("1.0", "end"))
            alice._select_message(first)
            self.pump_until(alice, lambda: not alice.pin_button.instate(["disabled"]))
            alice.pin_button.invoke()
            self.pump_until(bob, lambda: bob.logic.pinned_message["id"] == first and alice.logic.pinned_message["id"] == first)
            self.pump_until(bob, lambda: not bob.unpin_button.instate(["disabled"]))
            bob.unpin_button.invoke()
            self.pump_until(alice, lambda: alice.logic.pinned_message is None and bob.logic.pinned_message is None)
            self.assertEqual(alice.pin_frame.winfo_manager(), "")
            self.pump_until(alice, lambda: not alice.heart_button.instate(["disabled"]))
            alice.heart_button.invoke()
            self.pump_until(alice, lambda: alice.logic.messages[first]["heart_count"] == 0
                            and bob.logic.messages[first]["heart_count"] == 0)
            self.assertEqual(len(alice.logic.messages), 2)
            self.assertEqual(alice._selected_message_id, first)

    def test_snapshot_preserves_loaded_history_scroll_and_pin_outside_initial_page(self):
        server = self.server()
        raw_alice, raw_bob = self.raw_peer(server, "Alice"), self.raw_peer(server, "Bob")
        key = raw_alice.ok("open_private", {"target_user_id": raw_bob.user["id"]})["room"]["room_key"]
        ids = [raw_alice.ok("send_message", {"room_key": key, "content": f"History {i}"})["message"]["id"] for i in range(145)]
        raw_bob.ok("set_pin", {"room_key": key, "message_id": ids[0]})
        window = self.window(server)
        window.deiconify()
        window.geometry("880x600")
        window.connect()
        self.open_room(window, raw_bob.user["id"])
        self.assertNotIn(ids[0], window.logic.messages)
        self.assertIn("History 0", window.pin_text.get("1.0", "end"))
        while window.logic.has_more_older:
            self.pump_until(window, lambda: not window.older_button.instate(["disabled"]))
            previous = len(window.logic.messages)
            window.older_button.invoke()
            self.pump_until(window, lambda: len(window.logic.messages) > previous and window.logic.state == "connected")
        self.assertEqual(len(window.logic.messages), 145)
        window.logic._next_room_poll = time.monotonic() + 60
        window.chat.yview("50.0")
        window.update_idletasks()
        anchor = window.chat.index("@0,0")
        raw_bob.ok("set_heart", {"room_key": key, "message_id": ids[30], "hearted": True})
        raw_bob.ok("set_pin", {"room_key": key, "message_id": ids[1]})
        window.logic._next_room_poll = time.monotonic()
        self.pump_until(window, lambda: window.logic.room_revision == 3 and window.logic._snapshot is None)
        window.update_idletasks()
        self.assertEqual(window.chat.index("@0,0"), anchor)
        self.assertEqual(set(window.logic.messages), set(ids))
        self.assertEqual(window.logic.pinned_message["id"], ids[1])
        self.assertEqual(window.logic.messages[ids[30]]["heart_count"], 1)
        self.assertFalse(window.logic.messages[ids[30]]["hearted_by_me"])
        for widget in (window.pin_frame, window.unpin_button, window.heart_button, window.pin_button, window.send_button):
            bottom = widget.winfo_rooty() - window.winfo_rooty() + widget.winfo_height()
            self.assertLessEqual(bottom, window.winfo_height())

    def test_summary_jobs_keep_chat_responsive_and_both_windows_receive_ai_messages(self):
        from server_ai import AIConfig
        started = [threading.Event(), threading.Event()]
        release = threading.Event()
        lock = threading.Lock()
        calls = []
        def generate(config, source):
            with lock:
                index = len(calls)
                calls.append(source)
            started[index].set()
            release.wait(5)
            return f"Mocked summary {index + 1}", "actual-model"
        class AIServer(ChatServer):
            def __init__(self, config):
                super().__init__(config, ai_config=AIConfig("https://provider.example/v1", "test-model", "dummy"))
        with patch("server.generate_summary", side_effect=generate), patch("client_logic.DIRECTORY_POLL_INTERVAL", 0.3), \
                patch("client_logic.ROOM_POLL_INTERVAL", 0.15), patch("client_logic.SUMMARY_POLL_INTERVAL", 0.4):
            server = self.server(AIServer)
            raw_alice, raw_bob = self.raw_peer(server, "Alice"), self.raw_peer(server, "Bob")
            key = raw_alice.ok("open_private", {"target_user_id": raw_bob.user["id"]})["room"]["room_key"]
            raw_alice.ok("send_message", {"room_key": key, "content": "Please plan the meeting"})
            alice, bob = self.window(server), self.window(server, "Bob")
            alice.connect()
            bob.connect()
            self.open_room(alice, raw_bob.user["id"])
            self.open_room(bob, raw_alice.user["id"])
            try:
                for index in range(2):
                    self.pump_until(alice, lambda: not alice.summary_button.instate(["disabled"]))
                    alice.summary_button.invoke()
                    self.pump_until(alice, started[index].is_set)
                raw_bob.ok("send_message", {"room_key": key, "content": "While AI runs"})
                self.pump_until(alice, lambda: len(alice.logic.messages) == 2 and len(bob.logic.messages) == 2)
            finally:
                release.set()
            self.pump_until(alice, lambda: sum(m["sender_kind"] == "ai" for m in alice.logic.messages.values()) == 2
                            and sum(m["sender_kind"] == "ai" for m in bob.logic.messages.values()) == 2)
            self.assertIn("AI: Mocked summary", alice.chat.get("1.0", "end"))
            ai = next(m for m in alice.logic.messages.values() if m["sender_kind"] == "ai")
            self.assertIsNone(ai["sender"])
            self.assertEqual(ai["ai"]["model"], "actual-model")
            alice._select_message(ai["id"])
            self.pump_until(alice, lambda: not alice.heart_button.instate(["disabled"]))
            alice.heart_button.invoke()
            self.pump_until(bob, lambda: bob.logic.messages[ai["id"]]["heart_count"] == 1)
            self.pump_until(alice, lambda: not alice.pin_button.instate(["disabled"]))
            alice.pin_button.invoke()
            self.pump_until(bob, lambda: bob.logic.pinned_message is not None)
            self.assertIn("Pinned - AI:", bob.pin_text.get("1.0", "end"))
            alice.deiconify()
            alice.geometry("880x600")
            alice.update_idletasks()
            for widget in (alice.summary_button, alice.heart_button, alice.pin_button, alice.unpin_button, alice.send_button):
                x = widget.winfo_rootx() - alice.winfo_rootx()
                y = widget.winfo_rooty() - alice.winfo_rooty()
                self.assertGreaterEqual(x, 0)
                self.assertGreaterEqual(y, 0)
                self.assertLessEqual(x + widget.winfo_width(), alice.winfo_width())
                self.assertLessEqual(y + widget.winfo_height(), alice.winfo_height())


    def test_room_sound_reaches_both_windows_once_and_late_open_is_silent(self):
        audio = [Mock(), Mock(), Mock()]
        for player in audio:
            player.poll.return_value = None
        with patch("client.SoundPlayer", side_effect=audio), patch("client_logic.DIRECTORY_POLL_INTERVAL", 0.3), \
                patch("client_logic.ROOM_POLL_INTERVAL", 0.15):
            server = self.server()
            raw_bob, raw_carol = self.raw_peer(server, "Bob"), self.raw_peer(server, "Carol")
            alice, bob = self.window(server), self.window(server, "Bob")
            alice.connect()
            bob.connect()
            self.open_room(alice, raw_bob.user["id"])
            self.open_room(bob, alice.logic.user["id"])
            key = alice.logic.room["room_key"]
            self.pump_until(alice, lambda: not alice.sound_button.instate(["disabled"]))
            alice.sound_button.invoke()
            self.pump_until(alice, lambda: audio[0].play.call_count == 1 and audio[1].play.call_count == 1)
            heartbeat = threading.Event()
            alice.after(400, heartbeat.set)
            self.pump_until(alice, heartbeat.is_set)
            self.assertEqual((audio[0].play.call_count, audio[1].play.call_count), (1, 1))
            late = self.window(server, "Bob")
            late.connect()
            self.open_room(late, alice.logic.user["id"])
            self.assertEqual(late.logic._effect_revision, 1)
            audio[2].play.assert_not_called()
            previous_stops = audio[0].stop.call_count
            self.open_room(alice, raw_carol.user["id"])
            self.assertGreater(audio[0].stop.call_count, previous_stops)
            raw_bob.ok("trigger_effect", {"room_key": key})
            self.pump_until(bob, lambda: audio[1].play.call_count == 2 and audio[2].play.call_count == 1)
            self.assertEqual(audio[0].play.call_count, 1)
            self.assertEqual(alice.logic._effect_revision, 0)
            self.assertEqual(alice.logic.messages, {})

    def test_sound_error_keeps_chat_responsive_and_close_cleans_up_player(self):
        audio = Mock()
        audio.poll.return_value = None
        audio.play.side_effect = RuntimeError("Cannot play sound: install FFmpeg with ffplay available on PATH.")
        with patch("client.SoundPlayer", return_value=audio), patch("client_logic.ROOM_POLL_INTERVAL", 0.15):
            server = self.server()
            bob = self.raw_peer(server, "Bob")
            window = self.window(server)
            window.deiconify()
            window.geometry("880x600")
            window.connect()
            self.open_room(window, bob.user["id"])
            bob.ok("trigger_effect", {"room_key": window.logic.room["room_key"]})
            self.pump_until(window, lambda: "install FFmpeg" in window.activity.get("1.0", "end"))
            heartbeat = threading.Event()
            window.after(400, heartbeat.set)
            self.pump_until(window, heartbeat.is_set)
            self.assertEqual(audio.play.call_count, 1)
            self.assertTrue(window.logic.network.is_alive())
            window.update_idletasks()
            for widget in (window.sound_button, window.summary_button, window.send_button):
                x = widget.winfo_rootx() - window.winfo_rootx()
                y = widget.winfo_rooty() - window.winfo_rooty()
                self.assertLessEqual(x + widget.winfo_width(), window.winfo_width())
                self.assertLessEqual(y + widget.winfo_height(), window.winfo_height())
            worker = window.logic.network
            window.close()
            self.pump_until(window, lambda: window.logic.network is None)
            self.assertFalse(worker.is_alive())
            audio.close.assert_called()
