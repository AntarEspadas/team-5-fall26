"""Tests for resetting a forgotten password."""

import re

from django.core import mail
from django.test import TestCase
from django.urls import reverse

from accounts.models import User

OLD_PASSWORD = "Tr1cky-Passphrase!"
NEW_PASSWORD = "Fresh-Passphrase-42"


class PasswordResetTests(TestCase):
    """The forgot password flow, from the login page back to logging in."""

    @classmethod
    def setUpTestData(cls):
        """Create an account that has forgotten its password."""
        cls.user = User.objects.create_user(
            "alice", "alice@example.com", OLD_PASSWORD, display_name="Alice A."
        )

    def request_reset(self, email="alice@example.com"):
        """Submit the forgot password form and return the response."""
        return self.client.post(reverse("password_reset"), {"email": email})

    def reset_link(self):
        """Return the path of the reset link in the last email sent."""
        # Match relative path, no http:// domain
        return re.search(r"(/accounts/reset/\S+)", mail.outbox[-1].body).group(1)

    def test_request_page_loads(self):
        """The forgot password page asks for an email address."""
        response = self.client.get(reverse("password_reset"))
        self.assertEqual(response.status_code, 200)
        self.assertTemplateUsed(response, "accounts/password_reset_form.html")
        self.assertContains(response, 'name="email"')

    def test_request_emails_a_reset_link(self):
        """A registered email, in any case, gets a reset link."""
        response = self.request_reset("ALICE@Example.com")
        self.assertRedirects(response, reverse("password_reset_done"))
        self.assertEqual(len(mail.outbox), 1)
        email = mail.outbox[0]
        self.assertEqual(email.to, ["alice@example.com"])
        self.assertEqual(email.subject, "Reset your password")
        self.assertIn("Hi Alice A.,", email.body)
        self.assertIn("/accounts/reset/", email.body)

    def test_unknown_email_gets_same_page_and_no_email(self):
        """An unregistered email sees the same page, so accounts stay private."""
        response = self.request_reset("nobody@example.com")
        self.assertRedirects(response, reverse("password_reset_done"))
        self.assertEqual(mail.outbox, [])

    def test_done_page_says_to_check_email(self):
        """The confirmation page tells the user to check their inbox."""
        response = self.client.get(reverse("password_reset_done"))
        self.assertContains(response, "Check your email")

    def test_reset_redirects_to_login_and_new_password_works(self):
        """
        After a reset, the user lands on password reset complete
        page and can log in with new password.
        """
        self.request_reset()
        response = self.client.get(self.reset_link(), follow=True)
        self.assertTrue(response.context["validlink"])
        self.assertContains(response, "Choose a new password")

        response = self.client.post(
            response.redirect_chain[-1][0],
            {"new_password1": NEW_PASSWORD, "new_password2": NEW_PASSWORD},
        )
        # 现在是重定向到complete页面，不是login
        self.assertRedirects(response, reverse("password_reset_complete"))

        self.user.refresh_from_db()
        self.assertTrue(self.user.check_password(NEW_PASSWORD))
        self.assertFalse(self.user.check_password(OLD_PASSWORD))
        response = self.client.post(
            reverse("login"), {"username": "alice", "password": NEW_PASSWORD}
        )
        self.assertRedirects(response, reverse("landing"))

    def test_new_password_is_validated(self):
        """A weak or mismatched new password is rejected with a clear error."""
        self.request_reset()
        url = self.client.get(self.reset_link(), follow=True).redirect_chain[-1][0]
        cases = [
            ({"new_password1": "password", "new_password2": "password"}, "too common"),
            (
                {"new_password1": NEW_PASSWORD, "new_password2": "Other-Passphrase!"},
                "didn’t match",
            ),
        ]
        for data, message in cases:
            with self.subTest(data=data):
                response = self.client.post(url, data)
                self.assertEqual(response.status_code, 200)
                self.assertContains(response, message)
        self.user.refresh_from_db()
        self.assertTrue(self.user.check_password(OLD_PASSWORD))

    def test_link_works_only_once(self):
        """A used reset link shows an error with a way to request a new one."""
        self.request_reset()
        link = self.reset_link()
        url = self.client.get(link, follow=True).redirect_chain[-1][0]
        self.client.post(
            url, {"new_password1": NEW_PASSWORD, "new_password2": NEW_PASSWORD}
        )
        response = self.client.get(link, follow=True)
        self.assertFalse(response.context["validlink"])
        self.assertContains(response, "Reset link is invalid")
        self.assertContains(response, f'href="{reverse("password_reset")}"')
