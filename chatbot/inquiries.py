"""Answer questions that earlier versions of the assistant forwarded.

Staff answer them in the admin, and the answer reaches the asker in chat,
by notification, and by email when their address is verified.
"""

import logging

from allauth.account.models import EmailAddress
from django.conf import settings
from django.core.mail import send_mail
from django.db.models import Q
from django.utils import timezone

from accounts.ban_services import require_unbanned_user
from events.models import Notification

from . import gemini
from .models import Inquiry

logger = logging.getLogger(__name__)


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
        # A registered address is not necessarily owned by the asker.
        if asker.email and EmailAddress.objects.filter(
            user=asker, email__iexact=asker.email, verified=True
        ).exists():
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
