"""Tests for the Event model."""

from django.test import TestCase

from accounts.models import User
from events.models import (
    ContactRequest,
    Conversation,
    DirectMessage,
    Event,
    EventInterest,
    EventPost,
    EventReply,
)


class EventModelTests(TestCase):
    """Event rows default to upcoming and identify themselves by title."""

    def test_new_event_defaults_to_upcoming(self):
        """A freshly created event is upcoming until a sync marks it done."""
        event = Event.objects.create(guid="1", title="Yoga", content_hash="a" * 64)
        self.assertEqual(event.status, Event.Status.UPCOMING)
        self.assertEqual(event.get_status_display(), "Upcoming")
        self.assertEqual(Event.Status.DONE.value, "done")

    def test_str_returns_the_title(self):
        """Admin listings show the event title."""
        event = Event.objects.create(
            guid="2", title="Birdwatching", content_hash="b" * 64
        )
        self.assertEqual(str(event), "Birdwatching")

    def test_default_ordering_is_chronological(self):
        """Events are ordered by start time, then end time, then title."""
        self.assertEqual(Event._meta.ordering, ["start_time", "end_time", "title"])

    def test_primary_category_is_the_first_listed_category(self):
        """A card tag uses the first name in the pipe-separated category list."""
        event = Event.objects.create(
            guid="3",
            title="Zumba in the Park",
            categories="Fitness | Dance | Shape Up NYC",
            content_hash="c" * 64,
        )

        self.assertEqual(event.primary_category, "Fitness")

    def test_primary_category_handles_one_or_no_categories(self):
        """Events without a category list still return a clean, empty tag."""
        single = Event.objects.create(
            guid="4", title="Birdwatching", categories="Nature", content_hash="d" * 64
        )
        untagged = Event.objects.create(
            guid="5", title="Storytime Circle", content_hash="e" * 64
        )

        self.assertEqual(single.primary_category, "Nature")
        self.assertEqual(untagged.primary_category, "")

    def test_is_cancelled_follows_the_feed_title_prefix(self):
        """The feed's "CANCELLED" and "CANCELED" title prefixes both count."""
        cancelled = Event.objects.create(
            guid="6", title="CANCELLED: Yoga in the Park", content_hash="f" * 64
        )
        cancelled_us = Event.objects.create(
            guid="7", title="CANCELED: Tai Chi on the Lawn", content_hash="h" * 64
        )
        running = Event.objects.create(
            guid="8", title="Yoga in the Park", content_hash="g" * 64
        )

        self.assertTrue(cancelled.is_cancelled)
        self.assertTrue(cancelled_us.is_cancelled)
        self.assertFalse(running.is_cancelled)

    def test_community_and_message_models_have_useful_strings(self):
        """Admin-facing records identify their associated users and content."""
        sender = User.objects.create_user("model-sender", "sender@example.com")
        recipient = User.objects.create_user("model-recipient", "recipient@example.com")
        outsider = User.objects.create_user("model-outsider", "outsider@example.com")
        event = Event.objects.create(
            guid="community-models",
            title="Community Walk",
            content_hash="i" * 64,
        )
        interest = EventInterest.objects.create(event=event, user=sender)
        request = ContactRequest.objects.create(
            event=event,
            sender=sender,
            recipient=recipient,
        )
        conversation, _ = Conversation.get_or_create_for_users(sender, recipient)
        message = DirectMessage.objects.create(
            conversation=conversation,
            sender=sender,
            body="Hello",
        )
        post = EventPost.objects.create(event=event, author=sender, body="A post")
        reply = EventReply.objects.create(post=post, author=recipient, body="A reply")

        self.assertEqual(str(interest), f"{sender} — Interested — {event}")
        self.assertEqual(str(request), f"{sender} → {recipient} (pending)")
        self.assertIsNone(conversation.other_participant(outsider))
        with self.assertRaises(ValueError):
            Conversation.get_or_create_for_users(sender, sender)
        self.assertEqual(str(conversation), f"Conversation: {sender} ↔ {recipient}")
        self.assertEqual(
            str(message),
            f"Message from {sender} in conversation {conversation.pk}",
        )
        self.assertEqual(str(post), f"Post by {sender} on {event}")
        self.assertEqual(str(reply), f"Reply by {recipient} on post {post.pk}")
