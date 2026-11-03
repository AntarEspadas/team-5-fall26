"""Shared access checks for private conversations and conversation reports."""

from django.db.models import Q
from django.http import Http404
from django.shortcuts import get_object_or_404

from .models import ContactRequest, Conversation


def has_accepted_contact(first_user, second_user):
    """Return whether either direction has an accepted contact request."""
    return (
        ContactRequest.objects.filter(status=ContactRequest.Status.ACCEPTED)
        .filter(
            Q(sender=first_user, recipient=second_user)
            | Q(sender=second_user, recipient=first_user)
        )
        .exists()
    )


def get_accessible_conversation(user, conversation_id):
    """Return an approved participant's conversation, otherwise respond 404."""
    thread = get_object_or_404(
        Conversation.objects.select_related("user_one", "user_two").filter(
            Q(user_one=user) | Q(user_two=user)
        ),
        pk=conversation_id,
    )
    other_user = thread.other_participant(user)
    if not has_accepted_contact(user, other_user):
        raise Http404("Conversation not found.")
    return thread
