"""Tests for the user admin."""

from django.test import TestCase
from django.urls import reverse

from accounts.models import User

PASSWORD = "Tr1cky-Passphrase!"


class UserAdminTests(TestCase):
    """The admin pages for the custom user model."""

    @classmethod
    def setUpTestData(cls):
        """Create a superuser to use the admin with."""
        cls.admin = User.objects.create_superuser(
            "admin", "admin@example.com", PASSWORD
        )

    def setUp(self):
        """Log in as the superuser."""
        self.client.force_login(self.admin)

    def test_list_and_change_pages_load(self):
        """The user list and a user's change page render."""
        for url in (
            reverse("admin:accounts_user_changelist"),
            reverse("admin:accounts_user_change", args=[self.admin.pk]),
        ):
            with self.subTest(url=url):
                self.assertEqual(self.client.get(url).status_code, 200)

    def test_add_user_requires_email_and_saves_display_name(self):
        """Adding a user in the admin needs an email and keeps the display name."""
        url = reverse("admin:accounts_user_add")
        data = {
            "username": "staffpick",
            "email": "Staff.Pick@Example.com",
            "display_name": "Staff Pick",
            "usable_password": "true",
            "password1": PASSWORD,
            "password2": PASSWORD,
        }

        response = self.client.post(url, {**data, "email": ""})
        self.assertEqual(response.status_code, 200)
        self.assertFalse(User.objects.filter(username="staffpick").exists())

        response = self.client.post(url, data)
        user = User.objects.get(username="staffpick")
        self.assertRedirects(
            response, reverse("admin:accounts_user_change", args=[user.pk])
        )
        self.assertEqual(user.email, "staff.pick@example.com")
        self.assertEqual(user.display_name, "Staff Pick")
        self.assertTrue(user.check_password(PASSWORD))
