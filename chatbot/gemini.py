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

# Chat replies should arrive within a few seconds; give up well before the
# browser or load balancer would.
REQUEST_TIMEOUT_SECONDS = 20

# Block harmful content in both directions (on top of the system prompt).
SAFETY_SETTINGS = [
    {"category": category, "threshold": "BLOCK_MEDIUM_AND_ABOVE"}
    for category in (
        "HARM_CATEGORY_HARASSMENT",
        "HARM_CATEGORY_HATE_SPEECH",
        "HARM_CATEGORY_SEXUALLY_EXPLICIT",
        "HARM_CATEGORY_DANGEROUS_CONTENT",
    )
]
# Finish reasons that mean Gemini refused to answer for safety or policy.
BLOCKED_FINISH_REASONS = {
    "SAFETY",
    "PROHIBITED_CONTENT",
    "BLOCKLIST",
    "SPII",
    "RECITATION",
}

TRANSCRIBE_PROMPT = (
    "Transcribe this voice message exactly as spoken, in its original "
    "language. Do not translate or answer it. Also give the spoken language "
    "as a BCP 47 tag such as en-US, es-ES, hi-IN, or zh-CN. If there is no "
    "intelligible speech, return an empty transcript."
)


class GeminiError(Exception):
    """Raised when Gemini cannot be reached or returns an unusable reply.

    ``rate_limited`` is true when Gemini refused because of quota (HTTP 429).
    ``transient`` is true when the model was overloaded or timed out, so
    another model may still answer.
    """

    def __init__(self, message="", rate_limited=False, transient=False):
        super().__init__(message)
        self.rate_limited = rate_limited
        self.transient = transient


# Gemini answers these when a model is overloaded ("high demand").
TRANSIENT_HTTP_CODES = {500, 502, 503, 504}


def model_chain():
    """Return the models to try in order: the configured one, then the fallback."""
    models = (settings.GEMINI_MODEL, settings.GEMINI_FALLBACK_MODEL)
    return list(dict.fromkeys(model for model in models if model))


def generate_content(payload, models=None):
    """Send a ``generateContent`` request and return the decoded response.

    Tries each model in ``models`` (default: ``model_chain()``) until one
    answers, moving past models that are overloaded or time out. A list passed
    in is trimmed in place to start at the model that answered, so later
    requests in the same conversation turn stay on that model.
    """
    if models is None:
        models = model_chain()
    while True:
        try:
            return request_model(payload, models[0])
        except GeminiError as error:
            if not error.transient or len(models) < 2:
                raise
            logger.warning(
                "Gemini model %s unavailable; trying %s", models[0], models[1]
            )
            del models[0]


def request_model(payload, model):
    """Send one ``generateContent`` request to ``model``."""
    url = f"{settings.GEMINI_API_BASE_URL}/models/{model}:generateContent"
    request = Request(
        url,
        data=json.dumps(dict(payload, safetySettings=SAFETY_SETTINGS)).encode("utf-8"),
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
        raise GeminiError(
            f"Gemini returned HTTP {error.code}",
            rate_limited=error.code == 429,
            transient=error.code in TRANSIENT_HTTP_CODES,
        ) from error
    except (URLError, TimeoutError, OSError) as error:
        logger.warning("Gemini request failed: %s", error)
        raise GeminiError("Gemini could not be reached", transient=True) from error
    except ValueError as error:
        logger.warning("Gemini returned an unreadable response: %s", error)
        raise GeminiError("Gemini returned an unreadable response") from error


def first_content(response):
    """Return the first candidate's content, or None when Gemini blocked it.

    A prompt can be blocked outright (no candidates), or the reply can stop
    for a safety reason with little or no text.
    """
    candidates = response.get("candidates") or []
    if not candidates or candidates[0].get("finishReason") in BLOCKED_FINISH_REASONS:
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
