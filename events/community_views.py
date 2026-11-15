"""Views for event interests, discussions, and contact requests."""

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.db import transaction
from django.shortcuts import get_object_or_404, redirect
from django.urls import reverse
from django.utils import timezone
from django.views.decorators.http import require_POST

from accounts.ban_responses import banned_response
from accounts.ban_services import get_active_ban
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


CLOSED_EVENT = (
    "This event has ended or was cancelled, so it no longer takes new activity. "
    "Browse upcoming events instead."
)


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
    # Keep compatibility with requests that do not specify a status.
    status = request.POST.get(
        "status",
        EventInterest.Status.INTERESTED,
    )

    if (
        status != EventInterest.Status.INTERESTED
        and get_active_ban(request.user) is not None
    ):
        return banned_response(request)

    event = get_open_event(event_id)
    if event is None:
        messages.error(request, CLOSED_EVENT)
        return redirect(event_detail_url(event_id))

    valid_statuses = {
        EventInterest.Status.INTERESTED,
    }

    if status not in valid_statuses:
        messages.error(
            request, "That choice isn't available. Reload the page and try again."
        )
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
            messages.success(request, "Marked as interested.")
        elif interest.status == status:
            interest.delete()
            messages.success(request, "Removed from your interested events.")
        else:
            interest.status = status
            interest.save(update_fields=["status"])
            messages.success(request, "Marked as interested.")

    return redirect(event_detail_url(event_id))


@login_required
@require_POST
def create_event_post(request, event_id):
    """Create a discussion post only when the event is upcoming."""
    event = get_object_or_404(Event, pk=event_id)

    if event.status != Event.Status.UPCOMING:
        messages.error(request, CLOSED_EVENT)
        return redirect(event_detail_url(event_id))

    form = EventPostForm(request.POST)

    if form.is_valid():
        post = form.save(commit=False)
        post.event = event
        post.author = request.user
        post.save()
        messages.success(request, "Comment posted.")
    else:
        messages.error(request, "Write a comment before posting.")

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
        messages.error(request, CLOSED_EVENT)
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
        messages.success(request, "Reply posted.")
    else:
        messages.error(request, "Write a reply before posting.")

    return redirect(event_detail_url(post.event_id))


@login_required
@require_POST
def send_contact_request(request, event_id, user_id):
    """Send a contact request to a visible attendee of the same event."""
    event = get_open_event(event_id)

    if event is None:
        messages.error(request, CLOSED_EVENT)
        return redirect(event_detail_url(event_id))

    recipient = get_object_or_404(
        User,
        pk=user_id,
        share_profile_with_attendees=True,
        event_interests__event=event,
    )

    if recipient.pk == request.user.pk:
        messages.error(request, "You can't send a contact request to yourself.")
        return redirect(event_detail_url(event_id))

    if not EventInterest.objects.filter(
        event=event,
        user=request.user,
    ).exists():
        messages.error(
            request,
            "Mark yourself as interested in this event first, "
            "then send the request.",
        )
        return redirect(event_detail_url(event_id))

    if get_active_ban(recipient) is not None:
        messages.error(
            request, "Contact requests are currently unavailable for this member."
        )
        return redirect(event_detail_url(event_id))

    form = ContactRequestForm(request.POST)

    if not form.is_valid():
        messages.error(
            request, "Your message is too long. Shorten it and send the request again."
        )
        return redirect(event_detail_url(event_id))

    contact_request, created = create_contact_request(
        event,
        request.user,
        recipient,
        form.cleaned_data["message"],
        notification_sender=notify_contact_request,
    )
    if contact_request is None:
        if get_active_ban(recipient) is not None:
            messages.error(
                request, "Contact requests are currently unavailable for this member."
            )
        else:
            messages.error(
                request,
                "This contact request can't be sent. Make sure you are both "
                "interested in this upcoming event, then try again.",
            )
    elif contact_request.status == ContactRequest.Status.ACCEPTED:
        messages.success(request, "You're connected. You can message them now.")
    elif created:
        messages.success(request, "Contact request sent.")
    else:
        messages.info(request, "Your contact request is already waiting for a reply.")

    if contact_request is None:
        messages.error(request, "This contact request is no longer available.")
    elif not created:
        if contact_request.status == ContactRequest.Status.ACCEPTED:
            messages.info(request, "You are already connected with this person.")
        elif contact_request.sender_id == request.user.pk:
            messages.info(
                request, "Your contact request is already awaiting a response."
            )
        else:
            messages.info(
                request,
                "This person already sent you a request. Respond in notifications.",
            )
    elif contact_request.status == ContactRequest.Status.ACCEPTED:
        messages.success(request, "You can now message this person.")
    else:
        messages.success(request, "Your contact request was sent.")

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
            messages.error(request, "That action isn't available. Try again.")
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

    if action == "accept":
        messages.success(request, "Contact request accepted. You can message them now.")
    else:
        messages.success(request, "Contact request declined.")
    return redirect(destination)
