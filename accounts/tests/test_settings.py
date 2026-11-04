"""Unit and integration tests for editing an account's profile."""

from io import BytesIO
from tempfile import TemporaryDirectory
from unittest.mock import patch

from PIL import Image
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import Client, TestCase, override_settings
from django.urls import reverse

from accounts.forms import AccountSettingsForm
from accounts.models import User


def photo_upload(image_format="PNG", size=(24, 24), suffix=b""):
    """Build a real in-memory image, optionally with trailing untrusted bytes."""
    output = BytesIO()
    Image.new("RGB", size, "blue").save(output, format=image_format)
    return SimpleUploadedFile(
        f"photo.{image_format.lower()}",
        output.getvalue() + suffix,
        content_type=f"image/{image_format.lower()}",
    )


class AccountSettingsTests(TestCase):
    """Exercise model, form, session, database, storage, and rendered settings."""

    @classmethod
    def setUpTestData(cls):
        """Create an owner and a separate account for authorization checks."""
        cls.user = User.objects.create_user(
            "alice",
            "alice@example.com",
            "Tr1cky-Passphrase!",
            display_name="Alice",
            bio="Original bio",
            pronouns="she/her",
        )
        cls.other = User.objects.create_user("bob", "bob@example.com")

    def setUp(self):
        """Keep uploaded files isolated and log in as the profile owner."""
        directory = TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        media = override_settings(MEDIA_ROOT=directory.name)
        media.enable()
        self.addCleanup(media.disable)
        self.client.force_login(self.user)
        self.url = reverse("account_settings")
        self.data = {
            "display_name": "Alice Updated",
            "bio": "I enjoy hiking.",
            "pronouns": "she/they",
        }

    def test_get_has_prefilled_accessible_form(self):
        """Settings display saved values, labels, limits, and upload controls."""
        response = self.client.get(self.url)
        self.assertEqual(self.url, "/accounts/settings/")
        self.assertTemplateUsed(response, "accounts/settings.html")
        self.assertIsInstance(response.context["form"], AccountSettingsForm)
        for field in self.data:
            self.assertContains(response, f'name="{field}"')
            self.assertContains(response, f'for="id_{field}"')
            self.assertEqual(
                response.context["form"].initial[field], getattr(self.user, field)
            )
        self.assertContains(response, 'enctype="multipart/form-data"')
        self.assertContains(response, 'name="csrfmiddlewaretoken"')
        self.assertContains(response, 'maxlength="300"')
        self.assertContains(response, f'href="{reverse("landing")}"')

    def test_new_account_defaults_to_approval_and_can_enable_direct_contact(self):
        """New forms default to approval, and saved opt-in enables direct contact."""
        new_user_form = AccountSettingsForm(instance=User())
        self.assertFalse(new_user_form.fields["allow_direct_contact"].initial)

        form = AccountSettingsForm(
            data={**self.data, "allow_direct_contact": "on"},
            instance=self.user,
        )
        self.assertTrue(form.is_valid(), form.errors)
        updated_user = form.save()

        self.assertEqual(updated_user.contact_permission, "open")

    def test_anonymous_get_and_post_require_login(self):
        """Visitors cannot read or change a profile without signing in."""
        self.client.logout()
        for method in (self.client.get, self.client.post):
            with self.subTest(method=method.__name__):
                response = method(self.url)
                self.assertRedirects(
                    response,
                    f'{reverse("login")}?next={self.url}',
                    fetch_redirect_response=False,
                )
        self.user.refresh_from_db()
        self.assertEqual(self.user.display_name, "Alice")

    def test_save_persists_fields_and_confirms_success(self):
        """A valid POST redirects, confirms success, and survives a new GET."""
        response = self.client.post(self.url, self.data, follow=True)
        self.assertRedirects(response, self.url)
        self.assertContains(response, "Your account settings have been saved.")
        self.user.refresh_from_db()
        for field, value in self.data.items():
            self.assertEqual(getattr(self.user, field), value)
            self.assertEqual(response.context["form"].initial[field], value)
        self.assertContains(self.client.get(reverse("landing")), "Alice Updated")

    def test_all_fields_can_be_cleared(self):
        """Blank optional fields persist and the greeting falls back to username."""
        self.assertRedirects(self.client.post(self.url, {}), self.url)
        self.user.refresh_from_db()
        for field in self.data:
            self.assertEqual(getattr(self.user, field), "")
        self.assertFalse(self.user.profile_photo)
        self.assertEqual(self.user.get_display_name(), "alice")

    def test_limits_accept_boundaries_and_reject_overflow(self):
        """Each text field enforces its documented limit before saving."""
        limits = {"display_name": 50, "bio": 300, "pronouns": 50}
        form = AccountSettingsForm(
            data={field: "a" * length for field, length in limits.items()},
            instance=self.user,
        )
        self.assertTrue(form.is_valid(), form.errors)
        for field, length in limits.items():
            with self.subTest(field=field):
                response = self.client.post(
                    self.url, {**self.data, field: "a" * (length + 1)}
                )
                self.assertEqual(response.status_code, 200)
                self.assertIn(field, response.context["form"].errors)
        self.user.refresh_from_db()
        self.assertEqual(self.user.display_name, "Alice")
        self.assertEqual(self.user.bio, "Original bio")

    def test_whitespace_is_trimmed_and_unicode_is_allowed(self):
        """User-written names, biographies, and pronouns support Unicode."""
        values = {"display_name": "  Zoë  ", "bio": "  你好  ", "pronouns": "  él  "}
        self.client.post(self.url, values)
        self.user.refresh_from_db()
        for field, value in values.items():
            self.assertEqual(getattr(self.user, field), value.strip())

    def test_cannot_change_other_accounts_or_protected_fields(self):
        """Injected IDs and authentication fields cannot alter ownership or access."""
        original_password = self.user.password
        response = self.client.post(
            f"{self.url}?pk={self.other.pk}",
            {
                **self.data,
                "id": self.other.pk,
                "pk": self.other.pk,
                "username": "hijacked",
                "email": "hijacked@example.com",
                "password": "hijacked",
                "is_staff": True,
                "is_superuser": True,
            },
        )
        self.assertRedirects(response, self.url)
        self.user.refresh_from_db()
        self.other.refresh_from_db()
        self.assertEqual(self.other.display_name, "")
        self.assertEqual(self.user.username, "alice")
        self.assertEqual(self.user.email, "alice@example.com")
        self.assertEqual(self.user.password, original_password)
        self.assertFalse(self.user.is_staff)
        self.assertFalse(self.user.is_superuser)

    def test_csrf_is_required_for_saving(self):
        """A real session cannot submit updates without a valid CSRF token."""
        client = Client(enforce_csrf_checks=True)
        client.force_login(self.user)
        self.assertEqual(client.post(self.url, self.data).status_code, 403)
        client.get(self.url)
        response = client.post(
            self.url,
            self.data,
            HTTP_X_CSRFTOKEN=client.cookies["csrftoken"].value,
        )
        self.assertRedirects(response, self.url)

    def test_profile_text_is_escaped(self):
        """Profile text is rendered as text rather than executable HTML."""
        payload = '<script>alert("test")</script>'
        response = self.client.post(
            self.url, {field: payload for field in self.data}, follow=True
        )
        self.assertNotContains(response, payload)
        self.assertContains(response, "&lt;script&gt;")

    def test_photo_formats_are_sanitized_and_stored(self):
        """Supported real images are stored as clean JPEGs under generated names."""
        for image_format in ("PNG", "JPEG", "WEBP"):
            with self.subTest(image_format=image_format):
                response = self.client.post(
                    self.url,
                    {
                        **self.data,
                        "profile_photo": photo_upload(
                            image_format, suffix=b"<script>untrusted</script>"
                        ),
                    },
                    follow=True,
                )
                self.assertRedirects(response, self.url)
                self.user.refresh_from_db()
                photo = self.user.profile_photo
                self.assertRegex(photo.name, r"^profile_photos/[0-9a-f]{32}\.jpg$")
                self.assertContains(response, f'src="{photo.url}"')
                with photo.open("rb") as saved:
                    self.assertNotIn(b"<script>", saved.read())
                    saved.seek(0)
                    with Image.open(saved) as image:
                        self.assertEqual(image.format, "JPEG")

    def test_large_dimensions_are_resized_with_aspect_ratio(self):
        """Accepted large photos are normalized to a manageable display size."""
        self.client.post(
            self.url, {**self.data, "profile_photo": photo_upload(size=(2048, 1024))}
        )
        self.user.refresh_from_db()
        with self.user.profile_photo.open("rb") as saved:
            with Image.open(saved) as image:
                self.assertEqual(image.size, (1024, 512))

    def test_existing_photo_is_kept_replaced_and_cleared(self):
        """Photos can be kept, replaced, or cleared without displaying a URL."""
        self.client.post(self.url, {**self.data, "profile_photo": photo_upload()})
        self.user.refresh_from_db()
        first_name = self.user.profile_photo.name
        self.client.post(self.url, self.data)
        self.user.refresh_from_db()
        self.assertEqual(self.user.profile_photo.name, first_name)
        self.client.post(self.url, {**self.data, "profile_photo": photo_upload()})
        self.user.refresh_from_db()
        self.assertNotEqual(self.user.profile_photo.name, first_name)
        response = self.client.get(self.url)
        self.assertContains(response, 'alt="Your current profile photo"')
        self.assertContains(response, 'type="file"')
        self.assertNotContains(response, "Currently:")
        self.assertContains(response, 'name="profile_photo-clear"')
        self.assertNotContains(response, f'href="{self.user.profile_photo.url}"')
        self.assertNotContains(response, f">{self.user.profile_photo.name}</a>")
        response = self.client.post(
            self.url, {**self.data, "profile_photo-clear": "on"}, follow=True
        )
        self.assertRedirects(response, self.url)
        self.user.refresh_from_db()
        self.assertFalse(self.user.profile_photo)
        self.assertNotContains(response, 'name="profile_photo-clear"')

    def test_clear_is_not_saved_when_other_fields_are_invalid(self):
        """Failed submissions retain the photo and keep the clear control checked."""
        self.client.post(self.url, {**self.data, "profile_photo": photo_upload()})
        self.user.refresh_from_db()
        original = self.user.profile_photo.name
        response = self.client.post(
            self.url, {**self.data, "bio": "b" * 301, "profile_photo-clear": "on"}
        )
        self.assertEqual(response.status_code, 200)
        self.assertIn("bio", response.context["form"].errors)
        self.assertContains(response, 'id="profile_photo-clear_id" checked')
        self.user.refresh_from_db()
        self.assertEqual(self.user.profile_photo.name, original)
        self.assertContains(response, f'src="{self.user.profile_photo.url}"')

    def test_invalid_photos_do_not_save_any_changes(self):
        """Spoofed, unsupported, oversized, and excessively wide files fail."""
        uploads = [
            SimpleUploadedFile("fake.png", b"not an image", "image/png"),
            photo_upload("GIF"),
            photo_upload(size=(4097, 1)),
            photo_upload(suffix=b"x" * (5 * 1024 * 1024)),
        ]
        for upload in uploads:
            with self.subTest(name=upload.name, size=upload.size):
                response = self.client.post(
                    self.url, {**self.data, "profile_photo": upload}
                )
                self.assertEqual(response.status_code, 200)
                self.assertIn("profile_photo", response.context["form"].errors)
        self.user.refresh_from_db()
        self.assertEqual(self.user.display_name, "Alice")
        self.assertFalse(self.user.profile_photo)

    def test_image_decode_failure_is_a_form_error(self):
        """A file that passes header checks but cannot decode fails gracefully."""
        upload = photo_upload()
        with patch("accounts.forms.ImageOps.exif_transpose", side_effect=OSError):
            form = AccountSettingsForm(
                data=self.data, files={"profile_photo": upload}, instance=self.user
            )
            self.assertFalse(form.is_valid())
            self.assertIn("undamaged", form.errors["profile_photo"][0])

    def test_invalid_text_preserves_existing_photo(self):
        """A failed form cannot replace a stored photo or discard submitted text."""
        self.client.post(self.url, {**self.data, "profile_photo": photo_upload()})
        self.user.refresh_from_db()
        original = self.user.profile_photo.name
        response = self.client.post(
            self.url, {**self.data, "bio": "b" * 301, "profile_photo": photo_upload()}
        )
        self.assertEqual(response.context["form"]["bio"].value(), "b" * 301)
        self.user.refresh_from_db()
        self.assertEqual(self.user.profile_photo.name, original)
