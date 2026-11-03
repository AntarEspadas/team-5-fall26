"""Endpoints for the chat widget and the event managers' Questions page."""

import json
import logging
import time
from functools import wraps

from django.conf import settings
from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.cache import cache
from django.http import Http404, JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone
from django.views.decorators.http import require_GET, require_POST

from . import gemini
from .assistant import (
    INQUIRIES_KEY,
    append_history,
    get_history,
    reply,
)
from .forms import AnswerInquiryForm
from .inquiries import answer_inquiry, inquiries_managed_by, undelivered_answers
from .logs import record_chat
from .models import ChatLog, Inquiry

logger = logging.getLogger(__name__)

MAX_MESSAGE_LENGTH = 1000
MAX_AUDIO_BYTES = 4 * 1024 * 1024
AUDIO_TYPES = (
    "audio/wav",
    "audio/x-wav",
    "audio/webm",
    "audio/ogg",
    "audio/mpeg",
    "audio/mp4",
    "audio/aac",
    "audio/flac",
)
# Chat and voice requests allowed per visitor each minute.
RATE_LIMIT = 15
# A looser limit per network address, for visitors who drop cookies.
IP_RATE_LIMIT = 60
RATE_WINDOW_SECONDS = 60
# Answered questions listed under the open ones on the Questions page.
ANSWERED_SHOWN = 20

UNAVAILABLE = "The assistant is unavailable right now. Please try again in a moment."
BUSY = "The assistant is very busy right now. Please try again in a minute."
TOO_FAST = "You're sending messages too quickly. Please wait a moment."


def error(message, status):
    """Return a JSON error the widget shows to the visitor."""
    return JsonResponse({"error": message}, status=status)


def client_ip(request):
    """Return the visitor's address, as reported by Elastic Beanstalk's proxy."""
    forwarded = request.META.get("HTTP_X_FORWARDED_FOR", "")
    return forwarded.split(",")[0].strip() or request.META.get("REMOTE_ADDR", "")


def over_limit(key, limit):
    """Count one request against ``key`` and return whether it is over."""
    key = f"chatbot-rate:{key}"
    cache.add(key, 0, RATE_WINDOW_SECONDS)
    try:
        count = cache.incr(key)
    except ValueError:  # Expired between add() and incr().
        cache.set(key, 1, RATE_WINDOW_SECONDS)
        count = 1
    return count > limit


def is_rate_limited(request):
    """Limit each account or browser session, and each network address."""
    if request.user.is_authenticated:
        visitor = f"user:{request.user.pk}"
    else:
        if not request.session.session_key:
            request.session.save()
        visitor = f"session:{request.session.session_key}"
    limited = over_limit(visitor, RATE_LIMIT)
    return over_limit(f"ip:{client_ip(request)}", IP_RATE_LIMIT) or limited


def assistant_endpoint(view):
    """Hide endpoints while disabled and throttle visitors who send too much."""

    @wraps(view)
    def wrapper(request, *args, **kwargs):
        if not settings.CHATBOT_ENABLED:
            raise Http404("The assistant is not enabled.")
        if request.method == "POST" and is_rate_limited(request):
            return error(TOO_FAST, 429)
        return view(request, *args, **kwargs)

    return wrapper


def failure_message(exc):
    """Explain a Gemini failure to the visitor."""
    return BUSY if exc.rate_limited else UNAVAILABLE


def outcome_of(result):
    """Classify a reply for the conversation log."""
    if result["declined"]:
        return ChatLog.Outcome.DECLINED
    if result["escalated"]:
        return ChatLog.Outcome.ESCALATED
    return ChatLog.Outcome.ANSWERED


def run_assistant(request, message, page, voice=False):
    """Answer a message and log the exchange.

    Returns:
        dict | JsonResponse: the reply data, or an error response.
    """
    started = time.monotonic()
    try:
        result = reply(request, message, page)
    except gemini.GeminiError as exc:
        elapsed = (time.monotonic() - started) * 1000
        record_chat(request, message, "", ChatLog.Outcome.ERROR, elapsed, page, voice)
        return error(failure_message(exc), 503)
    elapsed = (time.monotonic() - started) * 1000
    logger.info("Assistant replied in %.0f ms", elapsed)
    record_chat(
        request, message, result["reply"], outcome_of(result), elapsed, page, voice
    )
    return result


@require_POST
@assistant_endpoint
def chat_message(request):
    """Answer a typed message: ``POST /api/chat`` with JSON ``{"message"}``.

    The optional ``page`` is the path the visitor is on. The reply is JSON
    ``{"reply", "escalated", "declined"}``, or ``{"error"}`` with a 4xx/5xx
    status the widget shows as a friendly message.
    """
    try:
        data = json.loads(request.body or b"{}")
    except ValueError:
        return error("Send the message as JSON.", 400)
    if not isinstance(data, dict):
        return error("Send the message as JSON.", 400)
    message = str(data.get("message", "")).strip()
    if not message:
        return error("Type a message first.", 400)
    if len(message) > MAX_MESSAGE_LENGTH:
        return error(
            f"Please keep messages under {MAX_MESSAGE_LENGTH} characters.", 400
        )
    result = run_assistant(request, message, data.get("page", ""))
    if isinstance(result, JsonResponse):
        return result
    return JsonResponse(result)


@require_POST
@assistant_endpoint
def chat_voice(request):
    """Transcribe a recorded voice message, then answer it."""
    audio = request.FILES.get("audio")
    if audio is None:
        return error("No recording was received.", 400)
    if audio.size > MAX_AUDIO_BYTES:
        return error("That recording is too long. Please keep it under a minute.", 400)
    mime_type = (audio.content_type or "").split(";")[0].strip().lower()
    if mime_type not in AUDIO_TYPES:
        return error("That audio format isn't supported.", 400)
    try:
        transcript, language = gemini.transcribe(audio.read(), mime_type)
    except gemini.GeminiError as exc:
        return error(failure_message(exc), 503)
    if not transcript:
        return error("Sorry, I couldn't hear that. Please try again.", 422)
    transcript = transcript[:MAX_MESSAGE_LENGTH]
    result = run_assistant(
        request, transcript, request.POST.get("page", ""), voice=True
    )
    if isinstance(result, JsonResponse):
        return result
    return JsonResponse(dict(result, transcript=transcript, language=language))


@require_GET
@assistant_endpoint
def chat_history(request):
    """Return the conversation, adding any answers from event managers."""
    session = request.session
    inquiry_ids = session.get(INQUIRIES_KEY, [])
    updates = list(undelivered_answers(request.user, inquiry_ids))
    if updates:
        append_history(
            session,
            *(
                {
                    "role": "model",
                    "text": (
                        "**Update from the event manager** on your question "
                        f"“{inquiry.question}”:\n\n{inquiry.answer_for_asker}"
                    ),
                }
                for inquiry in updates
            ),
        )
        Inquiry.objects.filter(pk__in=[i.pk for i in updates]).update(
            delivered_at=timezone.now()
        )
    pending = Inquiry.objects.filter(
        pk__in=inquiry_ids, status=Inquiry.Status.OPEN
    ).exists()
    return JsonResponse(
        {
            "messages": get_history(session),
            "updates": len(updates),
            "pending": pending,
        }
    )


@require_POST
@assistant_endpoint
def chat_reset(request):
    """Start a new conversation; answers to earlier questions still arrive."""
    request.session["chatbot_history"] = []
    return JsonResponse({"messages": []})


@login_required
def inquiry_list(request):
    """List open questions the user can answer, then recent answers."""
    inquiries = inquiries_managed_by(request.user)
    return render(
        request,
        "chatbot/inquiries.html",
        {
            "open_inquiries": inquiries.filter(status=Inquiry.Status.OPEN).order_by(
                "created_at", "pk"
            ),
            "answered_inquiries": inquiries.filter(
                status=Inquiry.Status.ANSWERED
            ).order_by("-answered_at", "-pk")[:ANSWERED_SHOWN],
            "form": AnswerInquiryForm(),
        },
    )


@login_required
@require_POST
def answer_inquiry_view(request, pk):
    """Save a manager's answer and send it to whoever asked."""
    inquiry = get_object_or_404(inquiries_managed_by(request.user), pk=pk)
    form = AnswerInquiryForm(request.POST)
    if not form.is_valid():
        messages.error(request, "Write an answer before sending.")
    elif answer_inquiry(inquiry, form.cleaned_data["answer"], request.user):
        messages.success(request, "Answer sent.")
    else:
        messages.info(request, "This question was already answered.")
    return redirect("chatbot:inquiries")
