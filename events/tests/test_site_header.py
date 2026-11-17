"""Tests for the shared site header shown on every page."""

from datetime import datetime
from unittest import mock
from zoneinfo import ZoneInfo

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.urls import reverse

from events.models import (
    ContactRequest,
    Conversation,
    DirectMessage,
    Event,
    Notification,
)

NYC = ZoneInfo("America/New_York")


class SiteHeaderTests(TestCase):
    """Every signed-in page renders the same header with unread badges."""

    @classmethod
    def setUpTestData(cls):
        """Create a host, a friend, an event, and unread mail for the host."""
        User = get_user_model()
        cls.user = User.objects.create_user(
            username="maya", email="maya@example.com", password="pass12345"
        )
        cls.friend = User.objects.create_user(
            username="alex", email="alex@example.com", password="pass12345"
        )
        cls.event = Event.objects.create(
            guid="header-event",
            title="Header Picnic",
            content_hash="h" * 64,
            start_time=datetime(2099, 1, 15, 9, 0, tzinfo=NYC),
            host=cls.user,
        )
        ContactRequest.objects.create(
            event=cls.event,
            sender=cls.friend,
            recipient=cls.user,
            status=ContactRequest.Status.ACCEPTED,
        )
        cls.thread, _ = Conversation.get_or_create_for_users(cls.user, cls.friend)
        DirectMessage.objects.create(
            conversation=cls.thread, sender=cls.friend, body="Hello there"
        )
        Notification.objects.create(
            recipient=cls.user, event=cls.event, subject="Update", body="Moved"
        )

    def setUp(self):
        """Sign the host in."""
        self.client.force_login(self.user)

    def page_urls(self):
        """Return every page a signed-in member can open."""
        pk = self.event.pk
        return [
            reverse("landing"),
            reverse("search") + "?q=picnic",
            reverse("event_detail", args=[pk]),
            reverse("event_create"),
            reverse("event_edit", args=[pk]),
            reverse("event_manage", args=[pk]),
            reverse("event_cancel", args=[pk]),
            reverse("event_report", args=[pk]),
            reverse("my_events"),
            reverse("archived_events"),
            reverse("inbox"),
            reverse("conversation", args=[self.thread.pk]),
            reverse("notifications"),
            reverse("account_settings"),
            reverse("public_profile", args=[self.friend.username]),
            reverse("home"),
        ]

    def test_every_page_has_one_shared_header(self):
        """Each page shows the header once, with all links and the menu."""
        for url in self.page_urls():
            with self.subTest(url=url):
                response = self.client.get(url)
                self.assertEqual(response.status_code, 200)
                html = response.content.decode()
                self.assertEqual(html.count('class="topbar site-header"'), 1)
                for label in ("Post an event", "My events", "Messages"):
                    self.assertIn(label, html)
                self.assertIn('class="account-menu"', html)
                self.assertContains(response, reverse("logout"))

    def test_every_page_shows_unread_badges(self):
        """Unread message and notification counts appear on every page."""
        for url in self.page_urls():
            if "conversations" in url or "notifications" in url:
                continue  # Opening these marks the items as read.
            with self.subTest(url=url):
                response = self.client.get(url)
                self.assertContains(response, "message-count-badge")
                self.assertContains(response, "notification-count-badge")
                self.assertContains(response, "Messages, 1 new")

    def test_current_page_is_highlighted(self):
        """The link for the page being viewed is marked as current."""
        response = self.client.get(reverse("my_events"))
        self.assertContains(
            response,
            f'class="nav-link active" href="{reverse("my_events")}" '
            'aria-current="page"',
        )

    def test_my_events_intro_has_no_post_link(self):
        """Posting is offered in the header only, not in the My Events intro."""
        response = self.client.get(reverse("my_events"))
        self.assertNotContains(response, "Post a new event")
        self.assertContains(response, "Post an event")

    def test_banned_member_cannot_see_post_link(self):
        """The Post an event link is hidden from banned members."""
        with mock.patch(
            "accounts.context_processors.get_active_ban", return_value=object()
        ):
            response = self.client.get(reverse("my_events"))
        self.assertNotContains(response, f'href="{reverse("event_create")}"')

    def test_signed_out_header_offers_log_in_and_sign_up(self):
        """Visitors get Log in and Sign up instead of the member links."""
        self.client.logout()
        response = self.client.get(reverse("landing"))
        self.assertContains(response, 'class="topbar site-header"')
        self.assertContains(response, reverse("login"))
        self.assertContains(response, reverse("register"))
        self.assertNotContains(response, "account-menu")

    def test_auth_pages_hide_their_own_link(self):
        """The log-in page does not link to itself, nor sign-up to itself."""
        self.client.logout()
        login = self.client.get(reverse("login")).content.decode()
        self.assertNotIn(f'class="nav-link" href="{reverse("login")}"', login)
        register = self.client.get(reverse("register")).content.decode()
        self.assertNotIn(f'class="btn-ghost" href="{reverse("register")}"', register)


class MobileNavigationTests(SiteHeaderTests):
    """Phones get a bottom tab bar that replaces the header links."""

    def test_every_page_has_the_tab_bar(self):
        """Each signed-in page renders the tab bar once."""
        for url in self.page_urls():
            with self.subTest(url=url):
                html = self.client.get(url).content.decode()
                self.assertEqual(html.count('<nav class="tabbar"'), 1)
                for label in ("Home", "Post", "My events", "Messages", "Alerts"):
                    self.assertIn(f"<span>{label}</span>", html)

    def test_tab_bar_shows_unread_counts(self):
        """Message and notification tabs carry unread badges."""
        response = self.client.get(reverse("my_events"))
        self.assertContains(response, '<span class="tab-badge">1</span>', count=2)

    def test_tab_bar_marks_current_page(self):
        """The tab for the page being viewed is marked as current."""
        response = self.client.get(reverse("inbox"))
        self.assertContains(response, f'class="tab active" href="{reverse("inbox")}"')

    def test_signed_out_pages_have_no_tab_bar(self):
        """Visitors keep the compact header without a tab bar."""
        self.client.logout()
        response = self.client.get(reverse("landing"))
        self.assertNotContains(response, 'class="tabbar"')

    def test_header_stylesheet_hides_links_on_phones(self):
        """The stylesheet swaps header links for the tab bar under 720px."""
        from django.contrib.staticfiles import finders

        with open(finders.find("events/header.css"), encoding="utf-8") as css:
            text = css.read()
        self.assertIn("@media (max-width: 720px)", text)
        self.assertIn(".tabbar {\n        position: fixed;", text)
