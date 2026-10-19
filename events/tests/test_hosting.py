"""Tests for event holders posting their own event listings."""

from datetime import datetime, timedelta
from unittest.mock import patch
from zoneinfo import ZoneInfo

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.urls import reverse

from events.forms import EventListingForm
from events.models import Event
from events.services import sync_events

NYC = ZoneInfo("America/New_York")
UTC = ZoneInfo("UTC")


def listing_data(**overrides):
    """Return a complete, valid submission of the event listing form."""
    data = {
        "title": "Sunset Yoga by the River",
        "description": "Bring a mat and unwind with an all-levels class.",
        "start_time": "2099-01-15T18:30",
        "end_time": "2099-01-15T19:45",
        "location": "Pier 1, Brooklyn Bridge Park",
        "borough": "brooklyn",
        "categories": ["yoga", "fitness"],
        "image_url": "https://example.org/yoga.png",
        "registration_url": "https://example.org/tickets",
    }
    data.update(overrides)
    return data


class EventCreateViewTests(TestCase):
    """Signed-in users post events through a validated form."""

    @classmethod
    def setUpTestData(cls):
        cls.host = get_user_model().objects.create_user(
            username="host",
            email="host@example.com",
            password="pass12345",
            display_name="River Yoga Collective",
        )

    def setUp(self):
        self.url = reverse("event_create")

    def post(self, **overrides):
        """Submit the form as the signed-in host."""
        self.client.force_login(self.host)
        return self.client.post(self.url, listing_data(**overrides))

    def test_logged_out_visitors_are_sent_to_login(self):
        """The form requires an account, then returns to it after login."""
        response = self.client.get(self.url)

        self.assertEqual(self.url, "/events/new/")
        self.assertRedirects(response, f"{reverse('login')}?next={self.url}")
        self.client.post(self.url, listing_data())
        self.assertFalse(Event.objects.exists())

    def test_home_page_links_signed_in_users_to_the_form(self):
        """Only signed-in visitors see the "Post an event" link."""
        link = f'href="{self.url}">Post an event</a>'
        self.assertNotContains(self.client.get(reverse("landing")), link)

        self.client.force_login(self.host)
        self.assertContains(self.client.get(reverse("landing")), link)

    def test_form_asks_for_every_listing_detail(self):
        """The form offers all fields, with calendars, boroughs, and categories."""
        self.client.force_login(self.host)
        response = self.client.get(self.url)

        self.assertEqual(response.status_code, 200)
        self.assertTemplateUsed(response, "events/event_form.html")
        for name in ("title", "description", "location", "image_url"):
            self.assertContains(response, f'name="{name}"')
        self.assertContains(response, 'type="datetime-local" name="start_time"')
        self.assertContains(response, 'type="datetime-local" name="end_time"')
        self.assertContains(response, '<option value="brooklyn">Brooklyn</option>')
        self.assertContains(response, 'name="categories" value="yoga"')
        self.assertContains(response, "Ticketing or registration link")

    def test_valid_submission_posts_the_event_for_its_host(self):
        """The event is saved in New York time and the host sees it posted."""
        response = self.post()

        event = Event.objects.get()
        self.assertRedirects(
            response,
            reverse("event_detail", args=[event.pk]),
            fetch_redirect_response=False,
        )
        self.assertEqual(event.host, self.host)
        self.assertTrue(event.guid.startswith(Event.HOSTED_GUID_PREFIX))
        self.assertEqual(event.start_time, datetime(2099, 1, 15, 18, 30, tzinfo=NYC))
        self.assertEqual(event.end_time, datetime(2099, 1, 15, 19, 45, tzinfo=NYC))
        self.assertEqual(event.borough, Event.Borough.BROOKLYN)
        self.assertEqual(event.categories, "Fitness | Yoga & Pilates")
        self.assertEqual(event.registration_url, "https://example.org/tickets")
        self.assertEqual(event.status, Event.Status.UPCOMING)

        detail = self.client.get(response.url)
        self.assertContains(detail, "Your event is posted.")
        self.assertContains(detail, "Hosted by <strong>River Yoga Collective</strong>")
        self.assertContains(detail, "(Brooklyn)")
        self.assertNotContains(self.client.get(response.url), "Your event is posted.")

    def test_each_posted_event_gets_its_own_id(self):
        """Hosts can post several events without clashing source IDs."""
        self.post()
        self.post(title="Second session")

        self.assertEqual(Event.objects.values("guid").distinct().count(), 2)

    def test_optional_fields_can_be_left_blank(self):
        """End time and both links are optional."""
        self.post(end_time="", image_url="", registration_url="")

        event = Event.objects.get()
        self.assertIsNone(event.end_time)
        self.assertEqual(event.image_url, "")
        self.assertEqual(event.registration_url, "")

    def test_required_fields_report_errors_and_keep_input(self):
        """Missing details show errors next to fields; typed values stay."""
        response = self.post(
            description="   ",
            location="",
            borough="",
            categories=[],
            start_time="",
        )

        self.assertEqual(response.status_code, 200)
        self.assertFalse(Event.objects.exists())
        form = response.context["form"]
        self.assertEqual(
            set(form.errors),
            {"description", "location", "borough", "categories", "start_time"},
        )
        self.assertContains(response, "Choose at least one category.")
        self.assertContains(response, 'value="Sunset Yoga by the River"')
        self.assertContains(response, 'value="https://example.org/tickets"')

    def test_start_time_must_be_in_the_future(self):
        """Events cannot be posted for a time that has already passed."""
        past = (datetime.now(NYC) - timedelta(hours=1)).strftime("%Y-%m-%dT%H:%M")
        response = self.post(start_time=past, end_time="")

        self.assertContains(response, "Choose a start time in the future.")
        self.assertFalse(Event.objects.exists())

    def test_end_time_must_be_after_the_start(self):
        """An end time before or equal to the start time is rejected."""
        for end_time in ("2099-01-15T17:00", "2099-01-15T18:30"):
            with self.subTest(end_time=end_time):
                response = self.post(end_time=end_time)

                self.assertContains(
                    response, "The end time must be after the start time."
                )
        self.assertFalse(Event.objects.exists())

    def test_links_must_be_valid(self):
        """Image and ticketing links must be real web addresses."""
        response = self.post(image_url="not a link", registration_url="javascript:1")

        self.assertEqual(
            set(response.context["form"].errors), {"image_url", "registration_url"}
        )
        self.assertFalse(Event.objects.exists())

    def test_imported_events_have_no_host_line(self):
        """NYC Parks events keep their detail page without a host."""
        event = Event.objects.create(guid="123", title="Park Tour", content_hash="")

        response = self.client.get(reverse("event_detail", args=[event.pk]))

        self.assertNotContains(response, "Hosted by")


class EventListingFormTests(TestCase):
    """The form shows stored times as New York wall-clock time."""

    def test_existing_times_are_displayed_in_new_york_time(self):
        """A stored UTC time appears as the matching local time in the input."""
        form = EventListingForm(
            initial={"start_time": datetime(2099, 1, 15, 23, 30, tzinfo=UTC)}
        )

        self.assertIn('value="2099-01-15T18:30"', str(form["start_time"]))


class HostedEventDiscoveryTests(TestCase):
    """Posted events are discoverable like NYC Parks events."""

    @classmethod
    def setUpTestData(cls):
        cls.host = get_user_model().objects.create_user(
            username="host", email="host@example.com", password="pass12345"
        )

    def setUp(self):
        self.client.force_login(self.host)
        self.client.post(reverse("event_create"), listing_data())
        self.event = Event.objects.get()

    def test_posted_event_appears_on_the_home_page(self):
        """The home page's upcoming list includes hosted events."""
        response = self.client.get(reverse("landing"))

        self.assertIn(self.event, response.context["events"])

    def test_posted_event_appears_in_search(self):
        """Full-text search finds hosted events by their title."""
        response = self.client.get(reverse("search"), {"q": "sunset yoga"})

        self.assertEqual(list(response.context["events"]), [self.event])

    def test_posted_event_matches_borough_category_and_date_filters(self):
        """Every filter uses the borough and categories the host chose."""
        matching = {
            "borough": "brooklyn",
            "category": "yoga",
            "date_from": "2099-01-15",
            "date_to": "2099-01-15",
        }
        response = self.client.get(reverse("search"), matching)
        self.assertEqual(list(response.context["events"]), [self.event])

        for wrong in (
            {"borough": "queens"},
            {"category": "art"},
            {"date_from": "2099-01-16"},
        ):
            with self.subTest(wrong=wrong):
                response = self.client.get(reverse("search"), wrong)
                self.assertNotIn(self.event, response.context["events"])


class HostedEventSyncTests(TestCase):
    """The NYC Parks sync never edits hosted events, but retires past ones."""

    NOW = datetime(2099, 1, 15, 12, 0, tzinfo=NYC)

    @classmethod
    def setUpTestData(cls):
        cls.host = get_user_model().objects.create_user(
            username="host", email="host@example.com", password="pass12345"
        )

    def hosted(self, guid, **fields):
        """Create a hosted event directly in the database."""
        return Event.objects.create(
            guid=Event.HOSTED_GUID_PREFIX + guid,
            host=self.host,
            title=fields.pop("title", guid),
            content_hash="",
            **fields,
        )

    def sync(self, records=()):
        """Run the sync at a fixed time against the given feed records."""
        with (
            patch("events.services.fetch_feed", return_value=list(records)),
            patch("events.services.timezone.now", return_value=self.NOW),
        ):
            return sync_events()

    def test_sync_leaves_hosted_events_unchanged(self):
        """Importing the feed neither edits nor removes hosted events."""
        event = self.hosted(
            "kept",
            title="Community Cleanup",
            start_time=datetime(2099, 1, 20, 9, tzinfo=NYC),
            end_time=datetime(2099, 1, 20, 11, tzinfo=NYC),
            borough="queens",
        )
        updated_at = event.updated_at

        counts = self.sync([{"guid": "999", "title": "Park Tour"}])

        event.refresh_from_db()
        self.assertEqual(counts["created"], 1)
        self.assertEqual(event.title, "Community Cleanup")
        self.assertEqual(event.borough, "queens")
        self.assertEqual(event.status, Event.Status.UPCOMING)
        self.assertEqual(event.updated_at, updated_at)

    def test_finished_hosted_events_are_marked_done(self):
        """Hosted events end like feed events; without an end, after their day."""
        ended = self.hosted(
            "ended",
            start_time=self.NOW - timedelta(hours=3),
            end_time=self.NOW - timedelta(hours=1),
        )
        yesterday = self.hosted("yesterday", start_time=self.NOW - timedelta(days=1))
        this_morning = self.hosted(
            "this-morning", start_time=self.NOW - timedelta(hours=4)
        )
        feed_without_end = Event.objects.create(
            guid="555", title="Feed event", start_time=self.NOW - timedelta(days=1)
        )

        counts = self.sync()

        self.assertEqual(counts["marked_done"], 2)
        for event, status in (
            (ended, Event.Status.DONE),
            (yesterday, Event.Status.DONE),
            (this_morning, Event.Status.UPCOMING),
            (feed_without_end, Event.Status.UPCOMING),
        ):
            with self.subTest(event=event.guid):
                event.refresh_from_db()
                self.assertEqual(event.status, status)
