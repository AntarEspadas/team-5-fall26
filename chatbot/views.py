"""Endpoints for the chat widget and the event managers' Questions page."""

import json
import logging
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
from .models import Inquiry

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
            return error(
                "You're sending messages too quickly. Please wait a moment.", 429
            )
        return view(request, *args, **kwargs)

    return wrapper


def run_assistant(request, message, page):
    """Run the assistant, returning None when Gemini is unavailable."""
    try:
        return reply(request, message, page)
    except gemini.GeminiError:
        return None


@require_POST
@assistant_endpoint
def chat_message(request):
    """Answer a typed message sent as JSON ``{"message", "page"}``."""
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
    if result is None:
        return error(UNAVAILABLE, 503)
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
    except gemini.GeminiError:
        return error(UNAVAILABLE, 503)
    if not transcript:
        return error("Sorry, I couldn't hear that. Please try again.", 422)
    transcript = transcript[:MAX_MESSAGE_LENGTH]
    result = run_assistant(request, transcript, request.POST.get("page", ""))
    if result is None:
        return error(UNAVAILABLE, 503)
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
