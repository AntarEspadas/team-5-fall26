"""Tests for the landing page and its event list."""

from datetime import datetime
from zoneinfo import ZoneInfo

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.urls import reverse

from events.models import Event
from events.views import UPCOMING_EVENT_LIMIT

NYC = ZoneInfo("America/New_York")


def make_event(guid, title, **kwargs):
    """Create an event row with sensible defaults for view tests."""
    kwargs.setdefault("content_hash", "v" * 64)
    return Event.objects.create(guid=guid, title=title, **kwargs)


class LandingViewTests(TestCase):
    """The landing page shows events to signed-in visitors only."""

    @classmethod
    def setUpTestData(cls):
        cls.user = get_user_model().objects.create_user(
            username="alice", email="alice@example.com", password="pass12345"
        )

    def setUp(self):
        self.alpha_event = make_event(
            "a1",
            "Alpha Yoga",
            start_time=datetime(2099, 1, 15, 9, 0, tzinfo=NYC),
            end_time=datetime(2099, 1, 15, 10, 0, tzinfo=NYC),
            location="Prospect Park",
            categories="Fitness | Yoga",
            description="Stretch and breathe in the park.",
            event_url="https://example.org/alpha",
            registration_url="https://example.org/alpha/register",
            image_url="https://example.org/img/yoga.png",
        )
        make_event(
            "b2",
            "Beta Birdwatching",
            start_time=datetime(2099, 1, 16, 10, 0, tzinfo=NYC),
            end_time=datetime(2099, 1, 16, 12, 0, tzinfo=NYC),
            park_names="Central Park",
        )
        make_event(
            "c3",
            "Hidden Past Event",
            start_time=datetime(2099, 1, 14, 9, 0, tzinfo=NYC),
            status=Event.Status.DONE,
        )

    def test_anonymous_visitors_see_the_login_prompt(self):
        """Logged-out visitors get the welcome page without an event list."""
        response = self.client.get(reverse("landing"))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Please log in")
        self.assertNotContains(response, "Alpha Yoga")
        self.assertEqual(list(response.context["events"]), [])

    def test_signed_in_visitors_see_upcoming_events_in_order(self):
        """Upcoming events are listed soonest first; done events stay hidden."""
        make_event(
            "d4",
            "Undated Event",
            start_time=None,
            end_time=datetime(2099, 1, 20, 9, 0, tzinfo=NYC),
        )
        self.client.login(username="alice", password="pass12345")

        response = self.client.get(reverse("landing"))

        self.assertContains(response, "Alpha Yoga")
        self.assertContains(response, "Beta Birdwatching")
        self.assertContains(response, "Undated Event")
        self.assertNotContains(response, "Hidden Past Event")
        self.assertEqual(
            [event.title for event in response.context["events"]],
            ["Alpha Yoga", "Beta Birdwatching", "Undated Event"],
        )

    def test_event_card_shows_details(self):
        """Cards render the time in NYC local time, place, and useful links."""
        self.client.login(username="alice", password="pass12345")

        response = self.client.get(reverse("landing"))

        self.assertContains(response, "Jan 15")
        self.assertContains(response, "9:00 AM")
        self.assertContains(response, "10:00 AM")
        self.assertContains(response, "Prospect Park")
        self.assertContains(response, "Fitness | Yoga")
        self.assertContains(
            response,
            f'href="{reverse("event_detail", args=[self.alpha_event.pk])}"',
        )
        self.assertContains(response, ">Register</a>")
        self.assertContains(response, "Stretch and breathe in the park.")

    def test_images_use_the_stored_url_or_a_placeholder(self):
        """Events with an image link to it; the rest fall back to a placeholder."""
        self.client.login(username="alice", password="pass12345")

        response = self.client.get(reverse("landing"))
        placeholder = "/static/events/no-image.svg"

        self.assertContains(
            response,
            'src="https://example.org/img/yoga.png"',
        )
        self.assertContains(response, f'data-placeholder="{placeholder}"')
        self.assertContains(response, f'src="{placeholder}"')

    def test_empty_state_message_is_shown_without_events(self):
        """A brand-new install explains that no events are synced yet."""
        Event.objects.all().delete()
        self.client.login(username="alice", password="pass12345")

        response = self.client.get(reverse("landing"))

        self.assertContains(response, "No upcoming events yet.")

    def test_cancelled_events_show_a_label_instead_of_register(self):
        """A "CANCELLED" title swaps the registration link for a plain label."""
        cancelled = make_event(
            "e5",
            "CANCELLED: Twilight Kayak Tour",
            start_time=datetime(2099, 1, 17, 19, 0, tzinfo=NYC),
            registration_url="https://example.org/e5/register",
        )
        self.client.login(username="alice", password="pass12345")

        response = self.client.get(reverse("landing"))

        self.assertContains(response, "Cancelled")
        self.assertNotContains(response, f'href="{cancelled.registration_url}"')

    def test_event_list_is_capped(self):
        """Only the first 20 upcoming events are loaded per page view."""
        for index in range(UPCOMING_EVENT_LIMIT + 5):
            make_event(
                f"bulk-{index}",
                f"Bulk Event {index}",
                start_time=datetime(2099, 2, 1 + index, 9, 0, tzinfo=NYC),
            )
        self.client.login(username="alice", password="pass12345")

        response = self.client.get(reverse("landing"))

        self.assertEqual(len(response.context["events"]), UPCOMING_EVENT_LIMIT)


class EventDetailViewTests(TestCase):
    """Tests for the event details page."""

    def setUp(self):
        self.event = make_event(
            "detail-1",
            "Community Yoga",
            start_time=datetime(2099, 3, 15, 9, 0, tzinfo=NYC),
            end_time=datetime(2099, 3, 15, 10, 0, tzinfo=NYC),
            location="Prospect Park",
            categories="Fitness | Yoga",
            description="Join us for a community yoga session.",
            event_url="https://example.org/yoga",
            registration_url="https://example.org/yoga/register",
            image_url="https://example.org/yoga.png",
        )
        self.url = reverse("event_detail", args=[self.event.pk])

    def test_event_details_page_displays_event_information(self):
        """The details page displays the selected event's information."""
        response = self.client.get(self.url)

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context["event"], self.event)
        self.assertContains(response, "Community Yoga")
        self.assertContains(response, "Prospect Park")
        self.assertContains(response, "Fitness | Yoga")
        self.assertContains(response, "Join us for a community yoga session.")

    def test_event_details_page_shows_available_links(self):
        """Official and registration links open in the current tab."""
        response = self.client.get(self.url)

        self.assertContains(response, 'href="https://example.org/yoga"')
        self.assertContains(response, 'href="https://example.org/yoga/register"')
        self.assertNotContains(response, 'target="_blank"')

    def test_missing_event_information_is_handled(self):
        """Missing optional information does not break the details page."""
        event = make_event("detail-2", "Event With Missing Information")
        response = self.client.get(reverse("event_detail", args=[event.pk]))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Event With Missing Information")
        self.assertContains(response, "To be announced")
        self.assertContains(response, "No description is available for this event.")
        self.assertContains(response, 'alt="No image available"')
        self.assertNotContains(response, ">Register</a>")
        self.assertNotContains(response, ">Official Event Page</a>")

    def test_missing_event_returns_404(self):
        """A nonexistent event ID returns a 404 response."""
        response = self.client.get(reverse("event_detail", args=[999999999]))

        self.assertEqual(response.status_code, 404)

    def test_community_section_is_displayed(self):
        """The event detail page displays the community features."""
        response = self.client.get(reverse("event_detail", args=[self.event.pk]))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Connect with Other Attendees")
        self.assertContains(response, "Attendees who share their profiles")
        self.assertContains(response, "Discuss This Event")
        self.assertContains(response, "No discussion posts yet.")
