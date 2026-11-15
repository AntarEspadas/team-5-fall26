"""Views for private attendee conversations."""

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.contrib.auth.mixins import LoginRequiredMixin
from django.db import transaction
from django.db.models import Count, Prefetch, Q
from django.http import Http404, JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils import timezone
from django.views.decorators.http import require_GET
from django.views.generic import ListView

from accounts.ban_responses import BAN_MESSAGE
from accounts.ban_services import get_active_ban
from accounts.models import User

from .contact_services import create_contact_request
from .conversation_services import get_accessible_conversation, has_accepted_contact
from .forms import ContactStartForm, DirectMessageForm
from .models import (
    ContactRequest,
    Conversation,
    DirectMessage,
    Event,
)


def _shared_events(first_user, second_user):
    """Return events both users marked as interested in."""
    return (
        Event.objects.filter(interests__user=first_user)
        .filter(interests__user=second_user)
        .distinct()
        .order_by("start_time", "title", "pk")
    )


def _shared_upcoming_events(first_user, second_user):
    """Return shared events that still accept new contact requests."""
    shared_events = _shared_events(first_user, second_user).filter(
        status=Event.Status.UPCOMING
    )
    # Use the same model rules as create_contact_request. In particular,
    # status alone can be stale and feed cancellations appear in the title.
    eligible_ids = [
        event.pk for event in shared_events if event.is_open and not event.is_cancelled
    ]
    return shared_events.filter(pk__in=eligible_ids)


def _message_data(message):
    """Return the public fields needed to render one chat bubble."""
    return {
        "id": message.pk,
        "sender_id": message.sender_id,
        "sender_name": message.sender.get_display_name(),
        "body": message.body,
        "created_at": message.created_at.isoformat(),
    }


class InboxView(LoginRequiredMixin, ListView):
    """Show the signed-in user's conversations, newest activity first."""

    template_name = "events/inbox.html"
    context_object_name = "conversations"
    paginate_by = 20

    def get_queryset(self):
        user = self.request.user
        latest_messages = DirectMessage.objects.select_related("sender").order_by(
            "-created_at", "-pk"
        )
        return (
            Conversation.objects.filter(Q(user_one=user) | Q(user_two=user))
            .select_related("user_one", "user_two")
            .annotate(
                unread_count=Count(
                    "messages",
                    filter=Q(messages__read_at__isnull=True)
                    & ~Q(messages__sender=user),
                )
            )
            .prefetch_related(
                Prefetch(
                    "messages",
                    queryset=latest_messages,
                    to_attr="ordered_messages",
                )
            )
            .order_by("-updated_at", "-pk")
        )

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        for thread in context["conversations"]:
            thread.other_user = thread.other_participant(self.request.user)
        return context


@login_required
def message_user(request, user_id):
    """Open an approved conversation or start the contact-request flow."""
    recipient = get_object_or_404(User, pk=user_id)
    if recipient.pk == request.user.pk:
        raise Http404("Conversation not found.")

    active_ban = get_active_ban(request.user)
    if has_accepted_contact(request.user, recipient):
        if active_ban is None:
            conversation, _ = Conversation.get_or_create_for_users(
                request.user, recipient
            )
        else:
            user_one_id, user_two_id = sorted((request.user.pk, recipient.pk))
            conversation = Conversation.objects.filter(
                user_one_id=user_one_id, user_two_id=user_two_id
            ).first()
        if conversation is not None:
            return redirect("conversation", conversation_id=conversation.pk)
    elif not recipient.share_profile_with_attendees:
        raise Http404("Profile not found.")

    if active_ban is not None:
        return render(
            request,
            "events/message_user.html",
            {"recipient": recipient, "ban_message": BAN_MESSAGE},
        )

    if get_active_ban(recipient) is not None:
        return render(
            request,
            "events/message_user.html",
            {"recipient": recipient, "recipient_banned": True},
        )

    shared_events = _shared_upcoming_events(request.user, recipient)
    pending_request = (
        ContactRequest.objects.filter(status=ContactRequest.Status.PENDING)
        .filter(
            Q(sender=request.user, recipient=recipient)
            | Q(sender=recipient, recipient=request.user)
        )
        .select_related("event")
        .first()
    )

    if request.method == "POST":
        form = ContactStartForm(
            request.POST,
            shared_events=shared_events,
        )
        if form.is_valid():
            event = form.cleaned_data["event"]
            contact_request, created = create_contact_request(
                event,
                request.user,
                recipient,
                form.cleaned_data["message"],
            )
            if contact_request is None:
                if get_active_ban(recipient) is not None:
                    error = (
                        "Contact requests are currently unavailable for this member."
                    )
                else:
                    error = "Choose an upcoming event that you both are interested in."
                messages.error(request, error)
                return redirect("message_user", user_id=recipient.pk)
            if contact_request.status == ContactRequest.Status.ACCEPTED:
                conversation, _ = Conversation.get_or_create_for_users(
                    request.user, recipient
                )
                return redirect("conversation", conversation_id=conversation.pk)
            if not created:
                messages.info(
                    request,
                    "Your contact request is already awaiting a response.",
                )
                return redirect("message_user", user_id=recipient.pk)

            messages.success(
                request,
                "Your contact request was sent. You can message once it is accepted.",
            )
            return redirect("message_user", user_id=recipient.pk)
    else:
        form = ContactStartForm(shared_events=shared_events)

    return render(
        request,
        "events/message_user.html",
        {
            "recipient": recipient,
            "form": form,
            "pending_request": pending_request,
            "has_shared_events": shared_events.exists(),
        },
    )


@login_required
def conversation(request, conversation_id):
    """Display a conversation and accept replies from either participant."""
    thread = get_accessible_conversation(request.user, conversation_id)
    other_user = thread.other_participant(request.user)

    if request.method == "POST":
        form = DirectMessageForm(request.POST)
        if form.is_valid():
            with transaction.atomic():
                message = DirectMessage.objects.create(
                    conversation=thread,
                    sender=request.user,
                    body=form.cleaned_data["body"],
                )
                thread.save(update_fields=["updated_at"])
            if request.headers.get("x-requested-with") == "XMLHttpRequest":
                message.sender = request.user
                return JsonResponse({"message": _message_data(message)})
            return redirect("conversation", conversation_id=thread.pk)
        if request.headers.get("x-requested-with") == "XMLHttpRequest":
            return JsonResponse(
                {"errors": form.errors.get_json_data()},
                status=400,
            )
    else:
        form = DirectMessageForm()

    DirectMessage.objects.filter(
        conversation=thread,
        read_at__isnull=True,
    ).exclude(
        sender=request.user,
    ).update(read_at=timezone.now())

    thread_messages = thread.messages.select_related("sender", "event").order_by(
        "created_at", "pk"
    )
    latest_message = thread.messages.order_by("-pk").first()
    return render(
        request,
        "events/conversation.html",
        {
            "conversation": thread,
            "other_user": other_user,
            "thread_messages": thread_messages,
            "latest_message_id": latest_message.pk if latest_message else 0,
            "updates_url": reverse("conversation_updates", args=[thread.pk]),
            "form": form,
        },
    )


@login_required
@require_GET
def conversation_updates(request, conversation_id):
    """Return new messages for one of the signed-in user's conversations."""
    thread = get_accessible_conversation(request.user, conversation_id)

    try:
        after_id = int(request.GET.get("after", "0"))
        if after_id < 0:
            raise ValueError
    except ValueError:
        return JsonResponse(
            {"error": "The after cursor must be a non-negative integer."},
            status=400,
        )

    new_messages = list(
        thread.messages.filter(pk__gt=after_id)
        .select_related("sender")
        .order_by("pk")[:100]
    )
    incoming_ids = [
        message.pk for message in new_messages if message.sender_id != request.user.pk
    ]
    if incoming_ids:
        DirectMessage.objects.filter(
            pk__in=incoming_ids,
            read_at__isnull=True,
        ).update(read_at=timezone.now())

    response = JsonResponse(
        {
            "messages": [_message_data(message) for message in new_messages],
        }
    )
    response["Cache-Control"] = "no-store"
    return response


@login_required
@require_GET
def unread_message_count(request):
    """Return the signed-in user's current unread direct-message count."""
    count = (
        DirectMessage.objects.filter(
            Q(conversation__user_one=request.user)
            | Q(conversation__user_two=request.user),
            read_at__isnull=True,
        )
        .exclude(sender=request.user)
        .count()
    )
    response = JsonResponse({"count": count})
    response["Cache-Control"] = "no-store"
    return response
