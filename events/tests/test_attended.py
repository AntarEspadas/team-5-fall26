"""Tests for the Attended tab on My Events (#163)."""

from datetime import timedelta

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from events.models import Event, Registration


def make_event(guid, title, days_from_now, **fields):
    """Create an event that starts ``days_from_now`` days away and lasts 2 hours."""
    start = timezone.now() + timedelta(days=days_from_now)
    fields.setdefault("content_hash", "")
    return Event.objects.create(
        guid=Event.HOSTED_GUID_PREFIX + guid,
        title=title,
        start_time=start,
        end_time=start + timedelta(hours=2),
        **fields,
    )


class AttendedTabTests(TestCase):
    """Attended lists only past events the user registered for, not cancelled."""

    @classmethod
    def setUpTestData(cls):
        cls.user = get_user_model().objects.create_user(
            username="ari", email="ari@example.com", password="pass12345"
        )
        cls.older = make_event("older", "Older Park Walk", -10)
        cls.recent = make_event("recent", "Recent Bird Count", -2)
        cls.future = make_event("future", "Future Kayak Day", 5)
        cls.host_cancelled = make_event(
            "host-cancelled",
            "Host Cancelled Picnic",
            -3,
            status=Event.Status.CANCELLED,
        )
        cls.feed_cancelled = make_event("feed-cancelled", "CANCELLED: Feed Concert", -4)
        cls.done = make_event("done", "Finished Cleanup", -6, status=Event.Status.DONE)
        for event, party in (
            (cls.older, 2),
            (cls.recent, 1),
            (cls.future, 3),
            (cls.host_cancelled, 1),
            (cls.feed_cancelled, 1),
            (cls.done, 4),
        ):
            Registration.objects.create(event=event, user=cls.user, party_size=party)

    def setUp(self):
        self.client.force_login(self.user)

    def attended(self):
        """Load My Events and return the response and the attended events."""
        response = self.client.get(reverse("my_events"))
        events = [r.event for r in response.context["attended_registrations"]]
        return response, events

    def test_only_past_uncancelled_events_most_recent_first(self):
        """Ended events are listed newest first; others are left out."""
        _, events = self.attended()

        self.assertEqual(events, [self.recent, self.done, self.older])

    def test_future_and_cancelled_events_are_not_in_attended(self):
        """Upcoming and cancelled registrations never show under Attended."""
        _, events = self.attended()

        for event in (self.future, self.host_cancelled, self.feed_cancelled):
            with self.subTest(event=event.title):
                self.assertNotIn(event, events)

    def test_attended_panel_text(self):
        """The panel is titled Attended Events and labels each event as ended."""
        response, _ = self.attended()
        html = response.content.decode()
        panel = html[
            html.index('id="attended-panel"') : html.index('id="favorites-panel"')
        ]

        self.assertIn("Attended Events", panel)
        self.assertIn("Past events you registered for, most recent first.", panel)
        self.assertIn("Recent Bird Count", panel)
        self.assertNotIn("Future Kayak Day", panel)
        self.assertNotIn("Host Cancelled Picnic", panel)
        self.assertNotIn("CANCELLED: Feed Concert", panel)
        self.assertNotIn("Event cancelled", panel)
        self.assertEqual(panel.count("Event ended"), 3)

    def test_upcoming_registration_still_shows_on_my_plans(self):
        """The future event moved out of Attended is still on My plans."""
        response = self.client.get(reverse("my_events"))
        planned = [
            plan["event"]
            for _, plans in response.context["plan_days"]
            for plan in plans
        ]

        self.assertIn(self.future, planned)

    def test_empty_state(self):
        """With nothing attended yet, the panel explains when events appear."""
        Registration.objects.filter(user=self.user).exclude(event=self.future).delete()

        response, events = self.attended()

        self.assertEqual(events, [])
        self.assertContains(
            response, "Events you register for will show up here after they end."
        )
