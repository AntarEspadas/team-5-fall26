"""Hosted events can't be dated so far ahead that pages crash (#179)."""

from datetime import datetime, timedelta

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.urls import reverse

from events.filters import NYC_TIMEZONE
from events.models import Event
from events.tests.test_hosting import listing_data

LIMIT_MESSAGE = "Choose a date within the next 2 years."


def as_input(moment):
    """Format a datetime the way the listing form's inputs submit it."""
    return moment.astimezone(NYC_TIMEZONE).strftime("%Y-%m-%dT%H:%M")


class EventDateLimitTests(TestCase):
    """Start and end dates must fall within the next two years."""

    @classmethod
    def setUpTestData(cls):
        cls.host = get_user_model().objects.create_user(
            username="farhost", email="farhost@example.com", password="pass12345"
        )

    def setUp(self):
        self.client.force_login(self.host)
        self.soon = datetime.now(NYC_TIMEZONE) + timedelta(days=7)

    def create(self, **overrides):
        """Submit the create form with valid data plus ``overrides``."""
        data = listing_data(
            start_time=as_input(self.soon),
            end_time=as_input(self.soon + timedelta(hours=2)),
        )
        data.update(overrides)
        return self.client.post(reverse("event_create"), data)

    def test_year_9999_start_is_rejected(self):
        """The date that used to save as the year 10000 in UTC is refused."""
        response = self.create(start_time="9999-12-31T23:59", end_time="")

        self.assertEqual(response.status_code, 200)
        self.assertFormError(response.context["form"], "start_time", LIMIT_MESSAGE)
        self.assertFalse(Event.objects.filter(host=self.host).exists())

    def test_far_future_end_is_rejected(self):
        """An end date years away is refused even when the start is soon."""
        response = self.create(end_time="9999-12-31T23:59")

        self.assertFormError(response.context["form"], "end_time", LIMIT_MESSAGE)
        self.assertFalse(Event.objects.filter(host=self.host).exists())

    def test_just_past_the_limit_is_rejected(self):
        """A start a few days past two years from now is refused."""
        later = datetime.now(NYC_TIMEZONE) + timedelta(days=735)
        response = self.create(
            start_time=as_input(later), end_time=as_input(later + timedelta(hours=1))
        )

        self.assertFormError(response.context["form"], "start_time", LIMIT_MESSAGE)
        self.assertFormError(response.context["form"], "end_time", LIMIT_MESSAGE)

    def test_dates_within_two_years_are_accepted(self):
        """A listing a year and a half ahead saves and its page loads."""
        later = datetime.now(NYC_TIMEZONE) + timedelta(days=540)
        response = self.create(
            start_time=as_input(later), end_time=as_input(later + timedelta(hours=1))
        )

        event = Event.objects.get(host=self.host)
        self.assertRedirects(response, reverse("event_detail", args=[event.pk]))
        self.assertEqual(self.client.get(response["Location"]).status_code, 200)

    def test_editing_other_fields_keeps_an_existing_far_date(self):
        """Only changed dates are checked, so old listings stay editable."""
        far = (datetime.now(NYC_TIMEZONE) + timedelta(days=1000)).replace(
            second=0, microsecond=0
        )
        event = Event.objects.create(
            guid=Event.HOSTED_GUID_PREFIX + "far-away",
            title="Far Away Picnic",
            host=self.host,
            start_time=far,
            end_time=far + timedelta(hours=2),
            content_hash="",
            location="Central Park",
            borough="manhattan",
            description="An old listing.",
            categories="Outdoors",
            registration_url="https://example.org/tickets",
        )
        data = listing_data(
            title="Far Away Picnic, renamed",
            start_time=as_input(event.start_time),
            end_time=as_input(event.end_time),
        )

        response = self.client.post(reverse("event_edit", args=[event.pk]), data)

        self.assertRedirects(response, reverse("event_detail", args=[event.pk]))
        event.refresh_from_db()
        self.assertEqual(event.title, "Far Away Picnic, renamed")
