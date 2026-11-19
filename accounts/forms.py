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

    template_name = "accounts/widgets/profile_photo_input.html"


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
            "Similar-event suggestions use your saved choices and update "
            "when you change them."
        ),
    )

    allow_direct_contact = forms.BooleanField(
        label="Allow anyone to contact me directly",
        required=False,
    )
    profile_photo_crop_x = forms.FloatField(required=False, widget=forms.HiddenInput())
    profile_photo_crop_y = forms.FloatField(required=False, widget=forms.HiddenInput())
    profile_photo_crop_zoom = forms.FloatField(
        required=False, min_value=1, max_value=3, widget=forms.HiddenInput()
    )

    share_profile_with_attendees = forms.BooleanField(
        label="Share my profile with other event attendees",
        help_text=(
            "When enabled, people interested in the same event can "
            "see your display name, profile photo, bio, and pronouns."
        ),
        required=False,
    )

    discoverable_for_similar_events = forms.BooleanField(
        label="Show my public profile for similar events",
        help_text=(
            "People browsing related events can discover your public profile "
            "when your saved interests match theirs. Only a shared event "
            "category is shown. You must also enable profile sharing above."
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
            "discoverable_for_similar_events",
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
            self.fields["profile_photo_crop_x"].initial = (
                self.instance.profile_photo_crop_x
            )
            self.fields["profile_photo_crop_y"].initial = (
                self.instance.profile_photo_crop_y
            )
            self.fields["profile_photo_crop_zoom"].initial = (
                self.instance.profile_photo_crop_zoom
            )

        if self.instance and self.instance.pk:
            self.fields["allow_direct_contact"].initial = (
                self.instance.contact_permission == ContactPermission.OPEN
            )
        else:
            self.fields["allow_direct_contact"].initial = False

    def clean_profile_photo(self):
        """Validate and sanitize new images while preserving the original framing."""

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

    def _crop_position(self, field_name, default):
        """Return a normalized 0..1 crop position from the submitted form."""

        position = self.cleaned_data[field_name]
        if position is None:
            return default
        return min(1.0, max(0.0, position))

    def save(self, commit=True):
        """Save profile data and render the selected square crop."""

        user = super().save(commit=False)
        old_x = user.profile_photo_crop_x
        old_y = user.profile_photo_crop_y
        old_zoom = user.profile_photo_crop_zoom
        replacing_photo = "profile_photo" in self.changed_data
        default_x, default_y = (0.5, 0.5) if replacing_photo else (old_x, old_y)
        user.profile_photo_crop_x = self._crop_position(
            "profile_photo_crop_x", default_x
        )
        user.profile_photo_crop_y = self._crop_position(
            "profile_photo_crop_y", default_y
        )
        user.profile_photo_crop_zoom = self.cleaned_data["profile_photo_crop_zoom"] or (
            1.0 if replacing_photo else old_zoom
        )
        crop_changed = (old_x, old_y, old_zoom) != (
            user.profile_photo_crop_x,
            user.profile_photo_crop_y,
            user.profile_photo_crop_zoom,
        )
        user.interests = self.cleaned_data["interests"]

        if self.cleaned_data["allow_direct_contact"]:
            user.contact_permission = ContactPermission.OPEN
        else:
            user.contact_permission = ContactPermission.APPROVAL_REQUIRED

        if not user.profile_photo:
            user.profile_photo_crop = ""
        elif replacing_photo or crop_changed or not user.profile_photo_crop:
            crop = self._render_profile_photo(user)
            if crop is not None:
                user.profile_photo_crop = crop
            elif replacing_photo:
                user.profile_photo_crop = ""
            else:
                user.profile_photo_crop_x = old_x
                user.profile_photo_crop_y = old_y
                user.profile_photo_crop_zoom = old_zoom

        if commit:
            user.save()
            self.save_m2m()

        return user

    def _render_profile_photo(self, user):
        """Prepare a square crop for the model to save with the source photo."""

        try:
            upload = self.cleaned_data["profile_photo"]
            if isinstance(upload, ContentFile):
                source_file = BytesIO(upload.read())
                upload.seek(0)
            else:
                source_file = user.profile_photo.storage.open(
                    user.profile_photo.name, "rb"
                )
            with source_file, Image.open(source_file) as source:
                image = source.convert("RGB")
                width, height = image.size
                side = max(1, round(min(width, height) / user.profile_photo_crop_zoom))
                crop_x = round((width - side) * user.profile_photo_crop_x)
                crop_y = round((height - side) * user.profile_photo_crop_y)
                image = image.crop((crop_x, crop_y, crop_x + side, crop_y + side))
                output = BytesIO()
                image.save(output, format="JPEG", quality=90)
        except (OSError, ValueError):
            return None

        return ContentFile(output.getvalue(), name=f"{uuid4().hex}.jpg")


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
