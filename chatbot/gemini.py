"""A small client for Google's Gemini ``generateContent`` REST endpoint.

Uses the standard library, like the NYC Parks feed sync, so the assistant
adds no dependencies. Every failure surfaces as ``GeminiError``.
"""

import base64
import json
import logging
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from django.conf import settings

logger = logging.getLogger(__name__)

REQUEST_TIMEOUT_SECONDS = 45

TRANSCRIBE_PROMPT = (
    "Transcribe this voice message exactly as spoken, in its original "
    "language. Do not translate or answer it. Also give the spoken language "
    "as a BCP 47 tag such as en-US, es-ES, hi-IN, or zh-CN. If there is no "
    "intelligible speech, return an empty transcript."
)


class GeminiError(Exception):
    """Raised when Gemini cannot be reached or returns an unusable reply."""


def generate_content(payload):
    """Send one ``generateContent`` request and return the decoded response."""
    url = (
        f"{settings.GEMINI_API_BASE_URL}/models/{settings.GEMINI_MODEL}"
        ":generateContent"
    )
    request = Request(
        url,
        data=json.dumps(payload).encode("utf-8"),
        headers={
            "Content-Type": "application/json",
            "x-goog-api-key": settings.GEMINI_API_KEY,
        },
        method="POST",
    )
    try:
        with urlopen(request, timeout=REQUEST_TIMEOUT_SECONDS) as response:
            return json.loads(response.read().decode("utf-8"))
    except HTTPError as error:
        detail = error.read().decode("utf-8", "replace")[:500]
        logger.warning("Gemini returned HTTP %s: %s", error.code, detail)
        raise GeminiError(f"Gemini returned HTTP {error.code}") from error
    except (URLError, TimeoutError, OSError, ValueError) as error:
        logger.warning("Gemini request failed: %s", error)
        raise GeminiError("Gemini could not be reached") from error


def first_content(response):
    """Return the first candidate's content, or None when Gemini blocked it."""
    candidates = response.get("candidates") or []
    if not candidates:
        return None
    return candidates[0].get("content") or {"role": "model", "parts": []}


def response_text(content):
    """Join the visible text parts of a content block, skipping thoughts."""
    parts = (content or {}).get("parts") or []
    return "".join(
        part.get("text", "") for part in parts if not part.get("thought")
    ).strip()


def generate_json(parts, schema):
    """Ask for a JSON reply matching ``schema`` and return it decoded."""
    response = generate_content(
        {
            "contents": [{"role": "user", "parts": parts}],
            "generationConfig": {
                "responseMimeType": "application/json",
                "responseSchema": schema,
            },
        }
    )
    try:
        return json.loads(response_text(first_content(response)))
    except ValueError as error:
        raise GeminiError("Gemini returned malformed JSON") from error


def transcribe(audio, mime_type):
    """Transcribe a voice message in any language.

    Returns:
        tuple: the transcript and its BCP 47 language tag (either may be "").
    """
    data = generate_json(
        [
            {
                "inlineData": {
                    "mimeType": mime_type,
                    "data": base64.b64encode(audio).decode("ascii"),
                }
            },
            {"text": TRANSCRIBE_PROMPT},
        ],
        {
            "type": "OBJECT",
            "properties": {
                "transcript": {"type": "STRING"},
                "language": {"type": "STRING"},
            },
            "required": ["transcript", "language"],
        },
    )
    if not isinstance(data, dict):
        raise GeminiError("Gemini returned an unexpected transcription")
    return str(data.get("transcript", "")).strip(), str(data.get("language", ""))


def translate(text, language):
    """Translate ``text`` into ``language`` (a name such as "Spanish")."""
    data = generate_json(
        [
            {
                "text": (
                    f"Translate the following message into {language}. Keep "
                    "names, links, dates, and numbers unchanged.\n\n" + text
                )
            }
        ],
        {
            "type": "OBJECT",
            "properties": {"translation": {"type": "STRING"}},
            "required": ["translation"],
        },
    )
    if not isinstance(data, dict) or not str(data.get("translation", "")).strip():
        raise GeminiError("Gemini returned an empty translation")
    return str(data["translation"]).strip()
