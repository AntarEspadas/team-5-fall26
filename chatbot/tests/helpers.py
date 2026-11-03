"""Shared helpers for the website assistant tests."""

from datetime import timedelta

from django.contrib.auth import get_user_model
from django.utils import timezone

from events.models import Event


def make_user(username, **kwargs):
    """Create a user with a matching email address."""
    kwargs.setdefault("email", f"{username}@example.com")
    return get_user_model().objects.create_user(
        username=username, password="pass12345", **kwargs
    )


def make_event(guid, title, days=2, **kwargs):
    """Create an upcoming event ``days`` from now unless told otherwise."""
    start = timezone.now() + timedelta(days=days)
    kwargs.setdefault("start_time", start)
    kwargs.setdefault("end_time", start + timedelta(hours=2))
    kwargs.setdefault("content_hash", "v" * 64)
    return Event.objects.create(guid=guid, title=title, **kwargs)


def text_response(text):
    """A Gemini response whose first candidate is plain text."""
    return {"candidates": [{"content": {"role": "model", "parts": [{"text": text}]}}]}


def call_response(name, **args):
    """A Gemini response asking to call one tool."""
    return {
        "candidates": [
            {
                "content": {
                    "role": "model",
                    "parts": [{"functionCall": {"name": name, "args": args}}],
                }
            }
        ]
    }
