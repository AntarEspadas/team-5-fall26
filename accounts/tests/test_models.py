"""Tests for the custom user model."""

from django.contrib.auth import get_user_model
from django.core.exceptions import ValidationError
from django.db import IntegrityError
from django.test import TestCase

from accounts.models import User

PASSWORD = "Tr1cky-Passphrase!"


class UserModelTests(TestCase):
    """Field rules and helpers on the custom ``User`` model."""

    def build_user(self, **fields):
        """Return an unsaved user with valid defaults, overridden by ``fields``."""
        user = User(**{"username": "alice", "email": "alice@example.com", **fields})
        user.set_password(PASSWORD)
        return user

    def test_is_the_active_user_model(self):
        """Django's auth framework uses the accounts app's user model."""
        self.assertIs(get_user_model(), User)

    def test_create_user_stores_only_a_password_hash(self):
        """The raw password is never saved, only a hash that can verify it."""
        user = User.objects.create_user("alice", "alice@example.com", PASSWORD)
        user.refresh_from_db()
        self.assertNotIn(PASSWORD, user.password)
        self.assertTrue(user.check_password(PASSWORD))

    def test_create_user_lowercases_email(self):
        """Email addresses are stored in lowercase."""
        user = User.objects.create_user("alice", "Alice@Example.COM", PASSWORD)
        self.assertEqual(user.email, "alice@example.com")

    def test_clean_lowercases_email(self):
        """Model validation lowercases and trims the email address too."""
        user = self.build_user(email="  Alice@Example.COM ")
        user.clean()
        self.assertEqual(user.email, "alice@example.com")

    def test_email_is_unique_regardless_of_case(self):
        """The database rejects a second account with the same email address."""
        User.objects.create_user("alice", "alice@example.com", PASSWORD)
        with self.assertRaises(IntegrityError):
            User.objects.create_user("bob", "ALICE@example.com", PASSWORD)

    def test_valid_users_pass_validation(self):
        """Users at the length limits, or without a display name, are valid."""
        self.build_user(username="a" * 20, display_name="d" * 50).full_clean()
        self.build_user(username="a.b@c+d-e_f1").full_clean()
        self.build_user(display_name="").full_clean()

    def test_invalid_fields_fail_validation(self):
        """Usernames, emails and display names that break the rules are rejected."""
        cases = {
            "username": ["", "a" * 21, "josé", "has space"],
            "email": ["", "not-an-email"],
            "display_name": ["d" * 51],
        }
        for field, values in cases.items():
            for value in values:
                with self.subTest(field=field, value=value):
                    with self.assertRaises(ValidationError) as caught:
                        self.build_user(**{field: value}).full_clean()
                    self.assertIn(field, caught.exception.message_dict)

    def test_get_display_name_prefers_display_name(self):
        """``get_display_name`` returns the display name when one is set."""
        user = self.build_user(display_name="Alice A.")
        self.assertEqual(user.get_display_name(), "Alice A.")

    def test_get_display_name_falls_back_to_username(self):
        """``get_display_name`` returns the username when no display name is set."""
        self.assertEqual(self.build_user().get_display_name(), "alice")
