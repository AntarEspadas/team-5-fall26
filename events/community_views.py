"""Views for event interests, discussions, and contact requests."""

from django.contrib.auth.decorators import login_required
from django.db import transaction
from django.shortcuts import get_object_or_404, redirect
from django.urls import reverse
from django.utils import timezone
from django.views.decorators.http import require_POST

from accounts.models import User

from .contact_services import create_contact_request
from .forms import ContactRequestForm, EventPostForm, EventReplyForm
from .models import (
    ContactRequest,
    Conversation,
    Event,
    EventInterest,
    EventPost,
    Notification,
)
from .notifications import notify_contact_request


def event_detail_url(event_id):
    """Return the URL for an event detail page."""
    return reverse("event_detail", args=[event_id])


def get_open_event(event_id):
    """Return an event that still accepts interest and contact interactions."""
    event = get_object_or_404(Event, pk=event_id)

    if event.status != Event.Status.UPCOMING:
        return None

    return event


@login_required
@require_POST
def toggle_event_interest(request, event_id):
    """Set, switch, or remove the current user's attendance intention."""
    event = get_open_event(event_id)

    if event is None:
        return redirect(event_detail_url(event_id))

    # Keep compatibility with requests that do not specify a status.
    status = request.POST.get(
        "status",
        EventInterest.Status.INTERESTED,
    )

    valid_statuses = {
        EventInterest.Status.INTERESTED,
        EventInterest.Status.WANT_TO_ATTEND,
    }

    if status not in valid_statuses:
        return redirect(event_detail_url(event_id))

    with transaction.atomic():
        interest = EventInterest.objects.filter(
            event=event,
            user=request.user,
        ).first()

        if interest is None:
            EventInterest.objects.create(
                event=event,
                user=request.user,
                status=status,
            )
        elif interest.status == status:
            interest.delete()
        else:
            interest.status = status
            interest.save(update_fields=["status"])

    return redirect(event_detail_url(event_id))


@login_required
@require_POST
def create_event_post(request, event_id):
    """Create a discussion post only when the event is upcoming."""
    event = get_object_or_404(Event, pk=event_id)

    if event.status != Event.Status.UPCOMING:
        return redirect(event_detail_url(event_id))

    form = EventPostForm(request.POST)

    if form.is_valid():
        post = form.save(commit=False)
        post.event = event
        post.author = request.user
        post.save()

    return redirect(event_detail_url(event_id))


@login_required
@require_POST
def create_event_reply(request, post_id):
    """Reply to a post only when its event is upcoming."""
    post = get_object_or_404(
        EventPost.objects.select_related("event", "author"),
        pk=post_id,
    )

    if post.event.status != Event.Status.UPCOMING:
        return redirect(event_detail_url(post.event_id))

    form = EventReplyForm(request.POST)

    if form.is_valid():
        with transaction.atomic():
            reply = form.save(commit=False)
            reply.post = post
            reply.author = request.user
            reply.save()

            # Notify the original post author, but never notify yourself.
            if post.author_id != request.user.pk:
                subject = f"New reply to your comment on {post.event.title}"
                Notification.objects.create(
                    recipient=post.author,
                    event=post.event,
                    subject=subject[
                        : Notification._meta.get_field("subject").max_length
                    ],
                    body=(
                        f"{request.user.get_display_name()} replied to "
                        f"your comment on '{post.event.title}'."
                    ),
                )

    return redirect(event_detail_url(post.event_id))


@login_required
@require_POST
def send_contact_request(request, event_id, user_id):
    """Send a contact request to a visible attendee of the same event."""
    event = get_open_event(event_id)

    if event is None:
        return redirect(event_detail_url(event_id))

    recipient = get_object_or_404(
        User,
        pk=user_id,
        share_profile_with_attendees=True,
        event_interests__event=event,
    )

    if recipient.pk == request.user.pk:
        return redirect(event_detail_url(event_id))

    if not EventInterest.objects.filter(
        event=event,
        user=request.user,
    ).exists():
        return redirect(event_detail_url(event_id))

    form = ContactRequestForm(request.POST)

    if not form.is_valid():
        return redirect(event_detail_url(event_id))

    create_contact_request(
        event,
        request.user,
        recipient,
        form.cleaned_data["message"],
        notification_sender=notify_contact_request,
    )

    return redirect(event_detail_url(event_id))


@login_required
@require_POST
def respond_to_contact_request(request, request_id, action):
    """Accept or decline a pending contact request addressed to the user."""
    with transaction.atomic():
        contact_request = get_object_or_404(
            ContactRequest.objects.select_for_update(),
            pk=request_id,
            recipient=request.user,
            status=ContactRequest.Status.PENDING,
        )
        destination = (
            "notifications"
            if request.POST.get("return_to") == "notifications"
            else event_detail_url(contact_request.event_id)
        )

        if action == "accept":
            contact_request.status = ContactRequest.Status.ACCEPTED
        elif action == "decline":
            contact_request.status = ContactRequest.Status.DECLINED
        else:
            return redirect(destination)

        contact_request.save(update_fields=["status", "updated_at"])
        if action == "accept":
            Conversation.get_or_create_for_users(
                contact_request.sender,
                contact_request.recipient,
            )
        Notification.objects.filter(
            contact_request=contact_request,
            recipient=request.user,
            read_at__isnull=True,
        ).update(read_at=timezone.now())

    return redirect(destination)
