"""Tests for the two-column event page with a sticky action panel."""

import re
from datetime import datetime
from zoneinfo import ZoneInfo

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.urls import reverse

from events.models import Event

NYC = ZoneInfo("America/New_York")


def panel_html(response):
    """Return the markup of the event page's action panel."""
    match = re.search(
        r'<aside class="detail-panel".*?</aside>',
        response.content.decode(),
        flags=re.DOTALL,
    )
    return match.group(0) if match else ""


class EventPageLayoutTests(TestCase):
    """The event page keeps its actions together in one side panel."""

    @classmethod
    def setUpTestData(cls):
        """Create a member, a host, and two upcoming events."""
        User = get_user_model()
        cls.member = User.objects.create_user(
            username="member", email="member@example.com", password="pass12345"
        )
        cls.host = User.objects.create_user(
            username="host", email="host@example.com", password="pass12345"
        )
        common = {
            "content_hash": "l" * 64,
            "start_time": datetime(2099, 3, 4, 10, 0, tzinfo=NYC),
            "end_time": datetime(2099, 3, 4, 12, 0, tzinfo=NYC),
            "location": "Great Lawn",
        }
        cls.event = Event.objects.create(
            guid="layout-photo",
            title="Photo walk",
            image_url="https://example.com/walk.jpg",
            event_url="https://example.com/walk",
            host=cls.host,
            **common,
        )
        cls.plain = Event.objects.create(
            guid="layout-plain", title="Plain walk", **common
        )

    def test_panel_holds_register_calendar_and_report(self):
        """Register, party size, Add to calendar and Report sit in the panel."""
        self.client.force_login(self.member)
        response = self.client.get(reverse("event_detail", args=[self.event.pk]))
        panel = panel_html(response)
        self.assertIn('class="register-toggle"', panel)
        self.assertIn('name="party_size"', panel)
        self.assertIn('class="calendar-menu"', panel)
        self.assertIn(reverse("event_report", args=[self.event.pk]), panel)
        self.assertIn("Wed, Mar 4", panel)
        self.assertIn('class="detail-layout"', response.content.decode())

    def test_panel_follows_the_details_in_page_order(self):
        """On phones the panel comes right after the event details."""
        response = self.client.get(reverse("event_detail", args=[self.event.pk]))
        html = response.content.decode()
        self.assertLess(
            html.index("</main>"), html.index('<aside class="detail-panel"')
        )
        self.assertLess(
            html.index('<aside class="detail-panel"'),
            html.index("Connect with Other Attendees"),
        )

    def test_visitors_see_log_in_to_register_in_panel(self):
        """Signed-out visitors get the log-in prompt in the panel."""
        response = self.client.get(reverse("event_detail", args=[self.event.pk]))
        self.assertIn("Log in to Register", panel_html(response))

    def test_host_actions_move_to_panel(self):
        """Hosts manage, edit and cancel from the panel."""
        self.client.force_login(self.host)
        response = self.client.get(reverse("event_detail", args=[self.event.pk]))
        panel = panel_html(response)
        for name in ("event_manage", "event_edit", "event_cancel"):
            self.assertIn(reverse(name, args=[self.event.pk]), panel)

    def test_banner_shrinks_without_a_photo(self):
        """Events with no photo get a shorter illustration banner."""
        plain = self.client.get(reverse("event_detail", args=[self.plain.pk]))
        self.assertContains(plain, 'class="detail-image is-illustration"')
        photo = self.client.get(reverse("event_detail", args=[self.event.pk]))
        self.assertContains(photo, 'class="detail-image"')
        self.assertNotContains(photo, 'class="detail-image is-illustration"')

    def test_registering_from_panel_keeps_layout(self):
        """Registering through the panel shows the registered state there."""
        self.client.force_login(self.member)
        url = reverse("event_detail", args=[self.plain.pk])
        response = self.client.post(
            reverse("event_register", args=[self.plain.pk]),
            {"party_size": 3},
            follow=True,
        )
        self.assertEqual(response.redirect_chain[-1][0], url)
        panel = panel_html(response)
        self.assertIn("You are registered for this event with a party of 3", panel)
        self.assertIn("Cancel Registration", panel)
