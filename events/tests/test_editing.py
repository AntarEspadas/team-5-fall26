"""Tests for hosts editing the events they posted."""

from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from events.models import Event

NYC = ZoneInfo("America/New_York")


def as_input(moment):
    """Format a datetime the way the form's calendar input submits it."""
    return timezone.localtime(moment, NYC).strftime("%Y-%m-%dT%H:%M")


def start_of_today():
    """Return midnight at the start of today in New York."""
    return timezone.localtime(timezone.now(), NYC).replace(
        hour=0, minute=0, second=0, microsecond=0
    )


class EditTestCase(TestCase):
    """Shared users and a hosted event for the edit tests."""

    @classmethod
    def setUpTestData(cls):
        User = get_user_model()
        cls.host = User.objects.create_user(
            username="host", email="host@example.com", password="pass12345"
        )
        cls.other = User.objects.create_user(
            username="other", email="other@example.com", password="pass12345"
        )

    def setUp(self):
        self.event = self.hosted("yoga")
        self.url = reverse("event_edit", args=[self.event.pk])

    def hosted(self, guid, **fields):
        """Create an event posted by the host, upcoming by default."""
        defaults = {
            "title": "Sunset Yoga by the River",
            "description": "Bring a mat.",
            "start_time": datetime(2099, 1, 15, 18, 30, tzinfo=NYC),
            "end_time": datetime(2099, 1, 15, 19, 45, tzinfo=NYC),
            "location": "Pier 1, Brooklyn Bridge Park",
            "borough": Event.Borough.BROOKLYN,
            "categories": "Fitness | Yoga & Pilates",
            "registration_url": "https://example.org/tickets",
        }
        defaults.update(fields)
        return Event.objects.create(
            guid=Event.HOSTED_GUID_PREFIX + guid,
            host=self.host,
            content_hash="",
            **defaults,
        )

    def edit_data(self, event=None, **overrides):
        """Return the event's current details as a form submission."""
        event = event or self.event
        data = {
            "title": event.title,
            "description": event.description,
            "start_time": as_input(event.start_time),
            "end_time": as_input(event.end_time) if event.end_time else "",
            "location": event.location,
            "borough": event.borough,
            "categories": ["fitness", "yoga"],
            "image_url": event.image_url,
            "registration_url": event.registration_url,
        }
        data.update(overrides)
        return data


class EditButtonTests(EditTestCase):
    """Only the host sees an "Edit event" button, and only before it ends."""

    def detail(self, event):
        """Render an event's detail page."""
        return self.client.get(reverse("event_detail", args=[event.pk]))

    def test_host_sees_the_edit_button(self):
        """The host's own upcoming event links to its edit page."""
        self.client.force_login(self.host)

        self.assertContains(
            self.detail(self.event), f'href="{self.url}">Edit event</a>'
        )

    def test_other_visitors_do_not_see_the_edit_button(self):
        """Other users and logged-out visitors get no edit link."""
        self.assertNotContains(self.detail(self.event), "Edit event")

        self.client.force_login(self.other)
        self.assertNotContains(self.detail(self.event), "Edit event")

    def test_ended_and_feed_events_have_no_edit_button(self):
        """Hosts cannot edit events that ended, and feed events have no host."""
        ended = self.hosted(
            "ended",
            start_time=timezone.now() - timedelta(hours=3),
            end_time=timezone.now() - timedelta(hours=1),
        )
        feed = Event.objects.create(guid="123", title="Park Tour", content_hash="")
        self.client.force_login(self.host)

        self.assertNotContains(self.detail(ended), "Edit event")
        self.assertNotContains(self.detail(feed), "Edit event")


class EventUpdateViewTests(EditTestCase):
    """Hosts edit their events through the same validated form."""

    def test_logged_out_visitors_are_sent_to_login(self):
        """Editing requires an account."""
        response = self.client.get(self.url)

        self.assertRedirects(response, f"{reverse('login')}?next={self.url}")

    def test_only_the_host_can_open_or_submit_the_edit_form(self):
        """Other users get "not found" and cannot change the event."""
        self.client.force_login(self.other)

        self.assertEqual(self.client.get(self.url).status_code, 404)
        response = self.client.post(self.url, self.edit_data(title="Hijacked"))

        self.assertEqual(response.status_code, 404)
        self.event.refresh_from_db()
        self.assertEqual(self.event.title, "Sunset Yoga by the River")

    def test_feed_events_cannot_be_edited(self):
        """NYC Parks events have no host, so nobody can edit them."""
        feed = Event.objects.create(guid="123", title="Park Tour", content_hash="")
        self.client.force_login(self.host)

        response = self.client.get(reverse("event_edit", args=[feed.pk]))

        self.assertEqual(response.status_code, 404)

    def test_ended_events_cannot_be_edited(self):
        """Events past their end, done, or from an earlier day are closed."""
        now = timezone.now()
        closed = [
            self.hosted(
                "past-end",
                start_time=now - timedelta(hours=3),
                end_time=now - timedelta(minutes=1),
            ),
            self.hosted("done", status=Event.Status.DONE),
            self.hosted(
                "yesterday",
                start_time=start_of_today() - timedelta(hours=2),
                end_time=None,
            ),
        ]
        self.client.force_login(self.host)

        for event in closed:
            with self.subTest(event=event.guid):
                url = reverse("event_edit", args=[event.pk])
                self.assertEqual(self.client.get(url).status_code, 404)
                response = self.client.post(url, self.edit_data(event, title="Late"))
                self.assertEqual(response.status_code, 404)

    def test_form_opens_with_the_current_details(self):
        """Every saved detail is filled in, with times in New York time."""
        self.client.force_login(self.host)
        response = self.client.get(self.url)

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "<h1>Edit event</h1>")
        self.assertContains(response, 'value="Sunset Yoga by the River"')
        self.assertContains(response, "Bring a mat.</textarea>")
        self.assertContains(response, 'name="start_time" value="2099-01-15T18:30"')
        self.assertContains(response, 'name="end_time" value="2099-01-15T19:45"')
        self.assertContains(response, '<option value="brooklyn" selected>')
        self.assertContains(response, 'value="fitness" id="id_categories_0" checked')
        self.assertContains(response, 'value="yoga" id="id_categories_1" checked')
        self.assertNotContains(response, 'value="art" id="id_categories_3" checked')
        self.assertContains(response, 'value="https://example.org/tickets"')
        self.assertContains(response, "Save changes")
        self.assertContains(
            response, f'href="{reverse("event_detail", args=[self.event.pk])}"'
        )

    def test_saving_updates_the_event_everywhere(self):
        """Changes appear on the detail page, in search, and in filters."""
        self.client.force_login(self.host)
        response = self.client.post(
            self.url,
            self.edit_data(
                title="Moonlight Painting",
                start_time="2099-02-01T20:00",
                end_time="2099-02-01T22:00",
                borough="queens",
                categories=["art"],
            ),
        )

        detail_url = reverse("event_detail", args=[self.event.pk])
        self.assertRedirects(response, detail_url, fetch_redirect_response=False)
        self.event.refresh_from_db()
        self.assertEqual(self.event.title, "Moonlight Painting")
        self.assertEqual(self.event.start_time, datetime(2099, 2, 1, 20, tzinfo=NYC))
        self.assertEqual(self.event.borough, Event.Borough.QUEENS)
        self.assertEqual(self.event.categories, "Arts & Crafts")
        self.assertEqual(self.event.host, self.host)
        self.assertEqual(self.event.guid, Event.HOSTED_GUID_PREFIX + "yoga")

        detail = self.client.get(detail_url)
        self.assertContains(detail, "Event updated.")
        self.assertContains(detail, "Moonlight Painting")

        search = reverse("search")
        new_title = self.client.get(search, {"q": "moonlight"})
        old_title = self.client.get(search, {"q": "sunset"})
        self.assertEqual(list(new_title.context["events"]), [self.event])
        self.assertEqual(list(old_title.context["events"]), [])
        filters = {"borough": "queens", "category": "art", "date_from": "2099-02-01"}
        self.assertEqual(
            list(self.client.get(search, filters).context["events"]), [self.event]
        )
        brooklyn = self.client.get(search, {"borough": "brooklyn"})
        self.assertNotIn(self.event, brooklyn.context["events"])

    def test_invalid_changes_report_errors_and_keep_the_event(self):
        """The edit form uses the same validation as the create form."""
        self.client.force_login(self.host)
        response = self.client.post(
            self.url,
            self.edit_data(title="", end_time="2099-01-15T17:00", categories=[]),
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            set(response.context["form"].errors), {"title", "end_time", "categories"}
        )
        self.assertContains(response, "The end time must be after the start time.")
        self.event.refresh_from_db()
        self.assertEqual(self.event.title, "Sunset Yoga by the River")

    def test_events_under_way_can_still_be_corrected(self):
        """An unchanged past start time is fine; moving it into the past is not."""
        now = timezone.now()
        ongoing = self.hosted(
            "ongoing",
            start_time=(now - timedelta(hours=1)).replace(second=0, microsecond=0),
            end_time=now + timedelta(hours=2),
        )
        url = reverse("event_edit", args=[ongoing.pk])
        self.client.force_login(self.host)

        response = self.client.post(
            url, self.edit_data(ongoing, description="Meet by the flagpole.")
        )
        self.assertEqual(response.status_code, 302)
        ongoing.refresh_from_db()
        self.assertEqual(ongoing.description, "Meet by the flagpole.")

        earlier = as_input(ongoing.start_time - timedelta(hours=1))
        response = self.client.post(url, self.edit_data(ongoing, start_time=earlier))
        self.assertContains(response, "Choose a start time in the future.")


class HasEndedTests(EditTestCase):
    """Events know when they are over, using the same rule as the sync."""

    def test_has_ended(self):
        """End times, the done status, and start days decide it."""
        now = timezone.now()
        cases = {
            "future": ({}, False),
            "past-end": (
                {
                    "start_time": now - timedelta(hours=2),
                    "end_time": now - timedelta(1),
                },
                True,
            ),
            "done": ({"status": Event.Status.DONE}, True),
            "today-no-end": ({"start_time": start_of_today(), "end_time": None}, False),
            "yesterday-no-end": (
                {"start_time": start_of_today() - timedelta(hours=1), "end_time": None},
                True,
            ),
            "undated": ({"start_time": None, "end_time": None}, False),
        }
        for guid, (fields, expected) in cases.items():
            with self.subTest(guid=guid):
                self.assertEqual(self.hosted(guid, **fields).has_ended, expected)
