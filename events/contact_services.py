"""Shared contact-request workflows."""

from django.db import transaction
from django.db.models import Q

from accounts.ban_services import get_active_ban, require_unbanned_user
from accounts.models import ContactPermission, User

from .models import ContactRequest, Conversation, Event, EventInterest, Registration
from .notifications import notify_contact_request


def participant_events(user):
    """Return events the user is interested in or registered for."""
    return Event.objects.filter(
        Q(pk__in=EventInterest.objects.filter(user=user).values("event_id"))
        | Q(pk__in=Registration.objects.filter(user=user).values("event_id"))
    )


def participates_in_event(user, event):
    """Both interest and registration count as event participation."""
    return participant_events(user).filter(pk=event.pk).exists()


def shared_upcoming_events(first_user, second_user):
    """Return shared events that still accept new contact requests."""
    shared = (
        participant_events(first_user)
        .filter(
            pk__in=participant_events(second_user).values("pk"),
            status=Event.Status.UPCOMING,
        )
        .order_by("start_time", "title", "pk")
    )
    eligible_ids = [
        event.pk for event in shared if event.is_open and not event.is_cancelled
    ]
    return shared.filter(pk__in=eligible_ids)


def create_contact_request(
    event,
    sender,
    recipient,
    message,
    notification_sender=notify_contact_request,
):
    """Create one eligible event contact request, or return an existing one."""
    with transaction.atomic():
        # Lock both users in the same order, including for requests sent in
        # opposite directions, to prevent duplicate pending invitations.
        locked = {
            member.pk: member
            for member in User.objects.select_for_update(no_key=True)
            .filter(pk__in=(sender.pk, recipient.pk))
            .order_by("pk")
        }
        sender = locked[sender.pk]
        recipient = locked[recipient.pk]
        require_unbanned_user(sender)
        if (
            not event.is_open
            or event.is_cancelled
            or sender.pk == recipient.pk
            or get_active_ban(recipient) is not None
            or not recipient.share_profile_with_attendees
            or not participates_in_event(sender, event)
            or not participates_in_event(recipient, event)
        ):
            return None, False

        # Pending/accepted contact is a user-pair relationship, not an
        # event-specific one. The earlier request takes priority.
        existing = (
            ContactRequest.objects.filter(
                Q(sender=sender, recipient=recipient)
                | Q(sender=recipient, recipient=sender),
                status__in=(
                    ContactRequest.Status.PENDING,
                    ContactRequest.Status.ACCEPTED,
                ),
            )
            .order_by("-created_at", "-pk")
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
