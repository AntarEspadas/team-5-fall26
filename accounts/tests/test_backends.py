"""Tests for logging in with an email address or a username."""

from django.contrib.auth import authenticate
from django.test import TestCase

from accounts.models import User

PASSWORD = "Tr1cky-Passphrase!"


class EmailOrUsernameBackendTests(TestCase):
    """``authenticate`` with ``EmailOrUsernameBackend``."""

    @classmethod
    def setUpTestData(cls):
        """Create an account to log in to."""
        cls.user = User.objects.create_user("Alice", "alice@example.com", PASSWORD)

    def test_logs_in_with_username_or_email_in_any_case(self):
        """The username and the email address both work, regardless of case."""
        for login in ("Alice", "alice", "alice@example.com", "ALICE@Example.com"):
            with self.subTest(login=login):
                self.assertEqual(
                    authenticate(username=login, password=PASSWORD), self.user
                )

    def test_rejects_wrong_password_or_unknown_account(self):
        """A wrong password or an unknown username or email logs no one in."""
        cases = [
            ("alice", "Wrong-Passphrase!"),
            ("bob", PASSWORD),
            ("bob@example.com", PASSWORD),
        ]
        for login, password in cases:
            with self.subTest(login=login, password=password):
                self.assertIsNone(authenticate(username=login, password=password))

    def test_rejects_missing_credentials(self):
        """Without a login or a password, no one is logged in."""
        self.assertIsNone(authenticate(password=PASSWORD))
        self.assertIsNone(authenticate(username="alice"))

    def test_rejects_inactive_user(self):
        """A deactivated account can't log in."""
        self.user.is_active = False
        self.user.save()
        self.assertIsNone(authenticate(username="alice", password=PASSWORD))

    def test_username_matching_another_users_email(self):
        """If a login matches two accounts, the one whose password fits logs in."""
        other = User.objects.create_user(
            "alice@example.com", "other@example.com", "Other-Passphrase!"
        )
        self.assertEqual(
            authenticate(username="alice@example.com", password=PASSWORD), self.user
        )
        self.assertEqual(
            authenticate(username="alice@example.com", password="Other-Passphrase!"),
            other,
        )
