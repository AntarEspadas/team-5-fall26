"""Tests for the landing page and its event list."""

from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from events.models import Event, Registration
from events.views import (
    ARCHIVED_EVENT_PAGE_SIZE,
    UPCOMING_EVENT_LIMIT,
)

NYC = ZoneInfo("America/New_York")


def make_event(guid, title, **kwargs):
    """Create an event row with sensible defaults for view tests."""
    kwargs.setdefault("content_hash", "v" * 64)
    # Feed events without a registration link are not listed (#158).
    kwargs.setdefault("registration_url", "https://example.com/register")
    return Event.objects.create(guid=guid, title=title, **kwargs)


class HomeWithoutWeekendSectionTests(TestCase):
    """The home page has no separate This weekend list (#157)."""

    @classmethod
    def setUpTestData(cls):
        cls.user = get_user_model().objects.create_user(
            username="bob", email="bob@example.com", password="pass12345"
        )
        # Starts in a few days, so it falls in or near any "this weekend".
        soon = timezone.now() + timedelta(days=2)
        cls.event = make_event(
            "soon",
            "Soon Kayaking",
            start_time=soon,
            end_time=soon + timedelta(hours=2),
        )

    def assert_no_weekend_section(self, response):
        """The weekend list and its context are gone from the page."""
        self.assertNotContains(response, "weekend-section")
        self.assertNotContains(response, "See all weekend events")
        self.assertNotContains(response, "Nothing is scheduled for this weekend yet.")
        self.assertNotIn("weekend_events", response.context)

    def assert_in_order(self, response, *markers):
        """Each marker appears on the page, in the given order."""
        html = response.content.decode()
        positions = [html.index(marker) for marker in markers]
        self.assertEqual(positions, sorted(positions))

    def test_signed_out_home_has_no_weekend_list(self):
        """Visitors see the search bar, then the sign-up card, and no event list."""
        response = self.client.get(reverse("landing"))

        self.assertEqual(response.status_code, 200)
        self.assert_no_weekend_section(response)
        self.assertNotContains(response, "Soon Kayaking")
        self.assert_in_order(
            response,
            'class="headline"',
            'action="/search"',
            'class="join-card"',
            'class="site-footer"',
        )

    def test_signed_in_home_keeps_its_sections_in_order(self):
        """Members keep their sections, in the same order, without the list."""
        self.client.force_login(self.user)

        response = self.client.get(reverse("landing"))

        self.assert_no_weekend_section(response)
        self.assert_in_order(
            response,
            'class="headline"',
            'action="/search"',
            'id="events-heading"',
            'id="archived-heading"',
            'class="site-footer"',
        )
        self.assertNotContains(response, 'class="join-card"')

    def test_this_weekend_filter_button_stays(self):
        """The quick filter by the search bar still offers This weekend."""
        for signed_in in (False, True):
            with self.subTest(signed_in=signed_in):
                if signed_in:
                    self.client.force_login(self.user)
                response = self.client.get(reverse("landing"))

                self.assertContains(response, 'name="when" value="weekend"')
                self.assertContains(
                    response, f'href="{reverse("search")}?when=weekend"'
                )


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
        # A hosted event, so its card offers registration on this site.
        make_event(
            Event.HOSTED_GUID_PREFIX + "b2",
            "Beta Birdwatching",
            start_time=datetime(2099, 1, 16, 10, 0, tzinfo=NYC),
            end_time=datetime(2099, 1, 16, 12, 0, tzinfo=NYC),
            park_names="Central Park",
            registration_url="",
            host=get_user_model().objects.create_user(
                username="beta_host", email="beta_host@example.com"
            ),
        )
        self.past_event = make_event(
            "c3",
            "Hidden Past Event",
            start_time=datetime(2099, 1, 14, 9, 0, tzinfo=NYC),
            status=Event.Status.DONE,
        )

    def test_anonymous_visitors_see_the_sign_up_prompt(self):
        """Logged-out visitors get a sign-up prompt but no personal sections."""
        response = self.client.get(reverse("landing"))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Log in or sign up")
        self.assertContains(response, f'href="{reverse("register")}"')
        self.assertContains(response, f'href="{reverse("login")}"')
        self.assertNotContains(response, "Alpha Yoga")
        # Match the section headings: the site footer links to Archived events.
        self.assertNotContains(response, 'id="events-heading"')
        self.assertNotContains(response, "Personal plans")
        self.assertNotContains(response, 'id="archived-heading"')
        self.assertEqual(list(response.context["events"]), [])
        self.assertEqual(list(response.context["archived_events"]), [])

    def test_signed_in_visitors_see_upcoming_events_in_order(self):
        """Upcoming events are listed soonest first;
        done events appear in the archive."""
        make_event(
            "d4",
            "Undated Event",
            start_time=None,
            end_time=datetime(2099, 1, 20, 9, 0, tzinfo=NYC),
        )
        self.client.login(username="alice", password="pass12345")

        response = self.client.get(reverse("landing"))

        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            [event.title for event in response.context["events"]],
            ["Alpha Yoga", "Beta Birdwatching", "Undated Event"],
        )
        self.assertNotIn(self.past_event, response.context["events"])
        self.assertIn(self.past_event, response.context["archived_events"])
        self.assertContains(response, "Hidden Past Event")

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
        beta_event = Event.objects.get(guid=Event.HOSTED_GUID_PREFIX + "b2")
        register_url = reverse("event_detail", args=[beta_event.pk])
        self.assertContains(
            response, f'<a class="register" href="{register_url}#register">'
        )
        self.assertContains(response, "Register")
        self.assertContains(response, "Stretch and breathe in the park.")

    def test_images_use_the_stored_url_or_a_category_illustration(self):
        """Events with an image link to it; the rest show an illustration."""
        self.client.login(username="alice", password="pass12345")

        response = self.client.get(reverse("landing"))
        fitness = "/static/events/categories/fitness.webp"
        park = "/static/events/categories/park.webp"

        self.assertContains(
            response,
            'src="https://example.org/img/yoga.png"',
        )
        self.assertContains(response, f'data-placeholder="{fitness}"')
        self.assertContains(response, f'src="{park}"')
        self.assertContains(response, f'data-placeholder="{park}"')

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

    def test_archived_events_are_shown_on_landing_page(self):
        """The landing page displays completed events in its archive section."""
        self.client.login(username="alice", password="pass12345")

        response = self.client.get(reverse("landing"))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Archived events")
        self.assertContains(response, "Hidden Past Event")
        self.assertIn(self.past_event, response.context["archived_events"])


class ArchivedEventsViewTests(TestCase):
    """Tests for the archived events page."""

    def setUp(self):
        self.user = get_user_model().objects.create_user(
            username="archive_user",
            email="archive@example.com",
            password="pass12345",
        )

        self.old_event = make_event(
            "archive-1",
            "Older Completed Event",
            start_time=datetime(2099, 1, 10, 9, 0, tzinfo=NYC),
            status=Event.Status.DONE,
        )

        self.new_event = make_event(
            "archive-2",
            "Newer Completed Event",
            start_time=datetime(2099, 1, 20, 9, 0, tzinfo=NYC),
            status=Event.Status.DONE,
        )

        self.upcoming_event = make_event(
            "archive-3",
            "Upcoming Event",
            start_time=datetime(2099, 2, 10, 9, 0, tzinfo=NYC),
            status=Event.Status.UPCOMING,
        )

        self.cancelled_event = make_event(
            "archive-4",
            "Cancelled Event",
            start_time=datetime(2099, 1, 15, 9, 0, tzinfo=NYC),
            status=Event.Status.CANCELLED,
        )

    def test_anonymous_users_are_redirected_to_login(self):
        """Anonymous visitors must log in to access the archive."""
        response = self.client.get(reverse("archived_events"))

        self.assertEqual(response.status_code, 302)
        self.assertIn("/accounts/login/", response.url)

    def test_archived_page_shows_only_completed_events(self):
        """The archive contains completed events, not upcoming or cancelled ones."""
        self.client.login(username="archive_user", password="pass12345")

        response = self.client.get(reverse("archived_events"))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Older Completed Event")
        self.assertContains(response, "Newer Completed Event")
        self.assertNotContains(response, "Upcoming Event")
        self.assertNotContains(response, "Cancelled Event")

    def test_archived_events_are_sorted_newest_first(self):
        """More recently completed events appear before older events."""
        self.client.login(username="archive_user", password="pass12345")

        response = self.client.get(reverse("archived_events"))

        self.assertEqual(
            [event.title for event in response.context["events"]],
            ["Newer Completed Event", "Older Completed Event"],
        )

    def test_archived_events_can_be_searched_by_keyword(self):
        """Keyword search returns matching completed events only."""
        matching_event = make_event(
            "archive-yoga",
            "Community Yoga",
            status=Event.Status.DONE,
        )
        make_event(
            "archive-upcoming-yoga",
            "Upcoming Yoga",
            status=Event.Status.UPCOMING,
        )
        self.client.login(username="archive_user", password="pass12345")
        response = self.client.get(
            reverse("archived_events"),
            {"q": "Yoga"},
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            list(response.context["events"]),
            [matching_event],
        )
        self.assertContains(response, "Community Yoga")
        self.assertNotContains(response, "Upcoming Yoga")
        self.assertEqual(response.context["query"], "Yoga")

    def test_archived_events_can_be_filtered_by_date(self):
        """Date filters narrow the completed events."""
        self.client.login(username="archive_user", password="pass12345")

        response = self.client.get(
            reverse("archived_events"),
            {
                "date_from": "2099-01-19",
                "date_to": "2099-01-21",
            },
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            list(response.context["events"]),
            [self.new_event],
        )
        self.assertTrue(response.context["has_filters"])

    def test_archived_events_can_be_filtered_by_category(self):
        """Category filters return matching completed events."""
        yoga_event = make_event(
            "archive-category-yoga",
            "Yoga Session",
            categories="Yoga & Pilates",
            status=Event.Status.DONE,
        )

        make_event(
            "archive-category-art",
            "Art Workshop",
            categories="Arts & Crafts",
            status=Event.Status.DONE,
        )

        self.client.login(username="archive_user", password="pass12345")

        response = self.client.get(
            reverse("archived_events"),
            {"category": "yoga"},
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            list(response.context["events"]),
            [yoga_event],
        )

    def test_archived_events_can_be_filtered_by_borough(self):
        """Borough filters return matching completed events."""
        brooklyn_event = make_event(
            "archive-brooklyn",
            "Brooklyn Park Event",
            borough="brooklyn",
            status=Event.Status.DONE,
        )

        make_event(
            "archive-queens",
            "Queens Park Event",
            borough="queens",
            status=Event.Status.DONE,
        )

        self.client.login(username="archive_user", password="pass12345")

        response = self.client.get(
            reverse("archived_events"),
            {"borough": "brooklyn"},
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            list(response.context["events"]),
            [brooklyn_event],
        )

    def test_archived_events_can_combine_search_and_filters(self):
        """Keyword search and category filters work together."""
        matching_event = make_event(
            "archive-combined-yoga",
            "Community Yoga",
            categories="Yoga & Pilates",
            status=Event.Status.DONE,
        )

        make_event(
            "archive-combined-art",
            "Community Art",
            categories="Arts & Crafts",
            status=Event.Status.DONE,
        )

        self.client.login(username="archive_user", password="pass12345")

        response = self.client.get(
            reverse("archived_events"),
            {
                "q": "Community",
                "category": "yoga",
            },
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            list(response.context["events"]),
            [matching_event],
        )
        self.assertEqual(response.context["query"], "Community")

    def test_archived_events_pagination_preserves_search_parameters(self):
        """Pagination links retain the active search and filters."""
        for index in range(ARCHIVED_EVENT_PAGE_SIZE + 1):
            make_event(
                f"archive-page-{index}",
                f"Community Event {index}",
                categories="Yoga & Pilates",
                status=Event.Status.DONE,
            )

        self.client.login(username="archive_user", password="pass12345")

        response = self.client.get(
            reverse("archived_events"),
            {
                "q": "Community",
                "category": "yoga",
            },
        )

        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.context["is_paginated"])
        self.assertEqual(
            response.context["paginator"].count,
            ARCHIVED_EVENT_PAGE_SIZE + 1,
        )

        self.assertContains(
            response,
            "q=Community&amp;category=yoga&amp;page=2",
        )

    def test_archived_events_show_empty_state_for_no_matches(self):
        """The archive explains when a search has no matching events."""
        self.client.login(username="archive_user", password="pass12345")

        response = self.client.get(
            reverse("archived_events"),
            {"q": "NonexistentActivity"},
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(len(response.context["events"]), 0)
        self.assertContains(response, "No archived events match your search.")

    def test_archived_events_handle_invalid_date_filter(self):
        """Invalid filter input keeps the form errors visible."""
        self.client.login(username="archive_user", password="pass12345")

        response = self.client.get(
            reverse("archived_events"),
            {"date_from": "not-a-date"},
        )

        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.context["filter_form"].errors)


class RegistrationViewTests(TestCase):
    """Tests for registering for and cancelling event registrations."""

    def setUp(self):
        User = get_user_model()
        self.user = User.objects.create_user(
            username="registration_user",
            email="registration@example.com",
            password="pass12345",
        )
        self.other_user = User.objects.create_user(
            username="event_host",
            email="host@example.com",
            password="pass12345",
        )

        self.event = make_event(
            "registration-1",
            "Community Yoga",
            host=self.other_user,
            start_time=datetime(2099, 3, 15, 9, 0, tzinfo=NYC),
            end_time=datetime(2099, 3, 15, 10, 0, tzinfo=NYC),
        )

        self.register_url = reverse("event_register", args=[self.event.pk])
        self.cancel_url = reverse("event_cancel_registration", args=[self.event.pk])
        self.detail_url = reverse("event_detail", args=[self.event.pk])

        self.client.login(
            username="registration_user",
            password="pass12345",
        )

    def test_user_can_register_for_an_upcoming_event(self):
        response = self.client.post(self.register_url)

        self.assertRedirects(response, self.detail_url)
        self.assertTrue(
            Registration.objects.filter(
                event=self.event,
                user=self.user,
            ).exists()
        )
        self.assertEqual(Registration.objects.count(), 1)

    def test_duplicate_registration_does_not_create_another_record(self):
        Registration.objects.create(
            event=self.event,
            user=self.user,
        )

        response = self.client.post(self.register_url)

        self.assertRedirects(response, self.detail_url)
        self.assertEqual(Registration.objects.count(), 1)

    def test_event_without_host_accepts_registration(self):
        self.event.host = None
        self.event.save(update_fields=["host"])

        response = self.client.post(self.register_url)

        self.assertRedirects(response, self.detail_url)
        self.assertTrue(
            Registration.objects.filter(
                event=self.event,
                user=self.user,
            ).exists()
        )
        self.assertEqual(Registration.objects.count(), 1)

    def test_host_cannot_register_for_own_event(self):
        self.client.logout()
        self.client.login(
            username="event_host",
            password="pass12345",
        )

        response = self.client.post(self.register_url)

        self.assertRedirects(response, self.detail_url)
        self.assertFalse(Registration.objects.exists())

    def test_closed_event_does_not_accept_registration(self):
        self.event.status = Event.Status.DONE
        self.event.save(update_fields=["status"])

        response = self.client.post(self.register_url)

        self.assertRedirects(response, self.detail_url)
        self.assertFalse(Registration.objects.exists())

    def test_cancelled_event_does_not_accept_registration(self):
        self.event.status = Event.Status.CANCELLED
        self.event.save(update_fields=["status"])

        response = self.client.post(self.register_url)

        self.assertRedirects(response, self.detail_url)
        self.assertFalse(Registration.objects.exists())

    def test_user_can_cancel_existing_registration(self):
        Registration.objects.create(
            event=self.event,
            user=self.user,
        )

        response = self.client.post(self.cancel_url)

        self.assertRedirects(response, self.detail_url)
        self.assertFalse(Registration.objects.exists())

    def test_cancelling_without_registration_does_not_create_record(self):
        response = self.client.post(self.cancel_url)

        self.assertRedirects(response, self.detail_url)
        self.assertFalse(Registration.objects.exists())

    def test_registration_requires_login(self):
        self.client.logout()

        response = self.client.post(self.register_url)

        self.assertEqual(response.status_code, 302)
        self.assertIn("/accounts/login/", response.url)
        self.assertFalse(Registration.objects.exists())

    def test_cancelling_registration_requires_login(self):
        self.client.logout()

        response = self.client.post(self.cancel_url)

        self.assertEqual(response.status_code, 302)
        self.assertIn("/accounts/login/", response.url)

    def test_registration_requires_post(self):
        response = self.client.get(self.register_url)

        self.assertEqual(response.status_code, 405)
        self.assertFalse(Registration.objects.exists())

    def test_cancelling_registration_requires_post(self):
        response = self.client.get(self.cancel_url)

        self.assertEqual(response.status_code, 405)
        self.assertFalse(Registration.objects.exists())


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
        self.assertContains(response, 'src="/static/events/categories/park.webp"')
        self.assertContains(response, 'alt=""')
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
