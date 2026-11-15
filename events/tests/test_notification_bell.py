"""Tests for the notification bell in the site header."""

from datetime import timedelta

from django.contrib.auth import get_user_model
from django.contrib.staticfiles import finders
from django.test import SimpleTestCase, TestCase
from django.urls import reverse
from django.utils import timezone

from accounts.ban_services import apply_ban
from events.models import ContactRequest, Event, Notification


class NotificationBellTests(TestCase):
    """The bell shows the five latest notifications and reads them."""

    @classmethod
    def setUpTestData(cls):
        """Create a member with seven notifications and a contact request."""
        User = get_user_model()
        cls.member = User.objects.create_user(
            username="member", email="member@example.com", password="pass12345"
        )
        cls.peer = User.objects.create_user(
            username="peer", email="peer@example.com", password="pass12345"
        )
        cls.event = Event.objects.create(
            guid="bell", title="Bell walk", content_hash="b" * 64
        )
        now = timezone.now()
        for number in range(7):
            note = Notification.objects.create(
                recipient=cls.member,
                event=cls.event,
                subject=f"Update {number}",
                body="Changed",
            )
            Notification.objects.filter(pk=note.pk).update(
                created_at=now - timedelta(hours=7 - number)
            )

    def setUp(self):
        """Sign the member in."""
        self.client.force_login(self.member)
        self.url = reverse("notification_bell")

    def test_header_has_bell_with_badge_and_see_all(self):
        """Every page's header has the bell, its unread badge and See all."""
        response = self.client.get(reverse("my_events"))
        self.assertContains(response, 'class="bell-menu"')
        self.assertContains(response, f'data-bell-url="{self.url}"')
        self.assertContains(response, 'aria-label="Notifications, 7 new"')
        self.assertContains(
            response, '<span class="bell-badge notification-count-badge">7</span>'
        )
        self.assertContains(response, "See all notifications")
        self.assertContains(response, "events/bell.js")

    def test_bell_is_marked_on_the_notifications_page(self):
        """The bell is highlighted on the notifications page."""
        response = self.client.get(reverse("notifications"))
        self.assertContains(response, 'class="bell-menu active"')

    def test_opening_returns_five_latest_and_marks_them_read(self):
        """Opening the bell lists the 5 newest and marks only those read."""
        response = self.client.post(self.url)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response["Cache-Control"], "no-store")
        data = response.json()
        html = data["html"]
        self.assertEqual(html.count('class="bell-item'), 5)
        self.assertLess(html.index("Update 6"), html.index("Update 2"))
        self.assertNotIn("Update 1", html)
        self.assertEqual(data["unread_count"], 2)
        self.assertEqual(
            set(
                self.member.notifications.filter(read_at__isnull=True).values_list(
                    "subject", flat=True
                )
            ),
            {"Update 0", "Update 1"},
        )

    def test_opening_again_shows_nothing_new(self):
        """A second opening shows the same items without the new marker."""
        self.client.post(self.url)
        html = self.client.post(self.url).json()["html"]
        self.assertNotIn("is-new", html)

    def test_pending_contact_requests_stay_new(self):
        """Contact requests stay unread until they are answered."""
        contact = ContactRequest.objects.create(
            event=self.event, sender=self.peer, recipient=self.member
        )
        Notification.objects.create(
            recipient=self.member,
            event=self.event,
            contact_request=contact,
            subject="Contact request",
            body="Hello",
        )
        data = self.client.post(self.url).json()
        self.assertIn(f'href="{reverse("notifications")}"', data["html"])
        self.assertTrue(
            Notification.objects.get(contact_request=contact).read_at is None
        )

    def test_empty_bell(self):
        """Members without notifications see a friendly empty state."""
        self.member.notifications.all().delete()
        data = self.client.post(self.url).json()
        self.assertIn("all caught up", data["html"])
        self.assertEqual(data["unread_count"], 0)

    def test_only_post_and_only_members(self):
        """GET is refused and visitors are sent to log in."""
        self.assertEqual(self.client.get(self.url).status_code, 405)
        self.client.logout()
        response = self.client.post(self.url)
        self.assertEqual(response.status_code, 302)
        self.assertIn(reverse("login"), response["Location"])

    def test_bell_items_link_to_their_event(self):
        """Event updates link to the event page."""
        html = self.client.post(self.url).json()["html"]
        self.assertIn(reverse("event_detail", args=[self.event.pk]), html)

    def test_banned_members_can_open_the_bell(self):
        """A ban blocks submissions, but opening the bell is only reading."""
        admin = get_user_model().objects.create_superuser(
            username="admin", email="admin@example.com", password="pass12345"
        )
        apply_ban(admin, self.member)
        response = self.client.post(self.url)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["unread_count"], 2)
        self.assertEqual(self.client.get(reverse("notifications")).status_code, 200)


class NotificationBellAssetTests(SimpleTestCase):
    """The bell's static files handle phones and changing counts."""

    @staticmethod
    def read(path):
        with open(finders.find(path), encoding="utf-8") as handle:
            return handle.read()

    def test_phone_panel_fits_the_screen_and_scrolls(self):
        """On phones the panel stops above the tab bar and its list scrolls."""
        css = self.read("events/header.css")
        phone = css[css.rindex("@media (max-width: 720px)") :]
        self.assertIn("max-height: calc(100dvh - 64px - 76px", phone)
        self.assertIn("overflow-y: auto", phone)

    def test_tab_bar_badge_is_created_and_relabelled(self):
        """The Alerts tab gains a badge when needed and keeps its label current."""
        js = self.read("events/bell.js")
        self.assertIn('tabBadge.className = "tab-badge"', js)
        self.assertIn("tab.append(tabBadge)", js)
        self.assertIn('tab.setAttribute(\n                    "aria-label"', js)
