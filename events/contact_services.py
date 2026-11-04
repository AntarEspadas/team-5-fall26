"""Shared contact-request workflows."""

from django.db import transaction

from accounts.models import ContactPermission, User

from .models import ContactRequest, Conversation, Event, EventInterest
from .notifications import notify_contact_request


def create_contact_request(
    event,
    sender,
    recipient,
    message,
    notification_sender=notify_contact_request,
):
    """Create one eligible event contact request, or return an existing one."""
    if (
        event.status != Event.Status.UPCOMING
        or sender.pk == recipient.pk
        or not recipient.share_profile_with_attendees
        or not EventInterest.objects.filter(event=event, user=sender).exists()
        or not EventInterest.objects.filter(event=event, user=recipient).exists()
    ):
        return None, False

    with transaction.atomic():
        User.objects.select_for_update(no_key=True).get(pk=sender.pk)
        existing = (
            ContactRequest.objects.filter(
                event=event,
                sender=sender,
                recipient=recipient,
            )
            .exclude(status=ContactRequest.Status.DECLINED)
            .first()
        )
        if existing:
            return existing, False

        contact_request = ContactRequest.objects.create(
            event=event,
            sender=sender,
            recipient=recipient,
            message=message,
            status=(
                ContactRequest.Status.ACCEPTED
                if recipient.contact_permission == ContactPermission.OPEN
                else ContactRequest.Status.PENDING
            ),
        )
        notification_sender(contact_request)
        if contact_request.status == ContactRequest.Status.ACCEPTED:
            Conversation.get_or_create_for_users(sender, recipient)

    return contact_request, True
