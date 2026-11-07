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
)


class FramingTests(unittest.TestCase):
    def test_utf8_can_be_split_inside_a_character(self):
        document = {"request_id": 1, "action": "ping", "payload": {"text": "ไทย\nบรรทัดสอง"}}
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
        for wire in [b"abc\r\n", b"abcd", b"abcd\n", "ไทย".encode()]:
            with self.subTest(wire=wire), self.assertRaises(FrameTooLarge):
                list(LineFramer(3).feed(wire))

    def test_valid_frame_precedes_fatal_frame_in_same_chunk(self):
        frames = LineFramer(3).feed(b"{}\nabcd\n")
        self.assertEqual(next(frames), b"{}")
        with self.assertRaises(FrameTooLarge):
            next(frames)

    def test_encoder_escapes_newlines_and_keeps_unicode(self):
        wire = encode_message({"text": "ไทย\nสอง"})
        self.assertEqual(wire.count(b"\n"), 1)
        self.assertIn("ไทย".encode(), wire)
        self.assertEqual(json.loads(wire), {"text": "ไทย\nสอง"})
        with self.assertRaises(FrameTooLarge):
            encode_message({"text": "ไทย"}, max_bytes=3)


class RequestValidationTests(unittest.TestCase):
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
