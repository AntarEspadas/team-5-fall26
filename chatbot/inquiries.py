"""Pass unanswered questions to event managers and deliver their answers."""

import logging

from django.conf import settings
from django.core.mail import mail_managers, send_mail
from django.db.models import Q
from django.utils import timezone
from django.utils.text import Truncator

from accounts.ban_services import require_unbanned_user
from events.models import Notification

from . import gemini
from .models import Inquiry

logger = logging.getLogger(__name__)

SUBJECT_MAX_LENGTH = Notification._meta.get_field("subject").max_length


def manager_for(event):
    """Return the host who manages an event, or None when staff manage it."""
    return event.host if event and event.host_id else None


def create_inquiry(user, question, question_english="", language="", event=None):
    """Save a question for an event manager and tell them about it.

    A hosted event's host gets an in-app notification and an email. Staff
    listed in ``MANAGERS`` are emailed about every question.
    """
    inquiry = Inquiry.objects.create(
        user=user,
        event=event,
        question=question,
        question_english=question_english,
        language=language,
    )
    about = f" about “{event.title}”" if event else ""
    summary = f"Someone asked the website assistant a question{about}:\n\n"
    summary += inquiry.display_question
    if not inquiry.is_english and question_english:
        summary += f"\n\nOriginal ({language}): {question}"
    summary += "\n\nAnswer it on the Questions page: /assistant/inquiries/"

    host = manager_for(event)
    if host and host != user:
        Notification.objects.create(
            recipient=host,
            event=event,
            subject=Truncator(f"New question about {event.title}").chars(
                SUBJECT_MAX_LENGTH
            ),
            body=summary,
        )
        if host.email:
            send_mail(
                "New question about your event",
                summary,
                settings.DEFAULT_FROM_EMAIL,
                [host.email],
                fail_silently=True,
            )
    mail_managers("New question for the event manager", summary, fail_silently=True)
    return inquiry


def inquiries_managed_by(user):
    """Return the inquiries a user may answer: all for staff, else their events."""
    inquiries = Inquiry.objects.select_related("user", "event", "answered_by")
    if user.is_staff:
        return inquiries
    return inquiries.filter(event__host=user)


def answer_inquiry(inquiry, answer, answered_by):
    """Record an answer and send it back to the person who asked.

    The answer is translated into the asker's language when needed. Returns
    False when someone else already answered the question.
    """
    require_unbanned_user(answered_by)
    if not Inquiry.objects.filter(pk=inquiry.pk, status=Inquiry.Status.OPEN).exists():
        return False
    translated = ""
    if not inquiry.is_english:
        try:
            translated = gemini.translate(answer, inquiry.language)
        except gemini.GeminiError:
            logger.warning("Could not translate the answer to inquiry %s", inquiry.pk)
    updated = Inquiry.objects.filter(pk=inquiry.pk, status=Inquiry.Status.OPEN).update(
        status=Inquiry.Status.ANSWERED,
        answer=answer,
        answer_translated=translated,
        answered_by=answered_by,
        answered_at=timezone.now(),
    )
    if not updated:
        return False
    inquiry.refresh_from_db()

    asker = inquiry.user
    if asker:
        body = (
            f"You asked: {inquiry.question}\n\n"
            f"Answer from the event manager: {inquiry.answer_for_asker}"
        )
        if inquiry.event_id:
            Notification.objects.create(
                recipient=asker,
                event=inquiry.event,
                subject="Your question was answered",
                body=body,
            )
        if asker.email:
            send_mail(
                "Your question was answered",
                body,
                settings.DEFAULT_FROM_EMAIL,
                [asker.email],
                fail_silently=True,
            )
    return True


def undelivered_answers(user, inquiry_ids):
    """Return answers not yet shown in the chat of this user or session."""
    owned = Q(pk__in=inquiry_ids)
    if user.is_authenticated:
        owned |= Q(user=user)
    return Inquiry.objects.filter(
        owned, status=Inquiry.Status.ANSWERED, delivered_at__isnull=True
    ).order_by("answered_at", "pk")
