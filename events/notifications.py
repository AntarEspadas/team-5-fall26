"""Event announcements and in-app contact request notifications."""

from django.conf import settings
from django.contrib.auth import get_user_model
from django.core.mail import send_mass_mail
from django.utils import dateformat, timezone
from django.utils.text import Truncator

from .filters import NYC_TIMEZONE
from .models import ContactRequest, Notification

# Edits to these fields send interested users an automatic notice.
NOTIFIED_CHANGES = ("start_time", "end_time", "location")
# Event titles can be longer than a notification subject allows.
SUBJECT_MAX_LENGTH = Notification._meta.get_field("subject").max_length


def interested_recipients(event):
    """Return the users interested in an event, other than its host."""
    return (
        get_user_model()
        .objects.filter(event_interests__event=event)
        .exclude(pk=event.host_id)
        .distinct()
    )


def notify_interested(event, subject, body, event_url):
    """Send a message to everyone interested in an event.

    Each recipient gets an in-app notification and their own email, so no
    one sees anyone else's address.

    Returns:
        int: how many people were notified.
    """
    recipients = list(interested_recipients(event))
    subject = Truncator(subject).chars(SUBJECT_MAX_LENGTH)
    Notification.objects.bulk_create(
        Notification(recipient=user, event=event, subject=subject, body=body)
        for user in recipients
    )
    email_body = f"{body}\n\nEvent details: {event_url}"
    send_mass_mail(
        [
            (subject, email_body, settings.DEFAULT_FROM_EMAIL, [user.email])
            for user in recipients
        ]
    )
    return len(recipients)


def notify_contact_request(contact_request):
    """Create at most one recipient notification for a saved request."""
    pending = contact_request.status == ContactRequest.Status.PENDING
    subject = "Contact request" if pending else "Introduction"
    notification, _ = Notification.objects.get_or_create(
        contact_request=contact_request,
        defaults={
            "recipient_id": contact_request.recipient_id,
            "event_id": contact_request.event_id,
            "subject": f"{subject} from {contact_request.sender.get_display_name()}",
            "body": contact_request.message,
            "read_at": None if pending else timezone.now(),
        },
    )
    return notification


def describe_when(start, end):
    """Describe an event's date and time in New York time, e.g. for emails."""
    if not start:
        return "To be announced"
    when = dateformat.format(
        timezone.localtime(start, NYC_TIMEZONE), r"l, F j, Y \a\t g:i A"
    )
    if end:
        when += " – " + dateformat.format(
            timezone.localtime(end, NYC_TIMEZONE), "g:i A"
        )
    return when


def change_notice(event, previous, changed_fields):
    """Return the subject and body announcing an edit, or None if not needed.

    Args:
        event: the event with its new details.
        previous: the field values before the edit.
        changed_fields: names of the fields the host changed.
    """
    if not set(NOTIFIED_CHANGES) & set(changed_fields):
        return None
    lines = []
    if {"start_time", "end_time"} & set(changed_fields):
        before = describe_when(previous["start_time"], previous["end_time"])
        after = describe_when(event.start_time, event.end_time)
        lines.append(f"When: {after} (was {before})")
    if "location" in changed_fields:
        lines.append(f"Where: {event.location} (was {previous['location']})")
    host = event.host.get_display_name()
    body = f"{host} changed the details of {event.title}:\n\n" + "\n".join(lines)
    return f"Update: {event.title} has changed", body


def cancel_notice(event, message=""):
    """Return the subject and body announcing that a host cancelled an event.

    Args:
        event: the cancelled event.
        message: the host's optional note to attendees.
    """
    host = event.host.get_display_name()
    lines = [
        f"{host} cancelled {event.title}.",
        "",
        f"It was planned for: {describe_when(event.start_time, event.end_time)}",
    ]
    if event.location:
        lines.append(f"Where: {event.location}")
    if message:
        lines += ["", f"Message from {host}:", message]
    return f"Cancelled: {event.title}", "\n".join(lines)
