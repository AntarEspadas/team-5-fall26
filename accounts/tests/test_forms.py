"""Tests for the registration form."""

from django.test import TestCase

from accounts.forms import RegistrationForm
from accounts.models import User

PASSWORD = "Tr1cky-Passphrase!"


def form_data(**overrides):
    """Return valid registration form data, with ``overrides`` applied."""
    return {
        "username": "newuser",
        "email": "newuser@example.com",
        "display_name": "New User",
        "password1": PASSWORD,
        "password2": PASSWORD,
        **overrides,
    }


class RegistrationFormTests(TestCase):
    """Validation and saving in ``RegistrationForm``."""

    @classmethod
    def setUpTestData(cls):
        """Create an existing account to check duplicates against."""
        User.objects.create_user("taken", "taken@example.com", PASSWORD)

    def test_valid_form_creates_user_with_hashed_password(self):
        """A valid form saves every field and stores only the password hash."""
        form = RegistrationForm(data=form_data())
        self.assertTrue(form.is_valid(), form.errors)
        user = User.objects.get(pk=form.save().pk)
        self.assertEqual(user.username, "newuser")
        self.assertEqual(user.email, "newuser@example.com")
        self.assertEqual(user.display_name, "New User")
        self.assertNotIn(PASSWORD, user.password)
        self.assertTrue(user.check_password(PASSWORD))

    def test_display_name_is_optional(self):
        """The form is valid without a display name."""
        form = RegistrationForm(data=form_data(display_name=""))
        self.assertTrue(form.is_valid(), form.errors)
        self.assertEqual(form.save().display_name, "")

    def test_email_is_saved_in_lowercase(self):
        """Email addresses are lowercased before they are saved."""
        form = RegistrationForm(data=form_data(email="New.User@Example.COM"))
        self.assertTrue(form.is_valid(), form.errors)
        self.assertEqual(form.save().email, "new.user@example.com")

    def test_duplicate_email_is_reported_once(self):
        """A registered email, in any case, gets a single clear error."""
        form = RegistrationForm(data=form_data(email="TAKEN@Example.com"))
        self.assertFalse(form.is_valid())
        self.assertEqual(
            form.errors["email"],
            ["An account with this email address already exists."],
        )

    def test_invalid_input_is_rejected_with_a_clear_message(self):
        """Each broken rule adds a readable error to the right field."""
        cases = [
            ("username", {"username": ""}, "This field is required."),
            ("username", {"username": "Taken"}, "A user with that username already"),
            ("username", {"username": "a" * 21}, "at most 20 characters"),
            ("username", {"username": "josé"}, "Enter a valid username."),
            ("username", {"username": "has space"}, "Enter a valid username."),
            ("email", {"email": ""}, "This field is required."),
            ("email", {"email": "not-an-email"}, "Enter a valid email address."),
            ("display_name", {"display_name": "d" * 51}, "at most 50 characters"),
            ("password1", {"password1": ""}, "This field is required."),
            ("password2", {"password2": "Other-Passphrase!"}, "didn’t match"),
            ("password2", {"password1": "Sh0rt!", "password2": "Sh0rt!"}, "too short"),
            (
                "password2",
                {"password1": "password", "password2": "password"},
                "too common",
            ),
            (
                "password2",
                {"password1": "4829105736", "password2": "4829105736"},
                "entirely numeric",
            ),
            (
                "password2",
                {"password1": "newuser123", "password2": "newuser123"},
                "too similar to the username",
            ),
        ]
        for field, overrides, message in cases:
            with self.subTest(field=field, overrides=overrides):
                form = RegistrationForm(data=form_data(**overrides))
                self.assertFalse(form.is_valid())
                self.assertTrue(
                    any(message in error for error in form.errors.get(field, [])),
                    form.errors,
                )
        self.assertEqual(User.objects.count(), 1)
