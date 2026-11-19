"""Test the public share action and the URLs that friends receive."""

from urllib.parse import urlencode

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.urls import reverse

from events.models import Event, Registration


class ShareEventTests(TestCase):
    """The share link opens the same event without exposing page state."""

    @classmethod
    def setUpTestData(cls):
        """Create two events and an account that can register."""
        cls.event = Event.objects.create(guid="share-walk", title="Park walk")
        cls.other = Event.objects.create(guid="share-yoga", title="Park yoga")
        cls.member = get_user_model().objects.create_user(
            "sharemember", "share@example.com", password="ShareTestPass123!"
        )

    def test_visitor_can_share_the_direct_event_url(self):
        """Guests get the share button, a labeled input, and a live status."""
        path = reverse("event_detail", args=[self.event.pk])
        response = self.client.get(path + "?photo_page=2&from_event=5", secure=True)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            response.context["share_event_url"], "https://testserver" + path
        )
        self.assertContains(response, 'value="https://testserver' + path + '"')
        self.assertContains(response, "data-share-button")
        self.assertContains(response, "Share event")
        self.assertContains(response, 'role="status" aria-live="polite"')
        self.assertContains(response, 'for="share-event-url"')
        self.assertContains(response, "events/share-event.js")
        self.assertContains(response, "events/share-event.css")
        self.assertContains(response, "Log in to Register")

    def test_each_event_has_its_own_link(self):
        """The link uses the event route, not a search URL or another event."""
        for event in (self.event, self.other):
            path = reverse("event_detail", args=[event.pk])
            response = self.client.get(path)
            self.assertEqual(
                response.context["share_event_url"], "http://testserver" + path
            )

    def test_friend_can_open_and_register_from_shared_url(self):
        """A signed-in friend can open the copied link and register normally."""
        path = reverse("event_detail", args=[self.event.pk])
        self.client.force_login(self.member)
        response = self.client.get(path)
        self.assertContains(response, "Share event")
        self.assertContains(response, "Register for Event")
        self.assertFalse(Registration.objects.exists())
        self.client.post(reverse("event_register", args=[self.event.pk]))
        self.assertTrue(
            Registration.objects.filter(event=self.event, user=self.member).exists()
        )

    def test_signed_out_friend_can_log_in_and_return_to_event(self):
        """Follow the login flow from a shared link before registering."""
        path = reverse("event_detail", args=[self.event.pk])
        response = self.client.get(path)
        login_url = reverse("login") + "?next=" + path
        self.assertContains(response, "Log in to Register")
        login = self.client.get(login_url)
        self.assertEqual(login.status_code, 200)
        response = self.client.post(
            login_url,
            {
                "username": self.member.username,
                "password": "ShareTestPass123!",
                "next": path,
            },
        )
        self.assertRedirects(response, path)
        self.client.post(reverse("event_register", args=[self.event.pk]))
        self.assertTrue(
            Registration.objects.filter(event=self.event, user=self.member).exists()
        )

    def test_new_friend_can_create_account_then_return_to_shared_event(self):
        """A friend without an account keeps the event through signup and login."""
        path = reverse("event_detail", args=[self.event.pk])
        query = urlencode({"next": path})
        login_url = reverse("login") + "?" + query
        signup_url = reverse("register") + "?" + query

        response = self.client.get(path)
        self.assertContains(response, "Log in to Register")
        response = self.client.get(login_url)
        self.assertContains(response, f'href="{signup_url}"', count=2)
        response = self.client.get(signup_url)
        self.assertContains(response, f'name="next" value="{path}"')
        self.assertEqual(response.context["login_url"], login_url)

        response = self.client.post(
            reverse("register"),
            {
                "username": "newsharefriend",
                "email": "newshare@example.com",
                "display_name": "New Friend",
                "password1": "Tr1cky-Sunset-Walk!",
                "password2": "Tr1cky-Sunset-Walk!",
                "next": path,
            },
        )
        self.assertRedirects(response, login_url)
        response = self.client.post(
            reverse("login"),
            {
                "username": "newsharefriend",
                "password": "Tr1cky-Sunset-Walk!",
                "next": path,
            },
        )
        self.assertRedirects(response, path)
        self.client.post(reverse("event_register", args=[self.event.pk]))
        self.assertTrue(
            Registration.objects.filter(
                event=self.event, user__username="newsharefriend"
            ).exists()
        )

    def test_invalid_signup_preserves_shared_event(self):
        """An invalid signup stays on the form without dropping the destination."""
        path = reverse("event_detail", args=[self.event.pk])
        response = self.client.post(reverse("register"), {"next": path})
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, f'name="next" value="{path}"')
        self.assertEqual(response.context["next"], path)

    def test_signup_rejects_unsafe_return_destinations(self):
        """Untrusted destinations never propagate into signup or login."""
        for target in (
            "https://evil.example/",
            "//evil.example/",
            "///evil.example/",
            "http://testserver/events/1/",
        ):
            with self.subTest(target=target):
                response = self.client.get(
                    reverse("register"), {"next": target}, secure=True
                )
                self.assertEqual(response.context["next"], "")
                self.assertEqual(response.context["login_url"], reverse("login"))

        response = self.client.post(
            reverse("register"),
            {
                "username": "unsafefriend",
                "email": "unsafe@example.com",
                "display_name": "Friend",
                "password1": "FriendPass123!",
                "password2": "FriendPass123!",
                "next": "//evil.example/",
            },
        )
        self.assertRedirects(response, reverse("login"))
