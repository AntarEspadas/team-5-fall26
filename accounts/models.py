"""Data models for user accounts."""

from django.contrib.auth.models import AbstractUser, UserManager
from django.contrib.auth.validators import ASCIIUsernameValidator
from django.db import models


class AccountManager(UserManager):
    """User manager that stores email addresses in lowercase."""

    @classmethod
    def normalize_email(cls, email):
        """Lowercase the whole address so emails are unique regardless of case."""
        return super().normalize_email(email).lower()


class ContactPermission(models.TextChoices):
    """Available contact permission settings."""

    APPROVAL_REQUIRED = "approval", "Require approval"
    OPEN = "open", "Allow direct contact"


class User(AbstractUser):
    """A person who signs up with a unique username and email address.

    Builds on Django's built-in user, so login, password hashing and
    permissions work as usual. On top of that, usernames are limited to 20
    ASCII characters, each email address can only be registered once, and
    people can choose an optional display name.
    """

    username_validator = ASCIIUsernameValidator()

    username = models.CharField(
        "username",
        max_length=20,
        unique=True,
        help_text=(
            "Required. 20 characters or fewer. "
            "Unaccented letters, digits and @/./+/-/_ only."
        ),
        validators=[username_validator],
        error_messages={"unique": "A user with that username already exists."},
    )
    email = models.EmailField(
        "email address",
        unique=True,
        error_messages={
            "unique": "An account with this email address already exists.",
        },
    )
    display_name = models.CharField(
        "display name",
        max_length=50,
        blank=True,
        help_text="Optional. 50 characters or fewer.",
    )
    profile_photo = models.ImageField(
        "profile photo", upload_to="profile_photos/", blank=True
    )
    bio = models.CharField(
        "bio",
        max_length=300,
        blank=True,
        help_text="Optional. Tell us about yourself in 300 characters or fewer.",
    )
    pronouns = models.CharField(
        "pronouns",
        max_length=50,
        blank=True,
        help_text="Optional. For example, she/her, he/him, or they/them.",
    )
    interests = models.JSONField(
        "interests",
        default=list,
        blank=True,
        help_text="Choose the activities and topics you are interested in.",
    )
    contact_permission = models.CharField(
        "contact permission",
        max_length=10,
        choices=ContactPermission.choices,
        default=ContactPermission.APPROVAL_REQUIRED,
        help_text=(
            "Choose whether other users need your approval " "before contacting you."
        ),
    )

    share_profile_with_attendees = models.BooleanField(
        "share profile with event attendees",
        default=False,
        help_text=(
            "Allow people interested in the same event to see "
            "your public profile information."
        ),
    )

    objects = AccountManager()

    def get_display_name(self):
        """Return the display name, or the username if no display name is set."""
        return self.display_name or self.username
