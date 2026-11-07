"""Wire-format regression tests without a running server."""

import json
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
