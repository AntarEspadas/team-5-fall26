"""Tests for the My plans timeline on My Events (#141)."""

from datetime import date, datetime, timezone as dt_timezone
from zoneinfo import ZoneInfo

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.urls import reverse

from events.models import Event, Registration

NYC = ZoneInfo("America/New_York")


def make_event(guid, title, host=None, **fields):
    """Create an event, posted by ``host`` when one is given."""
    fields.setdefault("content_hash", "")
    return Event.objects.create(
        guid=Event.HOSTED_GUID_PREFIX + guid, title=title, host=host, **fields
    )


class MyPlansTests(TestCase):
    """My Events opens on a day-by-day timeline of upcoming plans."""

    @classmethod
    def setUpTestData(cls):
        User = get_user_model()
        cls.user = User.objects.create_user(
            username="maya", email="maya@example.com", password="pass12345"
        )
        cls.other = User.objects.create_user(
            username="omar", email="omar@example.com", password="pass12345"
        )

    def setUp(self):
        self.client.force_login(self.user)

    def register(self, event, party_size=1):
        """Register the signed-in user for ``event``."""
        return Registration.objects.create(
            event=event, user=self.user, party_size=party_size
        )

    def get_plans(self):
        """Load My Events and return the response and its plan days."""
        response = self.client.get(reverse("my_events"))
        return response, response.context["plan_days"]

    def test_hosted_and_registered_events_are_grouped_by_day_in_order(self):
        """Plans are sorted by start time and grouped under their NYC date."""
        later = make_event(
            "later",
            "Evening Concert",
            host=self.other,
            start_time=datetime(2099, 1, 16, 19, 0, tzinfo=NYC),
        )
        hosted = make_event(
            "hosted",
            "Morning Cleanup",
            host=self.user,
            start_time=datetime(2099, 1, 15, 9, 0, tzinfo=NYC),
        )
        # 2 a.m. UTC on the 16th is still the 15th in New York.
        late_night = make_event(
            "night",
            "Night Walk",
            host=self.other,
            start_time=datetime(2099, 1, 16, 2, 0, tzinfo=dt_timezone.utc),
        )
        self.register(later, party_size=3)
        self.register(late_night)

        response, days = self.get_plans()

        self.assertEqual(
            [(day, [plan["event"] for plan in plans]) for day, plans in days],
            [
                (date(2099, 1, 15), [hosted, late_night]),
                (date(2099, 1, 16), [later]),
            ],
        )
        self.assertTrue(days[0][1][0]["hosting"])
        self.assertIsNone(days[0][1][0]["party_size"])
        self.assertFalse(days[1][1][0]["hosting"])
        self.assertEqual(days[1][1][0]["party_size"], 3)
        self.assertContains(response, "Thursday, January 15")
        self.assertContains(response, "Friday, January 16")
        self.assertContains(response, '<span class="plan-badge hosting">Hosting</span>')
        self.assertContains(response, "Going &middot; Party of 3")

    def test_hosting_your_own_registered_event_shows_once(self):
        """A host registered for their own event gets one plan with both badges."""
        event = make_event(
            "own",
            "Own Picnic",
            host=self.user,
            start_time=datetime(2099, 2, 1, 12, 0, tzinfo=NYC),
        )
        self.register(event, party_size=2)

        _, days = self.get_plans()

        self.assertEqual(len(days), 1)
        (plan,) = days[0][1]
        self.assertTrue(plan["hosting"])
        self.assertEqual(plan["party_size"], 2)

    def test_cancelled_finished_and_past_events_are_left_out(self):
        """Only open, uncancelled events count as plans."""
        start = datetime(2099, 3, 1, 10, 0, tzinfo=NYC)
        make_event(
            "cancelled",
            "Cancelled Hike",
            host=self.user,
            start_time=start,
            status=Event.Status.CANCELLED,
        )
        self.register(
            make_event(
                "feed", "CANCELLED: Feed Event", host=self.other, start_time=start
            )
        )
        self.register(
            make_event(
                "done",
                "Done Event",
                host=self.other,
                start_time=start,
                status=Event.Status.DONE,
            )
        )
        self.register(
            make_event(
                "past",
                "Past Event",
                host=self.other,
                start_time=datetime(2000, 1, 1, 10, 0, tzinfo=NYC),
                end_time=datetime(2000, 1, 1, 11, 0, tzinfo=NYC),
            )
        )

        response, days = self.get_plans()

        self.assertEqual(days, [])
        self.assertContains(response, "You have no upcoming plans yet.")
        self.assertContains(response, f'href="{reverse("search")}?when=weekend"')

    def test_undated_events_come_last(self):
        """Events without a date are listed after dated ones, under TBA."""
        undated = make_event("undated", "Pop-up Market", host=self.user)
        dated = make_event(
            "dated",
            "Bike Tour",
            host=self.user,
            start_time=datetime(2099, 4, 1, 10, 0, tzinfo=NYC),
        )

        response, days = self.get_plans()

        self.assertEqual(
            [(day, [plan["event"] for plan in plans]) for day, plans in days],
            [(date(2099, 4, 1), [dated]), (None, [undated])],
        )
        self.assertContains(response, "Date to be announced")
        self.assertContains(response, "Time to be announced")

    def test_my_plans_is_the_first_and_selected_tab(self):
        """My plans opens first; the other tabs start hidden."""
        response, _ = self.get_plans()
        html = response.content.decode()

        self.assertLess(html.index('id="plans-tab"'), html.index('id="hosted-tab"'))
        self.assertInHTML(
            '<button type="button" class="my-events-tab" id="plans-tab" role="tab"'
            ' aria-selected="true" aria-controls="plans-panel" tabindex="0">'
            "My plans</button>",
            html,
        )
        self.assertRegex(html, r'id="hosted-panel"[^>]*\s+hidden')
        self.assertRegex(html, r'id="attended-panel"[^>]*\s+hidden')

    def test_home_page_no_longer_shows_personal_plans(self):
        """The placeholder Personal plans section is gone from the home page."""
        response = self.client.get(reverse("landing"))

        self.assertNotContains(response, "Personal plans")
        self.assertNotContains(response, "Plan placeholder")
