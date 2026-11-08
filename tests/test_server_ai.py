"""Private configuration and mocked OpenAI-compatible HTTP; no real .env/API."""

import io
import json
import os
import tempfile
import unittest
import urllib.error
from pathlib import Path
from unittest.mock import Mock, patch

from common.protocol import ProtocolError
from server_ai import AIConfig, MAX_RESPONSE_BYTES, _NoRedirect, generate_summary, load_ai_config

CONFIG = AIConfig("https://provider.example/v1", "test-model", "dummy-private-key")
SOURCE = [{"sender": {"id": 1, "username": "Alice"}, "content": "Our meeting is on Friday."}]


class ProviderTests(unittest.TestCase):
    def response(self, document):
        return io.BytesIO(json.dumps(document).encode())

    def test_request_uses_base_url_and_keeps_credentials_out_of_prompt_and_repr(self):
        opener = Mock()
        opener.open.return_value = self.response({"model": "actual-model", "choices": [{"finish_reason": "stop", "message": {"content": "Meet on Friday."}}]})
        with patch("server_ai.urllib.request.build_opener", return_value=opener):
            self.assertEqual(generate_summary(CONFIG, SOURCE), ("Meet on Friday.", "actual-model"))
        request = opener.open.call_args.args[0]
        self.assertEqual(request.full_url, "https://provider.example/v1/chat/completions")
        self.assertEqual(opener.open.call_args.kwargs["timeout"], 30)
        self.assertEqual(request.get_header("Authorization"), "Bearer dummy-private-key")
        body = json.loads(request.data)
        self.assertEqual(body["model"], "test-model")
        self.assertFalse(body["stream"])
        self.assertEqual(json.loads(body["messages"][1]["content"])[0]["content"], SOURCE[0]["content"])
        self.assertNotIn(CONFIG.api_key, request.data.decode())
        self.assertNotIn(CONFIG.api_key, repr(CONFIG))

    def test_http_failures_are_fixed_safe_codes_with_optional_retry_delay(self):
        for status, code in [(429, "AI_RATE_LIMITED"), (401, "AI_UNAVAILABLE"), (503, "AI_UNAVAILABLE"), (400, "AI_FAILED")]:
            error = urllib.error.HTTPError(CONFIG.base_url, status, CONFIG.api_key, {"Retry-After": "12"}, io.BytesIO(CONFIG.api_key.encode()))
            opener = Mock()
            opener.open.side_effect = error
            with self.subTest(status=status), patch("server_ai.urllib.request.build_opener", return_value=opener):
                with self.assertRaises(ProtocolError) as caught:
                    generate_summary(CONFIG, SOURCE)
                self.assertEqual(caught.exception.code, code)
                self.assertNotIn(CONFIG.api_key, str(caught.exception))
                if status == 429:
                    self.assertEqual(caught.exception.details, {"retry_after_seconds": 12})

    def test_timeout_and_network_errors_do_not_expose_provider_details(self):
        for error, code in [(TimeoutError(CONFIG.api_key), "AI_TIMEOUT"),
                            (urllib.error.URLError(TimeoutError(CONFIG.api_key)), "AI_TIMEOUT"),
                            (urllib.error.URLError(CONFIG.api_key), "AI_UNAVAILABLE")]:
            opener = Mock()
            opener.open.side_effect = error
            with patch("server_ai.urllib.request.build_opener", return_value=opener):
                with self.assertRaises(ProtocolError) as caught:
                    generate_summary(CONFIG, SOURCE)
                self.assertEqual(caught.exception.code, code)
                self.assertNotIn(CONFIG.api_key, str(caught.exception))

    def test_invalid_truncated_oversized_or_credential_echo_responses_are_rejected(self):
        valid = {"model": "actual-model", "choices": [{"message": {"content": "Summary"}}]}
        documents = [[], {}, {**valid, "model": None},
                     {**valid, "choices": [{"message": {"content": "x" * 4001}}]},
                     {**valid, "choices": [{"finish_reason": "length", "message": {"content": "Partial"}}]},
                     {**valid, "choices": [{"message": {"content": CONFIG.api_key}}]},
                     {**valid, "choices": [{"message": {"content": None}}]}]
        for document in documents:
            opener = Mock()
            opener.open.return_value = self.response(document)
            with self.subTest(document=str(document)[:100]), patch("server_ai.urllib.request.build_opener", return_value=opener):
                with self.assertRaises(ProtocolError) as caught:
                    generate_summary(CONFIG, SOURCE)
                self.assertEqual(caught.exception.code, "AI_FAILED")
        for body in (b'not json', b'x' * (MAX_RESPONSE_BYTES + 1)):
            opener = Mock()
            opener.open.return_value = io.BytesIO(body)
            with patch("server_ai.urllib.request.build_opener", return_value=opener), self.assertRaises(ProtocolError):
                generate_summary(CONFIG, SOURCE)

    def test_redirects_do_not_forward_authorization(self):
        with self.assertRaises(urllib.error.URLError):
            _NoRedirect().redirect_request(None, None, 302, "redirect", {}, "https://other.example")

    def test_environment_file_is_data_and_exported_values_override_it(self):
        with tempfile.TemporaryDirectory() as folder, patch.dict(os.environ, {}, clear=True):
            path = Path(folder) / "example.env"
            path.write_text('export OPENAI_BASE_URL="https://provider.example/v1"\nOPENAI_MODEL=from-file\nOPENAI_API_KEY=\'dummy-private-key\'\nIGNORED=$(do-not-execute)\n')
            self.assertEqual(load_ai_config(path).model, "from-file")
            with patch.dict(os.environ, {"OPENAI_MODEL": "exported"}):
                self.assertEqual(load_ai_config(path).model, "exported")
            self.assertNotIn("dummy-private-key", repr(load_ai_config(path)))
            path.write_text('OPENAI_API_KEY=your_api_key_here\n')
            self.assertIsNone(load_ai_config(path))
            self.assertIsNone(load_ai_config(Path(folder) / "missing"))

    def test_configuration_errors_do_not_echo_values(self):
        for url in ("http://provider.example/v1", "https://user:secret@provider.example/v1", "https://provider.example/v1/chat/completions"):
            with self.subTest(url=url), self.assertRaises(ValueError):
                AIConfig(url, "model", "dummy")
        with tempfile.TemporaryDirectory() as folder, patch.dict(os.environ, {}, clear=True):
            path = Path(folder) / "example.env"
            path.write_text('OPENAI_API_KEY=dummy-private-key\n')
            with self.assertRaises(ValueError) as caught:
                load_ai_config(path)
            self.assertNotIn("dummy-private-key", str(caught.exception))
