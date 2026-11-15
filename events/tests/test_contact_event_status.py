"""Contact requests must not refer to events that are already over."""

from datetime import timedelta

from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from accounts.models import User
from events.contact_services import create_contact_request
from events.models import ContactRequest, Event, EventInterest


class ContactEventStatusTests(TestCase):
    """Recheck real event availability, not just a stale status field."""

    @classmethod
    def setUpTestData(cls):
        cls.sender = User.objects.create_user(
            username="expired_sender", email="expired_sender@example.com"
        )
        cls.recipient = User.objects.create_user(
            username="expired_recipient",
            email="expired_recipient@example.com",
            share_profile_with_attendees=True,
        )

    def event_with_interest(self, guid, **fields):
        """Create a source event that both users marked Interested."""
        event = Event.objects.create(
            guid=guid,
            title=fields.pop("title", guid),
            content_hash="e" * 64,
            **fields,
        )
        for user in (self.sender, self.recipient):
            EventInterest.objects.create(event=event, user=user)
        return event

    def test_stale_upcoming_event_that_ended_is_rejected(self):
        """The event's status can lag behind the actual date."""
        event = self.event_with_interest(
            "past-event",
            start_time=timezone.now() - timedelta(days=3),
        )
        self.assertEqual(event.status, Event.Status.UPCOMING)
        result, created = create_contact_request(
            event, self.sender, self.recipient, "Hello"
        )
        self.assertIsNone(result)
        self.assertFalse(created)
        self.assertFalse(ContactRequest.objects.exists())

    def test_cancelled_feed_title_is_rejected(self):
        """The feed marks cancellations in titles, not in status."""
        event = self.event_with_interest(
            "cancelled-title",
            title="CANCELLED Community Walk",
            start_time=timezone.now() + timedelta(days=4),
        )
        result, created = create_contact_request(
            event, self.sender, self.recipient, "Hello"
        )
        self.assertIsNone(result)
        self.assertFalse(created)

    def test_valid_upcoming_event_still_allows_contact(self):
        """Normal contact requests continue to work."""
        event = self.event_with_interest(
            "future-event",
            start_time=timezone.now() + timedelta(days=4),
        )
        result, created = create_contact_request(
            event, self.sender, self.recipient, "Hello"
        )
        self.assertTrue(created)
        self.assertEqual(result.event, event)

    def test_profile_form_only_lists_eligible_shared_events(self):
        """Past and cancelled events cannot be chosen in the profile form."""
        valid = self.event_with_interest(
            "future-available",
            start_time=timezone.now() + timedelta(days=4),
        )
        expired = self.event_with_interest(
            "ended-in-feed",
            start_time=timezone.now() - timedelta(days=4),
        )
        cancelled = self.event_with_interest(
            "feed-cancelled",
            title="   CANCELED Weekly Meetup",
            start_time=timezone.now() + timedelta(days=4),
        )
        self.client.force_login(self.sender)
        response = self.client.get(reverse("message_user", args=[self.recipient.pk]))
        self.assertContains(response, "Send Contact Request")
        event_choices = response.context["form"].fields["event"].queryset
        choice_ids = list(event_choices.values_list("pk", flat=True))
        self.assertEqual(choice_ids, [valid.pk])
        self.assertNotIn(expired.pk, choice_ids)
        self.assertNotIn(cancelled.pk, choice_ids)

    def test_profile_form_shows_empty_state_if_no_eligible_events(self):
        """Do not offer an unusable contact form when all events are over."""
        self.event_with_interest(
            "past-only", start_time=timezone.now() - timedelta(days=3)
        )
        self.event_with_interest(
            "cancelled-only",
            title="CANCELLED Weekend Meetup",
            start_time=timezone.now() + timedelta(days=3),
        )
        self.client.force_login(self.sender)
        url = reverse("message_user", args=[self.recipient.pk])
        response = self.client.get(url)
        self.assertContains(response, "You need to share an upcoming event")
        self.assertNotContains(response, 'class="message-form"')
        self.assertFalse(response.context["has_shared_events"])

    def test_cannot_post_an_expired_or_cancelled_event_from_profile(self):
        """Forged IDs for events removed from the choices must not be sent."""
        expired = self.event_with_interest(
            "expired-post", start_time=timezone.now() - timedelta(days=2)
        )
        cancelled = self.event_with_interest(
            "cancelled-post",
            title="CANCELED Game Night",
            start_time=timezone.now() + timedelta(days=2),
        )
        self.client.force_login(self.sender)
        url = reverse("message_user", args=[self.recipient.pk])
        for event in (expired, cancelled):
            with self.subTest(event=event.pk):
                response = self.client.post(
                    url, {"event": event.pk, "message": "Hello"}
                )
                self.assertEqual(response.status_code, 200)
                self.assertNotContains(response, 'class="message-form"')
                self.assertFalse(ContactRequest.objects.exists())
