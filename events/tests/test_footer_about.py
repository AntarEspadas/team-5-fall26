"""Tests for the shared site footer and the About us page (#147)."""

from datetime import datetime
from unittest import mock
from zoneinfo import ZoneInfo

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.urls import reverse

from events.models import Event
from events.views import TEAM_MEMBERS

NYC = ZoneInfo("America/New_York")

FOOTER = '<footer class="site-footer">'


class SiteFooterTests(TestCase):
    """Every page shows the same footer, linking only to pages that exist."""

    @classmethod
    def setUpTestData(cls):
        cls.user = get_user_model().objects.create_user(
            username="fern", email="fern@example.com", password="pass12345"
        )
        cls.event = Event.objects.create(
            guid="footer-event",
            title="Footer Test Walk",
            content_hash="",
            start_time=datetime(2099, 5, 1, 10, 0, tzinfo=NYC),
        )

    def test_footer_is_on_public_pages(self):
        """Visitors see the footer on home, search, event, account and About pages."""
        urls = [
            reverse("landing"),
            reverse("search"),
            reverse("event_detail", args=[self.event.pk]),
            reverse("login"),
            reverse("register"),
            reverse("about"),
        ]
        for url in urls:
            with self.subTest(url=url):
                response = self.client.get(url)
                self.assertEqual(response.status_code, 200)
                self.assertContains(response, FOOTER, count=1)
                self.assertContains(response, "events/footer.css")

    def test_footer_is_on_signed_in_pages(self):
        """Members see the footer on their own pages too."""
        self.client.force_login(self.user)
        urls = [
            reverse("my_events"),
            reverse("inbox"),
            reverse("notifications"),
            reverse("account_settings"),
            reverse("archived_events"),
            reverse("event_create"),
            reverse("public_profile", kwargs={"username": self.user.username}),
        ]
        for url in urls:
            with self.subTest(url=url):
                response = self.client.get(url)
                self.assertEqual(response.status_code, 200)
                self.assertContains(response, FOOTER, count=1)

    def test_signed_out_footer_offers_log_in_and_sign_up(self):
        """Visitors get Explore and About links plus Log in and Sign up."""
        response = self.client.get(reverse("landing"))

        for href in (
            reverse("landing"),
            reverse("search"),
            reverse("search") + "?when=weekend",
            reverse("archived_events"),
            reverse("login"),
            reverse("register"),
            reverse("about"),
        ):
            with self.subTest(href=href):
                self.assertContains(response, f'<a href="{href}">')
        self.assertNotContains(response, f'<a href="{reverse("my_events")}">')
        self.assertNotContains(response, f'<a href="{reverse("account_settings")}">')

    def test_signed_in_footer_lists_account_pages(self):
        """Members get links to their account pages instead of Log in."""
        self.client.force_login(self.user)
        response = self.client.get(reverse("landing"))

        for name in (
            "my_events",
            "event_create",
            "inbox",
            "notifications",
            "account_settings",
        ):
            with self.subTest(name=name):
                self.assertContains(response, f'<a href="{reverse(name)}">')
        self.assertNotContains(response, f'<a href="{reverse("login")}">Log in</a>')

    def test_banned_members_do_not_get_the_post_link(self):
        """The footer hides Post an event from banned members, like the header."""
        self.client.force_login(self.user)
        with mock.patch(
            "accounts.context_processors.get_active_ban", return_value=object()
        ):
            response = self.client.get(reverse("my_events"))

        self.assertNotContains(response, f'<a href="{reverse("event_create")}">')
        self.assertContains(response, f'<a href="{reverse("my_events")}">')

    def test_footer_has_data_credit_and_no_missing_pages(self):
        """The footer credits the data and skips pages the site doesn't have."""
        response = self.client.get(reverse("landing"))

        self.assertContains(response, "Event data from NYC Parks via NYC Open Data")
        self.assertContains(response, "Not an official NYC Parks website")
        for missing in ("Privacy", "Terms of use", "FAQ", "Contact us"):
            with self.subTest(missing=missing):
                self.assertNotContains(response, missing)


class AboutPageTests(TestCase):
    """The About us page explains the site to anyone, signed in or not."""

    def test_about_page_is_public(self):
        """Visitors can open the About us page without logging in."""
        response = self.client.get(reverse("about"))

        self.assertEqual(response.status_code, 200)
        self.assertTemplateUsed(response, "events/about.html")
        self.assertContains(response, "<title>About us | NYC Parks Events</title>")

    def test_about_page_explains_the_site_and_data(self):
        """The page covers what you can do and where events come from."""
        response = self.client.get(reverse("about"))

        for text in (
            "What you can do here",
            "Find events",
            "Host your own",
            "Where the events come from",
            "https://opendata.cityofnewyork.us/",
            "we sync them every hour",
            "This is not an official NYC Parks website.",
        ):
            with self.subTest(text=text):
                self.assertContains(response, text)

    def test_about_page_lists_the_team(self):
        """Every team member is listed, with the course project credit."""
        response = self.client.get(reverse("about"))

        self.assertEqual(list(response.context["team"]), list(TEAM_MEMBERS))
        self.assertContains(response, "Team 5 at New York University")
        for member in TEAM_MEMBERS:
            with self.subTest(member=member):
                self.assertContains(response, f"<li>{member}</li>")

    def test_about_page_links_to_search(self):
        """The page ends with a Browse events button."""
        response = self.client.get(reverse("about"))

        self.assertContains(
            response,
            f'<a class="btn-primary" href="{reverse("search")}">Browse events</a>',
        )
