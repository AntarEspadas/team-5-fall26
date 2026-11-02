"""Tests for the Gemini REST client."""

import io
import json
from unittest import mock
from urllib.error import HTTPError, URLError

from django.test import SimpleTestCase, override_settings

from chatbot import gemini
from chatbot.tests.helpers import text_response


def fake_urlopen(payload):
    """Return a urlopen stand-in that answers with ``payload`` as JSON."""
    response = mock.MagicMock()
    response.read.return_value = json.dumps(payload).encode()
    response.__enter__.return_value = response
    return mock.Mock(return_value=response)


@override_settings(
    GEMINI_API_KEY="test-key",
    GEMINI_MODEL="test-model",
    GEMINI_API_BASE_URL="https://gemini.test/v1beta",
)
class GenerateContentTests(SimpleTestCase):
    """Requests carry the key and model; failures become GeminiError."""

    def test_posts_json_with_the_api_key(self):
        urlopen = fake_urlopen(text_response("Hi"))
        with mock.patch("chatbot.gemini.urlopen", urlopen):
            result = gemini.generate_content({"contents": []})
        request = urlopen.call_args.args[0]
        self.assertEqual(
            request.full_url,
            "https://gemini.test/v1beta/models/test-model:generateContent",
        )
        self.assertEqual(request.get_header("X-goog-api-key"), "test-key")
        self.assertEqual(json.loads(request.data), {"contents": []})
        self.assertEqual(result, text_response("Hi"))

    def test_http_errors_raise_gemini_error(self):
        error = HTTPError("url", 429, "Too Many", {}, io.BytesIO(b"quota"))
        with mock.patch("chatbot.gemini.urlopen", side_effect=error):
            with self.assertRaises(gemini.GeminiError):
                gemini.generate_content({})

    def test_network_errors_raise_gemini_error(self):
        with mock.patch("chatbot.gemini.urlopen", side_effect=URLError("down")):
            with self.assertRaises(gemini.GeminiError):
                gemini.generate_content({})


class ResponseParsingTests(SimpleTestCase):
    """Candidates and text parts are read defensively."""

    def test_missing_candidates_mean_blocked(self):
        self.assertIsNone(gemini.first_content({"promptFeedback": {}}))

    def test_candidate_without_content_has_no_parts(self):
        content = gemini.first_content({"candidates": [{}]})
        self.assertEqual(content["parts"], [])

    def test_text_skips_thoughts(self):
        content = {
            "parts": [
                {"text": "thinking", "thought": True},
                {"text": "Hello "},
                {"text": "there"},
            ]
        }
        self.assertEqual(gemini.response_text(content), "Hello there")
        self.assertEqual(gemini.response_text(None), "")


class StructuredHelpersTests(SimpleTestCase):
    """Transcription and translation ask for JSON and validate it."""

    def test_transcribe_sends_audio_and_reads_json(self):
        reply = text_response('{"transcript": " Hola ", "language": "es-ES"}')
        with mock.patch(
            "chatbot.gemini.generate_content", return_value=reply
        ) as generate:
            self.assertEqual(gemini.transcribe(b"RIFF", "audio/wav"), ("Hola", "es-ES"))
        parts = generate.call_args.args[0]["contents"][0]["parts"]
        self.assertEqual(parts[0]["inlineData"]["mimeType"], "audio/wav")
        self.assertEqual(parts[0]["inlineData"]["data"], "UklGRg==")

    def test_transcribe_rejects_malformed_json(self):
        with mock.patch(
            "chatbot.gemini.generate_content", return_value=text_response("nope")
        ):
            with self.assertRaises(gemini.GeminiError):
                gemini.transcribe(b"x", "audio/wav")

    def test_transcribe_rejects_non_objects(self):
        with mock.patch(
            "chatbot.gemini.generate_content", return_value=text_response("[1]")
        ):
            with self.assertRaises(gemini.GeminiError):
                gemini.transcribe(b"x", "audio/wav")

    def test_translate(self):
        reply = text_response('{"translation": "Sí, es gratis."}')
        with mock.patch("chatbot.gemini.generate_content", return_value=reply):
            self.assertEqual(
                gemini.translate("Yes, free.", "Spanish"), "Sí, es gratis."
            )

    def test_translate_rejects_empty_text(self):
        reply = text_response('{"translation": " "}')
        with mock.patch("chatbot.gemini.generate_content", return_value=reply):
            with self.assertRaises(gemini.GeminiError):
                gemini.translate("Yes", "Spanish")
