"""Anonymous conversation logs that help staff improve the FAQ."""

import re

from django.conf import settings

from .models import ChatLog

EMAIL = re.compile(r"[\w.+-]+@[\w-]+(?:\.[\w-]+)+")
# Digit runs with spaces, dots, dashes, or brackets, such as phone numbers.
NUMBER = re.compile(r"\+?\(?\d[\d\s().-]{7,}\d")
# Fewer digits than this are kept, so dates like 2026-11-07 stay readable.
MIN_MASKED_DIGITS = 9
LOGGED_TEXT_LIMIT = 2000


def mask_number(match):
    """Hide a phone, card, or account number; keep shorter numbers."""
    text = match.group()
    if sum(char.isdigit() for char in text) < MIN_MASKED_DIGITS:
        return text
    return "[number]"


def redact(text):
    """Mask email addresses and long numbers in ``text``."""
    text = EMAIL.sub("[email]", str(text or ""))
    return NUMBER.sub(mask_number, text)[:LOGGED_TEXT_LIMIT]


def record_chat(request, message, reply, outcome, response_ms, page="", voice=False):
    """Save one exchange without anything that identifies the visitor."""
    if not settings.CHATBOT_LOG_CONVERSATIONS:
        return None
    return ChatLog.objects.create(
        signed_in=request.user.is_authenticated,
        via_voice=voice,
        page=str(page or "")[:200],
        message=redact(message),
        reply=redact(reply),
        outcome=outcome,
        response_ms=max(0, int(response_ms)),
    )
