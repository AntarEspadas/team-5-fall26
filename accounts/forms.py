"""Forms for account registration, login, and profile settings."""

from io import BytesIO
from uuid import uuid4

from PIL import Image, ImageOps
from django import forms
from django.contrib.auth.forms import AuthenticationForm, UserCreationForm
from django.core.files.base import ContentFile

from .models import ContactPermission, User


class ProfilePhotoInput(forms.ClearableFileInput):
    """File input that hides the current photo URL but keeps clear support."""

    initial_text = ""
    input_text = ""

    def get_context(self, name, value, attrs):
        """Keep the current file value for Django's clear-checkbox logic."""
        context = super().get_context(name, value, attrs)
        context["widget"]["value"] = None
        return context


class AccountSettingsForm(forms.ModelForm):
    """Edit public profile fields and contact preferences."""

    INTEREST_CHOICES = [
        ("walking", "Walking & Hiking"),
        ("gardening", "Gardening"),
        ("volunteering", "Volunteering"),
        ("nature", "Nature & Wildlife"),
        ("sports", "Sports & Fitness"),
        ("photography", "Photography"),
        ("community", "Community Events"),
        ("cleanup", "Park Cleanup"),
        ("arts", "Arts & Culture"),
        ("other", "Other"),
    ]

    interests = forms.MultipleChoiceField(
        label="My interests",
        choices=INTEREST_CHOICES,
        required=False,
        widget=forms.CheckboxSelectMultiple,
        help_text=(
            "Choose any interests that describe you. "
            "You can change these at any time."
        ),
    )

    allow_direct_contact = forms.BooleanField(
        label="Allow anyone to contact me directly",
        required=False,
    )

    share_profile_with_attendees = forms.BooleanField(
        label="Share my profile with other event attendees",
        help_text=(
            "When enabled, people interested in the same event can "
            "see your display name, profile photo, bio, and pronouns."
        ),
        required=False,
    )

    class Meta:
        """Expose only profile fields the account owner may edit."""

        model = User
        fields = (
            "display_name",
            "profile_photo",
            "bio",
            "pronouns",
            "interests",
            "share_profile_with_attendees",
        )
        widgets = {
            "profile_photo": ProfilePhotoInput(
                attrs={"accept": "image/jpeg,image/png,image/webp"}
            ),
            "bio": forms.Textarea(attrs={"rows": 4}),
        }
        help_texts = {
            "profile_photo": (
                "Optional. JPEG, PNG, or WebP; up to 5 MB " "and 4096 pixels per side."
            ),
        }

    def __init__(self, *args, **kwargs):
        """Initialize upload limits and the saved contact preference."""

        super().__init__(*args, **kwargs)
        self.fields["profile_photo"].max_length = 512

        if self.instance and self.instance.pk:
            self.fields["interests"].initial = self.instance.interests or []

        if self.instance and self.instance.pk:
            self.fields["allow_direct_contact"].initial = (
                self.instance.contact_permission == ContactPermission.OPEN
            )
        else:
            self.fields["allow_direct_contact"].initial = False

    def save(self, commit=True):
        """Save profile data, interests, and contact preferences."""

        user = super().save(commit=False)

        user.interests = self.cleaned_data["interests"]

        if self.cleaned_data["allow_direct_contact"]:
            user.contact_permission = ContactPermission.OPEN
        else:
            user.contact_permission = ContactPermission.APPROVAL_REQUIRED

        if commit:
            user.save()
            self.save_m2m()

        return user

    def clean_profile_photo(self):
        """Validate and re-encode new images without embedded metadata."""

        photo = self.cleaned_data.get("profile_photo")

        if not photo or not hasattr(photo, "image"):
            return photo

        if photo.size > 5 * 1024 * 1024:
            raise forms.ValidationError("Choose a photo no larger than 5 MB.")

        if photo.image.format not in {"JPEG", "PNG", "WEBP"}:
            raise forms.ValidationError("Choose a JPEG, PNG, or WebP photo.")

        if max(photo.image.size) > 4096:
            raise forms.ValidationError("Photos must be at most 4096 pixels per side.")

        try:
            photo.seek(0)
            with Image.open(photo) as source:
                normalized = ImageOps.exif_transpose(source).convert("RGB")
                normalized.thumbnail((1024, 1024))
                normalized.info.clear()

                output = BytesIO()
                normalized.save(output, format="JPEG", quality=90)

        except (OSError, ValueError) as error:
            raise forms.ValidationError("Choose a valid, undamaged photo.") from error

        return ContentFile(
            output.getvalue(),
            name=f"{uuid4().hex}.jpg",
        )


class RegistrationForm(UserCreationForm):
    """Register a new user account."""

    class Meta:
        model = User
        fields = ("username", "email", "display_name")

    def clean_email(self):
        """Normalize email addresses and reject duplicates."""

        email = self.cleaned_data.get("email", "").strip().lower()

        if User.objects.filter(email__iexact=email).exists():
            raise forms.ValidationError(
                "An account with this email address already exists."
            )

        return email


class LoginForm(AuthenticationForm):
    """Authenticate users with a username or email address."""

    username = forms.CharField(
        label="Email or username",
        widget=forms.TextInput(attrs={"autofocus": True}),
    )
