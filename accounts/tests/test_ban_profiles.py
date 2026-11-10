"""Profile ban visibility and read-only settings preserve privacy and saved data."""

from datetime import timedelta
from unittest.mock import patch
from zoneinfo import ZoneInfo

from django.contrib.auth import SESSION_KEY
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone
from django.utils.formats import date_format
from django.utils.html import escape

from accounts.ban_services import apply_ban, lift_ban
from accounts.models import ContactPermission, User, UserBan
from events.models import Event, EventInterest


class BanProfileTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.admin = User.objects.create_superuser("admin", "admin@example.com")
        cls.owner = User.objects.create_user(
            "owner",
            "owner@example.com",
            display_name="Park walker",
            bio="I enjoy the parks.",
            pronouns="they/them",
            interests=["walking", "arts"],
            profile_photo="profile_photos/existing.jpg",
            share_profile_with_attendees=True,
        )
        cls.viewer = User.objects.create_user("viewer", "viewer@example.com")
        cls.event = Event.objects.create(
            guid="ban-profile-event",
            title="Weekend walk",
            start_time=timezone.now() + timedelta(days=30),
        )
        EventInterest.objects.create(user=cls.owner, event=cls.event)
        cls.profile_url = reverse("public_profile", args=[cls.owner.username])
        cls.settings_url = reverse("account_settings")

    def setUp(self):
        self.client.force_login(self.owner)

    def test_unbanned_profile_and_settings_remain_editable(self):
        response = self.client.get(self.profile_url)
        self.assertIsNone(response.context["profile_ban"])
        self.assertNotContains(response, "Account banned")
        self.assertContains(response, "Edit profile")
        response = self.client.get(self.settings_url)
        self.assertIsNone(response.context["active_ban"])
        self.assertContains(response, "Save changes")
        self.assertContains(response, 'type="file"')
        self.assertContains(response, "accounts/profile-photo-preview.js")

    def test_temporary_banner_has_accessible_status_and_expiration(self):
        ban, _ = apply_ban(self.admin, self.owner)
        expiration = timezone.localtime(ban.expires_at, ZoneInfo("America/New_York"))
        for url in (self.profile_url, self.settings_url):
            with self.subTest(url=url):
                response = self.client.get(url)
                self.assertContains(
                    response, 'role="status" aria-labelledby="ban-banner-title"'
                )
                self.assertContains(
                    response, '<h2 id="ban-banner-title">Account banned</h2>'
                )
                self.assertContains(response, "Your account is banned")
                self.assertContains(
                    response,
                    f'datetime="{expiration.isoformat()}"',
                )
                self.assertContains(
                    response, date_format(expiration, "F j, Y, g:i A T")
                )
                self.assertContains(response, expiration.strftime("%Z"))
                self.assertContains(response, "marking events as Interested")
                self.assertContains(response, "including profile edits, are disabled")
                self.assertNotContains(response, "indefinitely")

    def test_indefinite_banner_has_no_expiration(self):
        first, _ = apply_ban(self.admin, self.owner)
        lift_ban(self.admin, first)
        ban, _ = apply_ban(self.admin, self.owner)
        self.assertIsNone(ban.expires_at)
        for url in (self.profile_url, self.settings_url):
            with self.subTest(url=url):
                response = self.client.get(url)
                self.assertContains(response, "Your account is banned")
                self.assertContains(response, "indefinitely.")
                self.assertNotContains(response, "<time ")

    def test_shared_profile_displays_owner_ban_to_another_member(self):
        ban, _ = apply_ban(self.admin, self.owner)
        self.client.force_login(self.viewer)
        response = self.client.get(self.profile_url)
        self.assertEqual(response.context["profile_ban"], ban)
        self.assertContains(response, "This account is banned")
        self.assertNotContains(response, "Your account is banned")
        for value in (
            self.owner.display_name,
            self.owner.bio,
            self.owner.pronouns,
            self.event.title,
        ):
            self.assertContains(response, value)
        self.assertContains(
            response, f'href="{reverse("event_detail", args=[self.event.pk])}"'
        )

    def test_banner_uses_profile_owner_ban_when_viewer_is_also_banned(self):
        owner_ban, _ = apply_ban(self.admin, self.owner)
        viewer_ban, _ = apply_ban(self.admin, self.viewer)
        UserBan.objects.filter(pk=viewer_ban.pk).update(expires_at=None)
        self.client.force_login(self.viewer)
        response = self.client.get(self.profile_url)
        self.assertEqual(response.context["profile_ban"], owner_ban)
        self.assertContains(response, "This account is banned")
        self.assertNotContains(response, "indefinitely")

    def test_banned_viewer_does_not_mark_unbanned_profile_as_banned(self):
        apply_ban(self.admin, self.viewer)
        self.client.force_login(self.viewer)
        response = self.client.get(self.profile_url)
        self.assertIsNone(response.context["profile_ban"])
        self.assertNotContains(response, "Account banned")
        self.assertContains(response, self.owner.display_name)

    def test_hidden_banned_profile_stays_private_to_other_members(self):
        apply_ban(self.admin, self.owner)
        User.objects.filter(pk=self.owner.pk).update(share_profile_with_attendees=False)
        self.client.force_login(self.viewer)
        response = self.client.get(self.profile_url)
        self.assertTemplateUsed(response, "accounts/profile_unavailable.html")
        self.assertEqual(response.status_code, 200)
        self.assertNotContains(response, "Account banned")
        self.assertNotContains(response, self.owner.display_name)
        self.assertNotContains(response, self.owner.bio)
        self.assertNotContains(response, self.owner.profile_photo.url)
        self.assertNotIn("profile_ban", response.context)

    def test_owner_can_read_hidden_profile_and_ban_banner(self):
        apply_ban(self.admin, self.owner)
        User.objects.filter(pk=self.owner.pk).update(share_profile_with_attendees=False)
        response = self.client.get(self.profile_url)
        self.assertTemplateUsed(response, "accounts/public_profile.html")
        self.assertContains(response, "Your account is banned")
        self.assertContains(response, self.owner.bio)

    def test_anonymous_visitors_still_need_login(self):
        apply_ban(self.admin, self.owner)
        self.client.logout()
        response = self.client.get(self.profile_url)
        self.assertRedirects(
            response,
            f'{reverse("login")}?next={self.profile_url}',
            fetch_redirect_response=False,
        )

    def test_banned_profile_hides_edit_links_and_retains_settings_access(self):
        apply_ban(self.admin, self.owner)
        User.objects.filter(pk=self.owner.pk).update(interests=[])
        response = self.client.get(self.profile_url)
        self.assertNotContains(response, "Edit profile")
        self.assertNotContains(response, "Choose your interests")
        self.assertContains(
            response, f'href="{self.settings_url}">Account settings</a>'
        )
        self.assertContains(response, self.event.title)

    def test_settings_show_saved_values_without_edit_or_upload_controls(self):
        first, _ = apply_ban(self.admin, self.owner)
        before = list(User.objects.filter(pk=self.owner.pk).values())
        for permanent in (False, True):
            if permanent:
                lift_ban(self.admin, first)
                apply_ban(self.admin, self.owner)
            with self.subTest(permanent=permanent):
                response = self.client.get(self.settings_url)
                self.assertContains(response, "settings are read-only")
                for value in (
                    self.owner.display_name,
                    self.owner.bio,
                    self.owner.pronouns,
                    "Walking &amp; Hiking",
                    "Arts &amp; Culture",
                    "Require approval",
                ):
                    self.assertContains(response, value)
                self.assertContains(response, f'src="{self.owner.profile_photo.url}"')
                self.assertContains(response, "<dd>Yes</dd>", html=True)
                for control in (
                    "Save changes",
                    'enctype="multipart/form-data"',
                    "profile_photo-clear",
                    "accounts/profile-photo-preview.js",
                    'name="bio"',
                    'name="display_name"',
                ):
                    self.assertNotContains(response, control)
                self.assertNotRegex(
                    response.content.decode(),
                    r'<input\b[^>]*\btype="(?:file|checkbox)"',
                )
                self.assertContains(response, 'href="/"')
        self.assertEqual(list(User.objects.filter(pk=self.owner.pk).values()), before)

    def test_readonly_settings_show_empty_values_and_direct_contact_preference(self):
        apply_ban(self.admin, self.owner)
        User.objects.filter(pk=self.owner.pk).update(
            display_name="",
            bio="",
            pronouns="",
            interests=[],
            profile_photo="",
            share_profile_with_attendees=False,
            contact_permission=ContactPermission.OPEN,
        )
        response = self.client.get(self.settings_url)
        self.assertContains(response, "Not set", count=3)
        self.assertContains(response, "No profile photo.")
        self.assertContains(response, "No interests added yet.")
        self.assertContains(response, "<dd>No</dd>", html=True)
        self.assertContains(response, "Allow direct contact")

    def test_readonly_settings_escape_profile_text_and_interest_labels(self):
        apply_ban(self.admin, self.owner)
        payload = '<script>alert("profile")</script>'
        User.objects.filter(pk=self.owner.pk).update(
            display_name=payload, bio=payload, pronouns=payload, interests=[payload]
        )
        for url in (self.profile_url, self.settings_url):
            with self.subTest(url=url):
                response = self.client.get(url)
                self.assertNotContains(response, payload)
                self.assertContains(response, escape(payload))

    def test_settings_remain_protected_from_forged_submissions(self):
        apply_ban(self.admin, self.owner)
        before = list(User.objects.filter(pk=self.owner.pk).values())
        response = self.client.post(
            self.settings_url,
            {"display_name": "New name", "bio": "New bio", "profile_photo-clear": "on"},
        )
        self.assertEqual(response.status_code, 403)
        self.assertEqual(list(User.objects.filter(pk=self.owner.pk).values()), before)

    def test_expiration_boundary_removes_banner_and_restores_editing_in_same_session(
        self,
    ):
        ban, _ = apply_ban(self.admin, self.owner)
        now = timezone.now()
        UserBan.objects.filter(pk=ban.pk).update(expires_at=now)
        with patch("accounts.ban_services.timezone.now", return_value=now):
            response = self.client.get(self.profile_url)
            self.assertNotContains(response, "Account banned")
            self.assertContains(response, "Edit profile")
            response = self.client.get(self.settings_url)
            self.assertNotContains(response, "Account banned")
            self.assertContains(response, "Save changes")
            self.assertContains(response, 'type="file"')
        self.assertEqual(int(self.client.session[SESSION_KEY]), self.owner.pk)

    def test_lifting_either_ban_removes_banner_and_restores_edit_controls(self):
        for permanent in (False, True):
            ban, _ = apply_ban(self.admin, self.owner)
            self.assertEqual(ban.expires_at is None, permanent)
            lift_ban(self.admin, ban)
            with self.subTest(permanent=permanent):
                response = self.client.get(self.profile_url)
                self.assertNotContains(response, "Account banned")
                self.assertContains(response, "Edit profile")
                response = self.client.get(self.settings_url)
                self.assertNotContains(response, "Account banned")
                self.assertContains(response, "Save changes")
        self.assertEqual(self.owner.bans.count(), 2)

    def test_another_members_settings_are_not_affected(self):
        apply_ban(self.admin, self.owner)
        self.client.force_login(self.viewer)
        response = self.client.get(self.settings_url)
        self.assertIsNone(response.context["active_ban"])
        self.assertContains(response, "Save changes")
        self.assertNotContains(response, "Account banned")
