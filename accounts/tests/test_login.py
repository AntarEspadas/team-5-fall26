"""Tests for the login, logout and home pages."""

from django.test import TestCase
from django.core.cache import cache
from django.urls import reverse

from accounts.forms import LoginForm
from accounts.models import User

PASSWORD = "Tr1cky-Passphrase!"


class LoginViewTests(TestCase):
    """The login page at ``/accounts/login/``."""

    @classmethod
    def setUpTestData(cls):
        """Create an account to log in to."""
        cls.user = User.objects.create_user(
            "alice", "alice@example.com", PASSWORD, display_name="Alice A."
        )

    def setUp(self):
        """Set up the page URL."""
        self.url = reverse("login")

    def test_page_is_at_accounts_login(self):
        """The login page is served from /accounts/login/."""
        self.assertEqual(self.url, "/accounts/login/")

    def test_get_shows_form_and_links(self):
        """The page shows both fields, a forgot password link and a sign-up link."""
        response = self.client.get(self.url)
        self.assertEqual(response.status_code, 200)
        self.assertIsInstance(response.context["form"], LoginForm)
        self.assertContains(response, "Email or username")
        self.assertContains(response, 'name="username"')
        self.assertContains(response, 'maxlength="254"')
        self.assertContains(response, 'name="password"')
        self.assertContains(response, "Forgot password?")
        self.assertContains(response, f'href="{reverse("password_reset")}"')
        self.assertContains(response, f'href="{reverse("register")}"')

    def test_logs_in_with_username_or_email(self):
        """Either the username or the email address logs the user in."""
        for login in ("alice", "Alice@Example.com"):
            with self.subTest(login=login):
                response = self.client.post(
                    self.url, {"username": login, "password": PASSWORD}
                )
                self.assertRedirects(response, reverse("landing"))
                self.assertEqual(
                    int(self.client.session["_auth_user_id"]), self.user.pk
                )
                self.client.logout()

    def test_follows_next_parameter(self):
        """After logging in, the user goes back to the page they came from."""
        response = self.client.post(
            f"{self.url}?next=/admin/",
            {"username": "alice", "password": PASSWORD, "next": "/admin/"},
        )
        self.assertRedirects(response, "/admin/", fetch_redirect_response=False)

    def test_wrong_password_shows_a_clear_error(self):
        """Bad credentials re-render the form with an error and log no one in."""
        response = self.client.post(
            self.url, {"username": "alice", "password": "Wrong-Passphrase!"}
        )
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Invalid credentials.")
        self.assertNotIn("_auth_user_id", self.client.session)

    def test_blank_username_uses_submitted_value_for_attempt_tracking(self):
        """An invalid blank username still increments the login attempt counter."""
        response = self.client.post(self.url, {"username": "  ", "password": "bad"})

        self.assertEqual(response.status_code, 200)
        self.assertEqual(cache.get("login_attempts:"), 1)

    def test_logged_in_user_is_sent_home(self):
        """
        Someone already logged in who opens the
        login page is sent to landing page.
        """
        self.client.force_login(self.user)
        self.assertRedirects(self.client.get(self.url), reverse("landing"))

    def test_sliding_session_resets_expiry_on_activity(self):
        """
        Authenticated user's session expiry gets reset on every request.
        Verifies sliding 48-hour idle session requirement: timer restarts
        from last activity.
        """
        self.client.force_login(self.user)
        # First request, capture session expiry
        self.client.get(reverse("home"))
        expiry1 = self.client.session.get_expiry_date()

        # Make a second request shortly after
        self.client.get(reverse("home"))
        expiry2 = self.client.session.get_expiry_date()

        # The new expiry should be later than the original expiry time
        self.assertTrue(expiry2 > expiry1)


class LogoutAndHomeTests(TestCase):
    """The home page and logging out."""

    @classmethod
    def setUpTestData(cls):
        """Create an account to log in to."""
        cls.user = User.objects.create_user("alice", "alice@example.com", PASSWORD)

    def test_home_offers_login_and_signup_to_visitors(self):
        """A visitor who isn't logged in sees links to log in and sign up."""
        response = self.client.get(reverse("home"))
        self.assertContains(response, f'href="{reverse("login")}"')
        self.assertContains(response, f'href="{reverse("register")}"')

    def test_home_greets_logged_in_user(self):
        """A logged-in user is greeted by name and can log out."""
        self.client.force_login(self.user)
        response = self.client.get(reverse("home"))
        self.assertContains(response, "Hi, alice!")
        self.assertContains(response, f'action="{reverse("logout")}"')

    def test_logout_sends_user_to_login(self):
        """Logging out ends the session and opens the login page."""
        self.client.force_login(self.user)
        response = self.client.post(reverse("logout"))
        self.assertRedirects(response, reverse("landing"))
        self.assertNotIn("_auth_user_id", self.client.session)
