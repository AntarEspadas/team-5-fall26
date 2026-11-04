"""Tests for the registration page and custom login page."""

import time
from django.contrib.messages import get_messages
from django.test import TestCase
from django.urls import reverse
from django.core.cache import cache

from accounts.forms import RegistrationForm
from accounts.models import User

PASSWORD = "Tr1cky-Passphrase!"


class RegisterViewTests(TestCase):
    """The registration page at ``/accounts/register/``."""

    def setUp(self):
        """Set up the page URL and a valid submission."""
        self.url = reverse("register")
        self.data = {
            "username": "newuser",
            "email": "NewUser@Example.com",
            "display_name": "New User",
            "password1": PASSWORD,
            "password2": PASSWORD,
        }

    def messages_for(self, response):
        """Return the text of the messages queued for the response's request."""
        return [str(message) for message in get_messages(response.wsgi_request)]

    def test_page_is_at_accounts_register(self):
        """The registration page is served from /accounts/register/."""
        self.assertEqual(self.url, "/accounts/register/")

    def test_get_shows_form_and_login_link(self):
        """The page shows every registration field and a link to log in."""
        response = self.client.get(self.url)
        self.assertEqual(response.status_code, 200)
        self.assertTemplateUsed(response, "accounts/register.html")
        self.assertIsInstance(response.context["form"], RegistrationForm)
        for name in ("username", "email", "display_name", "password1", "password2"):
            self.assertContains(response, f'name="{name}"')
        self.assertContains(response, "Already have an account?")
        login_path = reverse("login")
        self.assertContains(response, f'href="{login_path}"')

    def test_login_link_opens_the_login_page(self):
        """The "Log in" link on the registration page leads to a working page."""
        response = self.client.get(self.client.get(self.url).context["login_url"])
        self.assertEqual(response.status_code, 200)
        self.assertTemplateUsed(response, "accounts/login.html")

    def test_valid_submission_creates_account_and_redirects_to_login(self):
        """A valid submission saves the user and redirects with a success message."""
        response = self.client.post(self.url, self.data)
        expected_url = reverse("login")
        self.assertRedirects(response, expected_url, fetch_redirect_response=False)
        user = User.objects.get(username="newuser")
        self.assertEqual(user.email, "newuser@example.com")
        self.assertEqual(user.display_name, "New User")
        self.assertNotIn(PASSWORD, user.password)
        self.assertTrue(user.check_password(PASSWORD))
        self.assertEqual(
            self.messages_for(response),
            ["Welcome, New User! Your account has been created. Please log in."],
        )

    def test_success_message_uses_username_without_display_name(self):
        """Without a display name, the success message greets the username."""
        response = self.client.post(self.url, {**self.data, "display_name": ""})
        self.assertEqual(
            self.messages_for(response),
            ["Welcome, newuser! Your account has been created. Please log in."],
        )

    def test_success_message_is_shown_on_the_next_page(self):
        """The success message is displayed on the next page the user opens."""
        self.client.post(self.url, self.data)
        response = self.client.get(self.url)
        self.assertContains(response, "Welcome, New User! Your account has been")

    def test_registered_user_can_log_in(self):
        """Someone who registers can then log in with their new credentials."""
        self.client.post(self.url, self.data)
        self.assertTrue(self.client.login(username="newuser", password=PASSWORD))

    def test_invalid_submission_shows_errors_and_creates_nothing(self):
        """Invalid input re-renders the form with errors and saves nothing."""
        User.objects.create_user("taken", "taken@example.com", PASSWORD)
        response = self.client.post(
            self.url,
            {
                **self.data,
                "username": "taken",
                "email": "Taken@Example.com",
                "password2": "Other-Passphrase!",
            },
        )
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "A user with that username already exists.")
        self.assertContains(
            response, "An account with this email address already exists."
        )
        self.assertContains(response, "The two password fields didn’t match.")
        self.assertEqual(User.objects.count(), 1)
        self.assertEqual(self.messages_for(response), [])


class CustomLoginViewTests(TestCase):
    """
    Unit and integration tests for the custom login view.
    Covers username/email login, brute force lockout, lock expiry timestamp context,
    session expiry, and logout functionality.
    """

    def setUp(self):
        """Initialize test user, login url and clear cache before each test."""
        self.login_url = reverse("login")
        self.logout_url = reverse("logout")
        self.test_user = User.objects.create_user(
            username="testuser", email="test@example.com", password=PASSWORD
        )
        cache.clear()
        self.attempt_key_prefix = "login_attempts:"
        # Match the constant defined in views.py
        self.lock_key_prefix = "login_lock_expiry:"

    def test_login_with_username_success(self):
        """User can log in by providing their username and correct password."""
        response = self.client.post(
            self.login_url, {"username": "testuser", "password": PASSWORD}, follow=True
        )
        self.assertTrue(response.context["user"].is_authenticated)

    def test_login_with_email_success(self):
        """User can log in by providing their registered email and correct password."""
        response = self.client.post(
            self.login_url,
            {"username": "test@example.com", "password": PASSWORD},
            follow=True,
        )
        self.assertTrue(response.context["user"].is_authenticated)

    def test_failed_attempt_increments_counter(self):
        """
        A single failed login attempt increments the
        cache counter with correct key.
        """
        self.client.post(
            self.login_url, {"username": "testuser", "password": "WrongPass"}
        )
        stored_count = cache.get(f"{self.attempt_key_prefix}testuser")
        self.assertEqual(stored_count, 1)

    def test_lockout_after_five_failed_attempts(self):
        """
        After 5 consecutive failed logins, user receives lockout
        message and lock key is set.
        """
        login_data = {"username": "testuser", "password": "wrongpass"}
        attempt_key = f"{self.attempt_key_prefix}testuser"
        lock_key = f"{self.lock_key_prefix}testuser"

        # 5 failed tries
        for _ in range(5):
            self.client.post(reverse("login"), login_data)

        sixth_response = self.client.post(reverse("login"), login_data)
        self.assertEqual(sixth_response.status_code, 200)

        # Check lock key is created
        self.assertIsNotNone(cache.get(lock_key))
        # Attempt counter stops at 5, sixth request hits
        # dispatch lock and skips increment
        self.assertEqual(cache.get(attempt_key), 5)

    def test_lock_expiry_timestamp_passed_to_template_context_on_lock(self):
        """
        When locked, lock_expiry timestamp is injected into
        template context for frontend countdown.
        """
        wrong_payload = {"username": "testuser", "password": "WrongPass"}
        for _ in range(5):
            self.client.post(self.login_url, wrong_payload)
        response = self.client.post(self.login_url, wrong_payload)
        self.assertIn("lock_expiry", response.context)
        self.assertIsNotNone(response.context["lock_expiry"])

    def test_locked_state_blocks_login_before_validation(self):
        """If identifier is locked, dispatch blocks request before credential check."""
        # Set lock cache key directly
        lock_key = f"{self.lock_key_prefix}testuser"
        future_ts = time.time() + 600
        cache.set(lock_key, future_ts, 600)

        response = self.client.post(
            reverse("login"), {"username": "testuser", "password": "wrongpass"}
        )
        # Should render login page (200), not redirect
        self.assertEqual(response.status_code, 200)
        # Verify lock key exists in cache
        self.assertIsNotNone(cache.get(lock_key))

    def test_successful_login_clears_attempt_and_lock_counter(self):
        """A successful login clears the stored failed attempt counter and lock key."""
        wrong_payload = {"username": "testuser", "password": "WrongPass"}
        self.client.post(self.login_url, wrong_payload)
        self.client.post(self.login_url, {"username": "testuser", "password": PASSWORD})
        self.assertIsNone(cache.get(f"{self.attempt_key_prefix}testuser"))
        self.assertIsNone(cache.get(f"{self.lock_key_prefix}testuser"))

    def test_session_expiry_set_to_48_hours(self):
        """Successful login sets session expiry to 48 hours of inactivity."""
        self.client.post(self.login_url, {"username": "testuser", "password": PASSWORD})
        session = self.client.session
        self.assertEqual(session.get_expiry_age(), 48 * 60 * 60)

    def test_logout_post_request_works(self):
        """POST logout request logs user out and redirects to landing page."""
        # Log in first
        self.client.login(username="testuser", password=PASSWORD)
        response = self.client.post(self.logout_url, follow=True)
        self.assertFalse(response.context["user"].is_authenticated)
        self.assertRedirects(response, reverse("landing"))
