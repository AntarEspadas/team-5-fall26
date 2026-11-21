"""Feed events nobody can sign up for are kept out of lists (#158)."""

from datetime import timedelta

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from chatbot.tools import Toolbox
from events.models import Event, SearchHistory
from events.recommendations import recommended_events

REGISTER = "https://example.com/register"


class ListedEventsTests(TestCase):
    """Only hosted events and feed events with a registration link are listed."""

    @classmethod
    def setUpTestData(cls):
        cls.user = get_user_model().objects.create_user(
            username="ari", email="ari@example.com", password="pass12345"
        )
        cls.host = get_user_model().objects.create_user(
            username="host", email="host@example.com", password="pass12345"
        )
        start = timezone.now() + timedelta(days=2)

        def event(guid, title, **fields):
            return Event.objects.create(
                guid=guid,
                title=title,
                start_time=start,
                end_time=start + timedelta(hours=2),
                content_hash="",
                **fields,
            )

        cls.with_link = event("feed-link", "Kayak Lesson", registration_url=REGISTER)
        cls.no_link = event("feed-nolink", "Kayak Drop In")
        cls.hosted = event(
            Event.HOSTED_GUID_PREFIX + "kayak", "Kayak Meetup", host=cls.host
        )
        past = timezone.now() - timedelta(days=2)
        cls.past_with_link = event(
            "feed-old-link",
            "Kayak Lesson Archive",
            registration_url=REGISTER,
            status=Event.Status.DONE,
        )
        cls.past_no_link = event(
            "feed-old-nolink", "Kayak Drop In Archive", status=Event.Status.DONE
        )
        Event.objects.filter(
            pk__in=[cls.past_with_link.pk, cls.past_no_link.pk]
        ).update(start_time=past, end_time=past + timedelta(hours=2))

    def setUp(self):
        self.client.force_login(self.user)

    def test_listed_queryset(self):
        self.assertCountEqual(
            Event.objects.listed(),
            [self.with_link, self.hosted, self.past_with_link],
        )

    def test_search_leaves_out_events_without_a_link(self):
        response = self.client.get(reverse("search"), {"q": "kayak"})

        self.assertCountEqual(response.context["events"], [self.with_link, self.hosted])

    def test_archive_leaves_out_events_without_a_link(self):
        response = self.client.get(reverse("archived_events"))

        self.assertEqual(list(response.context["events"]), [self.past_with_link])

    def test_home_leaves_out_events_without_a_link(self):
        response = self.client.get(reverse("landing"))

        self.assertNotIn(self.no_link, response.context["events"])
        self.assertIn(self.with_link, response.context["events"])
        self.assertNotIn(self.past_no_link, response.context["archived_events"])

    def test_recommendations_leave_out_events_without_a_link(self):
        SearchHistory.objects.create(user=self.user, query="kayak")

        recommended = recommended_events(self.user)

        self.assertNotIn(self.no_link, recommended)
        self.assertIn(self.with_link, recommended)

    def test_assistant_search_leaves_out_events_without_a_link(self):
        found = Toolbox(self.user).search_events(query="kayak")

        titles = {event["title"] for event in found["events"]}
        self.assertEqual(titles, {"Kayak Lesson", "Kayak Meetup"})

    def test_unlisted_event_page_still_opens(self):
        """Old links and existing registrations keep working."""
        response = self.client.get(reverse("event_detail", args=[self.no_link.pk]))

        self.assertEqual(response.status_code, 200)
