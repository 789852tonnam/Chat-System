"""Wire-format regression tests without a running server."""

import json
import copy
import unittest

from common.protocol import (
    MAX_REQUEST_ID,
    FrameTooLarge,
    LineFramer,
    ProtocolError,
    decode_document,
    encode_message,
    read_request_id,
    validate_envelope,
    validate_directory,
    validate_response,
    validate_username,
    validate_content,
    validate_group_name,
    validate_id,
    validate_room,
    validate_room_key,
    validate_sync_request,
    validate_sync_response,
)


class FramingTests(unittest.TestCase):
    def test_utf8_can_be_split_inside_a_character(self):
        document = {"request_id": 1, "action": "ping", "payload": {"text": "\u0e44\u0e17\u0e22\n\u0e1a\u0e23\u0e23\u0e17\u0e31\u0e14\u0e2a\u0e2d\u0e07"}}
        wire = encode_message(document)
        framer = LineFramer()
        frames = []
        for byte in wire:
            frames.extend(framer.feed(bytes([byte])))
        self.assertEqual(len(frames), 1)
        self.assertEqual(decode_document(frames[0]), document)
        self.assertFalse(framer.has_pending)

    def test_multiple_lines_and_incomplete_tail(self):
        framer = LineFramer()
        self.assertEqual(list(framer.feed(b'{}\n[]\r\n{"x":')), [b"{}", b"[]"])
        self.assertTrue(framer.has_pending)
        self.assertEqual(list(framer.feed(b"1}\n")), [b'{"x":1}'])
        self.assertFalse(framer.has_pending)

    def test_limit_counts_bytes_and_cr_but_not_lf(self):
        self.assertEqual(list(LineFramer(3).feed(b"ab\r\n")), [b"ab"])
        for wire in [b"abc\r\n", b"abcd", b"abcd\n", "\u0e44\u0e17\u0e22".encode()]:
            with self.subTest(wire=wire), self.assertRaises(FrameTooLarge):
                list(LineFramer(3).feed(wire))

    def test_valid_frame_precedes_fatal_frame_in_same_chunk(self):
        frames = LineFramer(3).feed(b"{}\nabcd\n")
        self.assertEqual(next(frames), b"{}")
        with self.assertRaises(FrameTooLarge):
            next(frames)

    def test_encoder_escapes_newlines_and_keeps_unicode(self):
        wire = encode_message({"text": "\u0e44\u0e17\u0e22\n\u0e2a\u0e2d\u0e07"})
        self.assertEqual(wire.count(b"\n"), 1)
        self.assertIn("\u0e44\u0e17\u0e22".encode(), wire)
        self.assertEqual(json.loads(wire), {"text": "\u0e44\u0e17\u0e22\n\u0e2a\u0e2d\u0e07"})
        with self.assertRaises(FrameTooLarge):
            encode_message({"text": "\u0e44\u0e17\u0e22"}, max_bytes=3)


class RequestValidationTests(unittest.TestCase):
    def test_username_bounds_and_characters(self):
        for name in ["Ab3", "Alice_123", "a" * 32]:
            self.assertEqual(validate_username(name), name)
        for name in [None, True, 1, "", "ab", "a" * 33, " Alice", "a b", "a-b", "\u0e44\u0e17\u0e22"]:
            with self.subTest(name=name), self.assertRaises(ProtocolError):
                validate_username(name)

    def test_bad_json_and_non_finite_numbers_are_recoverable(self):
        for frame in [
            b"", b"{", b'{"x":1,"x":2}', b'{"nested":{"x":1,"x":2}}',
            b'{"x":NaN}', b'{"x":Infinity}', b'{"x":-Infinity}', b'{"x":1e999}',
            b'{"x":' * 2000 + b"0",  # Incomplete even if the parser supports deep nesting.
        ]:
            with self.subTest(frame=frame[:60]), self.assertRaises(ProtocolError) as caught:
                decode_document(frame)
            self.assertEqual(caught.exception.code, "INVALID_JSON")
            self.assertFalse(caught.exception.close)

    def test_invalid_utf8_is_fatal(self):
        with self.assertRaises(ProtocolError) as caught:
            decode_document(b"\xff")
        self.assertEqual(caught.exception.code, "INVALID_UTF8")
        self.assertTrue(caught.exception.close)

    def test_top_level_must_be_object(self):
        for frame in [b"[]", b"null", b"1", b'"hello"', b"true"]:
            with self.subTest(frame=frame), self.assertRaises(ProtocolError) as caught:
                decode_document(frame)
            self.assertEqual(caught.exception.code, "INVALID_REQUEST")

    def test_request_id_rejects_bool_float_string_and_out_of_range(self):
        for value in [None, True, False, 1.0, "1", 0, -1, MAX_REQUEST_ID + 1]:
            with self.subTest(value=value), self.assertRaises(ProtocolError) as caught:
                read_request_id({"request_id": value})
            self.assertEqual(caught.exception.code, "INVALID_REQUEST_ID")
        self.assertEqual(read_request_id({"request_id": MAX_REQUEST_ID}), MAX_REQUEST_ID)

    def test_strict_envelope(self):
        base = {"request_id": 1, "action": "ping", "payload": {}}
        for document in [
            {"request_id": 1, "action": "ping"}, {**base, "extra": True},
            {**base, "action": 1}, {**base, "payload": []}, {**base, "payload": None},
        ]:
            with self.subTest(document=document), self.assertRaises(ProtocolError) as caught:
                validate_envelope(document, 1)
            self.assertEqual(caught.exception.code, "INVALID_REQUEST")
        request = validate_envelope(base, 1)
        self.assertEqual((request.request_id, request.action, request.payload), (1, "ping", {}))


class ResponseValidationTests(unittest.TestCase):
    def test_success_error_and_additional_response_fields(self):
        for response in [
            {"request_id": 1, "ok": True, "data": {}, "extra": 1},
            {"request_id": 1, "ok": False, "error": {
                "code": "SERVER_BUSY", "message": "Try again", "details": {},
            }},
        ]:
            self.assertIs(validate_response(response, 1), response)

    def test_invalid_response_fields_and_mismatched_ids(self):
        valid = {"request_id": 1, "ok": True, "data": {}}
        for response in [
            {}, {**valid, "request_id": True}, {**valid, "request_id": 2},
            {**valid, "request_id": None}, {**valid, "ok": 1},
            {**valid, "data": []}, {**valid, "error": {}},
            {"request_id": 1, "ok": False},
            {"request_id": 1, "ok": False, "error": {"code": "BAD"}},
            {"request_id": 1, "ok": False, "error": {"code": 1, "message": "Bad"}},
            {"request_id": 1, "ok": False, "error": {
                "code": "BAD", "message": "Bad", "details": [],
            }},
            {"request_id": 1, "ok": False, "error": {
                "code": "BAD", "message": "Bad",
            }, "data": {}},
        ]:
            with self.subTest(response=response), self.assertRaises(ProtocolError):
                validate_response(response, 1)


class DirectoryValidationTests(unittest.TestCase):
    def test_valid_directory_keeps_public_user_fields(self):
        data = {"online_users": [{"id": 1, "username": "Alice"}], "groups": []}
        self.assertIs(validate_directory(data), data)
        self.assertEqual(validate_directory({"online_users": [], "groups": []})["online_users"], [])

    def test_invalid_users_lists_and_duplicates_are_rejected(self):
        alice = {"id": 1, "username": "Alice"}
        for data in [
            {}, {"online_users": None, "groups": []}, {"online_users": [], "groups": None},
            {"online_users": [None], "groups": []},
            {"online_users": [{"id": True, "username": "Alice"}], "groups": []},
            {"online_users": [{"id": 1, "username": "invalid name"}], "groups": []},
            {"online_users": [alice, alice], "groups": []},
            {"online_users": [alice, {"id": 2, "username": "ALICE"}], "groups": []},
        ]:
            with self.subTest(data=data), self.assertRaises(ProtocolError):
                validate_directory(data)


class ChatValidationTests(unittest.TestCase):
    def test_group_names_trim_unicode_and_reject_controls_or_line_breaks(self):
        self.assertEqual(validate_group_name("  Study  "), "Study")
        self.assertEqual(validate_group_name("\u0e01" * 80), "\u0e01" * 80)
        for value in [None, True, 1, "", "  ", "a" * 81, "bad\n", "bad\rname",
                      "bad\tname", "bad\x7f", "bad\ud800", "bad\u2028name", "bad\u2029name"]:
            with self.subTest(value=repr(value)), self.assertRaises(ProtocolError):
                validate_group_name(value)

    def test_group_room_requires_canonical_key_name_and_unique_members(self):
        group = {"room_key": "group:00000000-0000-4000-8000-000000000000", "kind": "group",
                 "name": "Study", "created_by_user_id": 1, "created_at": "2026-11-08T00:00:00.000Z",
                 "members": [{"id": 1, "username": "Alice"}]}
        self.assertIs(validate_room(group), group)
        for bad in [
            {**group, "room_key": "private:1:2"}, {**group, "room_key": group["room_key"].replace("4000", "1000")},
            {**group, "name": " Study "}, {**group, "name": None}, {**group, "name": "line\nbreak"},
            {**group, "members": []}, {**group, "members": group["members"] * 2},
            {**group, "created_by_user_id": 2}, {**group, "kind": []},
        ]:
            with self.subTest(bad=bad), self.assertRaises(ProtocolError):
                validate_room(bad)

    def test_directory_rejects_duplicate_groups_and_private_rooms(self):
        group = {"room_key": "group:00000000-0000-4000-8000-000000000000", "kind": "group",
                 "name": "Study", "created_by_user_id": 1, "created_at": "2026-11-08T00:00:00.000Z",
                 "members": [{"id": 1, "username": "Alice"}]}
        private = {**group, "room_key": "private:1:2", "kind": "private", "name": None,
                   "members": group["members"] + [{"id": 2, "username": "Bob"}]}
        validate_directory({"online_users": [], "groups": [group]})
        for groups in [[group, group], [private], [None], [{**group, "name": ""}]]:
            with self.subTest(groups=groups), self.assertRaises(ProtocolError):
                validate_directory({"online_users": [], "groups": groups})

    def test_content_normalizes_crlf_and_rejects_unsupported_text(self):
        self.assertEqual(validate_content("  one\r\ntwo\t  "), "  one\ntwo\t  ")
        self.assertEqual(validate_content("x" * 4000), "x" * 4000)
        for value in [None, True, "", " \n", "x" * 4001, "a\r", "a\x00", "a\x7f", "a\ud800"]:
            with self.subTest(value=repr(value)), self.assertRaises(ProtocolError):
                validate_content(value)

    def test_room_keys_and_id_types(self):
        self.assertEqual(validate_room_key("private:1:2"), "private:1:2")
        self.assertEqual(validate_id(0, "cursor", zero=True), 0)
        for key in [None, "", "private:2:1", "private:1:1", "private:01:2", "private:1:9999999999999999"]:
            with self.subTest(key=key), self.assertRaises(ProtocolError):
                validate_room_key(key)
        for value in [True, 1.0, "1", 0, -1]:
            with self.subTest(value=value), self.assertRaises(ProtocolError):
                validate_id(value, "id")

    def test_sync_request_initial_empty_and_loaded_states(self):
        base = {"room_key": "private:1:2", "old_count": 20, "oldest_id": None,
                "latest_id": None, "room_revision": None}
        validate_sync_request(base)
        validate_sync_request({**base, "old_count": 0, "latest_id": 0, "room_revision": 0})
        validate_sync_request({**base, "oldest_id": 4, "latest_id": 9, "room_revision": 0})
        for payload in [
            {**base, "old_count": 0}, {**base, "old_count": True}, {**base, "old_count": 101},
            {**base, "latest_id": 0}, {**base, "latest_id": True},
            {**base, "oldest_id": 9, "latest_id": 4, "room_revision": 0},
            {**base, "oldest_id": 0, "latest_id": 1, "room_revision": 0},
            {**base, "oldest_id": 1, "latest_id": 2}, {**base, "unexpected": 1},
        ]:
            with self.subTest(payload=payload), self.assertRaises(ProtocolError):
                validate_sync_request(payload)

    def test_room_and_sync_response_reject_wrong_members_and_cursor_skips(self):
        room = {"room_key": "private:1:2", "kind": "private", "name": None,
                "created_by_user_id": 1, "created_at": "2026-11-07T00:00:00.000Z",
                "members": [{"id": 1, "username": "Alice"}, {"id": 2, "username": "Bob"}]}
        validate_room(room)
        for bad in [{**room, "room_key": "private:1:3"}, {**room, "created_by_user_id": True},
                    {**room, "members": room["members"] * 2}]:
            with self.assertRaises(ProtocolError):
                validate_room(bad)
        request = {"room_key": "private:1:2", "old_count": 0, "oldest_id": None,
                   "latest_id": 0, "room_revision": 0}
        message = {"id": 8, "room_key": "private:1:2", "sender_kind": "human",
                   "sender": room["members"][0], "content": "Hello", "created_at": room["created_at"],
                   "heart_count": 0, "hearted_by_me": False, "ai": None}
        data = {"room_key": "private:1:2", "older_messages": [], "new_messages": [message],
                "oldest_id": 8, "latest_id": 8, "has_more_older": False, "has_more_new": False,
                "room_revision": 0, "reload_required": False, "pinned_message": None,
                "effect_revision": 0, "effect": None}
        validate_sync_response(data, request)
        for bad in [{**data, "latest_id": 999}, {**data, "latest_id": True}, {**data, "has_more_new": True},
                    {**data, "room_key": "private:1:3"},
                    {**data, "new_messages": [message, message]}, {**data, "reload_required": True}]:
            with self.assertRaises(ProtocolError):
                validate_sync_response(bad, request)
        bad = copy.deepcopy(data)
        bad["new_messages"][0]["content"] = "\ud800"
        with self.assertRaises(ProtocolError):
            validate_sync_response(bad, request)


class SnapshotValidationTests(unittest.TestCase):
    def setUp(self):
        self.request = {"room_key": "private:1:2", "from_id": 4, "through_id": 12,
                        "after_id": None, "room_revision": 3, "limit": 2}
        self.message = {"id": 4, "room_key": "private:1:2", "sender_kind": "human",
                        "sender": {"id": 1, "username": "Alice"}, "content": "Hello", "created_at": "now",
                        "heart_count": 1, "hearted_by_me": True, "ai": None}
        self.page = {"room_key": "private:1:2", "room_revision": 3,
                     "messages": [self.message, {**self.message, "id": 8}],
                     "pinned_message": {**self.message, "id": 1}, "has_more": True, "next_after_id": 8}

    def test_snapshot_request_accepts_fixed_or_null_ranges_only(self):
        from common.protocol import validate_snapshot_request
        validate_snapshot_request(self.request)
        validate_snapshot_request({**self.request, "from_id": None, "through_id": None})
        for change in [{"from_id": None}, {"through_id": 3}, {"after_id": True}, {"after_id": 13},
                       {"room_revision": -1}, {"limit": True}, {"limit": 101}, {"extra": 1}]:
            with self.subTest(change=change), self.assertRaises(ProtocolError):
                validate_snapshot_request({**self.request, **change})

    def test_snapshot_response_checks_revision_range_and_progress(self):
        from common.protocol import validate_snapshot_response
        validate_snapshot_response(self.page, self.request)
        for change in [{"room_revision": 2}, {"room_revision": True}, {"next_after_id": 4},
                       {"has_more": 1}, {"messages": [self.message]},
                       {"messages": [{**self.message, "id": 3}, {**self.message, "id": 8}]},
                       {"messages": [{**self.message, "id": 8}, {**self.message, "id": 8}]},
                       {"pinned_message": {**self.message, "room_key": "private:1:3"}}]:
            with self.subTest(change=change), self.assertRaises(ProtocolError):
                validate_snapshot_response({**self.page, **change}, self.request)
        empty = {**self.page, "messages": [], "has_more": False, "next_after_id": None}
        validate_snapshot_response(empty, {**self.request, "from_id": None, "through_id": None})
        with self.assertRaises(ProtocolError):
            validate_snapshot_response({**self.page, "has_more": False}, self.request)

    def test_heart_and_pin_acknowledgements_match_desired_state(self):
        from common.protocol import validate_mutation_response, validate_message
        request = {"room_key": "private:1:2", "message_id": 4, "hearted": True}
        data = {**request, "heart_count": 1, "hearted_by_me": True, "room_revision": 3}
        validate_mutation_response(data, request, "set_heart")
        for change in [{"message_id": True}, {"message_id": 5}, {"heart_count": -1},
                       {"heart_count": True}, {"heart_count": 0}, {"hearted_by_me": False}]:
            with self.subTest(change=change), self.assertRaises(ProtocolError):
                validate_mutation_response({**data, **change}, request, "set_heart")
        validate_message(self.message, request["room_key"])
        with self.assertRaises(ProtocolError):
            validate_message({**self.message, "hearted_by_me": 1}, request["room_key"])
        pin = {"room_key": request["room_key"], "pinned_message": self.message, "room_revision": 3}
        validate_mutation_response(pin, request, "set_pin")
        with self.assertRaises(ProtocolError):
            validate_mutation_response(pin, {**request, "message_id": None}, "set_pin")

    def test_sync_reload_flag_must_match_revision_without_advancing_cursors(self):
        request = {"room_key": "private:1:2", "old_count": 0, "oldest_id": 4, "latest_id": 12, "room_revision": 2}
        data = {"room_key": "private:1:2", "older_messages": [], "new_messages": [],
                "oldest_id": 4, "latest_id": 12, "has_more_older": False, "has_more_new": False,
                "reload_required": True, "room_revision": 3, "pinned_message": self.message,
                "effect_revision": 0, "effect": None}
        validate_sync_response(data, request)
        for change in [{"reload_required": False}, {"room_revision": 2}, {"latest_id": 13}]:
            with self.assertRaises(ProtocolError):
                validate_sync_response({**data, **change}, request)


class SummaryValidationTests(unittest.TestCase):
    def test_ai_message_requires_no_user_and_valid_source_model_job(self):
        from common.protocol import validate_message
        message = {"id": 8, "room_key": "private:1:2", "sender_kind": "ai", "sender": None,
                   "content": "Summary", "created_at": "now", "heart_count": 1, "hearted_by_me": True,
                   "ai": {"model": "actual-model", "summary_job_id": "00000000-0000-4000-8000-000000000001", "source_last_message_id": 4}}
        validate_message(message, message["room_key"])
        for change in [{"sender": {"id": 1, "username": "Alice"}}, {"ai": None},
                       {"ai": {**message["ai"], "source_last_message_id": 8}},
                       {"ai": {**message["ai"], "summary_job_id": "bad"}},
                       {"ai": {**message["ai"], "model": "bad\nmodel"}}]:
            with self.subTest(change=change), self.assertRaises(ProtocolError):
                validate_message({**message, **change}, message["room_key"])

    def test_job_ack_and_status_fields_must_agree_with_state(self):
        from common.protocol import validate_summary_ack, validate_summary_status
        job_id = "00000000-0000-4000-8000-000000000001"
        validate_summary_ack({"job_id": job_id, "state": "running"})
        with self.assertRaises(ProtocolError):
            validate_summary_ack({"job_id": job_id, "state": "succeeded"})
        job = {"job_id": job_id, "room_key": "private:1:2", "state": "running", "created_at": "now",
               "completed_at": None, "result_message_id": None, "error": None}
        validate_summary_status({"job": job}, job_id)
        validate_summary_status({"job": {**job, "state": "succeeded", "completed_at": "later", "result_message_id": 8}}, job_id)
        validate_summary_status({"job": {**job, "state": "failed", "completed_at": "later",
                                        "error": {"code": "AI_RATE_LIMITED", "message": "Later", "retry_after_seconds": 5}}}, job_id)
        for change in [{"job_id": "bad"}, {"state": "other"}, {"result_message_id": 1},
                       {"state": "succeeded", "completed_at": "later", "result_message_id": True},
                       {"state": "failed", "completed_at": "later", "error": {"code": "AI_FAILED", "message": "Oops", "retry_after_seconds": True}}]:
            with self.subTest(change=change), self.assertRaises(ProtocolError):
                validate_summary_status({"job": {**job, **change}}, job_id)
