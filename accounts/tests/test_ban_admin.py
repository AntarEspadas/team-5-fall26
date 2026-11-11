"""Confirmed user-admin moderation, permissions, immutable history, and audit."""

from datetime import timedelta
from unittest.mock import patch

from django.contrib import admin
from django.contrib.admin.models import CHANGE, LogEntry
from django.contrib.auth.models import Permission
from django.test import Client, TestCase
from django.urls import NoReverseMatch, reverse
from django.utils import timezone

from accounts.ban_services import apply_ban, get_active_ban, lift_ban
from accounts.models import User, UserBan


class BanAdminTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.admin = User.objects.create_superuser("admin", "admin@example.com")
        cls.manager = User.objects.create_user(
            "manager", "manager@example.com", is_staff=True
        )
        cls.manager.user_permissions.add(
            Permission.objects.get(
                codename="change_user", content_type__app_label="accounts"
            )
        )
        cls.viewer = User.objects.create_user(
            "viewer", "viewer@example.com", is_staff=True
        )
        cls.viewer.user_permissions.add(
            Permission.objects.get(
                codename="view_user", content_type__app_label="accounts"
            )
        )
        cls.user = User.objects.create_user(
            "member", "member@example.com", display_name="Community Member"
        )
        cls.other = User.objects.create_user("other", "other@example.com")
        cls.ban_url = reverse("admin:accounts_user_ban", args=[cls.user.pk])
        cls.lift_url = reverse("admin:accounts_user_lift_ban", args=[cls.user.pk])
        cls.change_url = reverse("admin:accounts_user_change", args=[cls.user.pk])

    def setUp(self):
        self.client.force_login(self.admin)

    def preview(self, url=None, client=None):
        response = (client or self.client).get(url or self.ban_url)
        self.assertEqual(response.status_code, 200)
        self.assertTemplateUsed(response, "admin/accounts/user/ban_confirmation.html")
        return response

    def confirm(self, url=None, token=None, client=None, **data):
        client = client or self.client
        url = url or self.ban_url
        if token is None:
            token = self.preview(url, client).context["confirmation_token"]
        return client.post(url, {"confirmation_token": token, **data})

    def assert_stale(self, response):
        self.assertContains(response, "confirmation is out of date", status_code=409)
        self.assertNotContains(response, 'name="confirmation_token"', status_code=409)

    def test_first_ban_preview_is_read_only_and_shows_duration(self):
        response = self.preview()
        self.assertContains(response, "Community Member")
        self.assertContains(response, "offense 1")
        self.assertContains(response, "120 hours (5 days)")
        self.assertContains(response, 'name="csrfmiddlewaretoken"')
        self.assertContains(response, 'name="confirmation_token"')
        self.assertEqual(
            response["Cache-Control"],
            "max-age=0, no-cache, no-store, must-revalidate, private",
        )
        self.assertFalse(UserBan.objects.exists())
        self.assertFalse(LogEntry.objects.exists())

    def test_first_ban_confirm_ignores_submitted_duration_and_records_audit(self):
        response = self.confirm(
            expires_at="",
            offense_number="99",
            duration="indefinite",
            issued_by=self.other.pk,
        )
        self.assertRedirects(response, self.change_url)
        ban = self.user.bans.get()
        self.assertEqual(ban.offense_number, 1)
        self.assertEqual(ban.expires_at - ban.started_at, timedelta(hours=120))
        self.assertEqual(ban.issued_by, self.admin)
        entry = LogEntry.objects.get()
        self.assertEqual(entry.user, self.admin)
        self.assertEqual(entry.object_id, str(self.user.pk))
        self.assertEqual(entry.content_type.model, "user")
        self.assertEqual(entry.action_flag, CHANGE)
        self.assertEqual(entry.change_message, "Applied ban for offense 1.")

    def test_change_user_staff_can_apply_and_lift_without_ban_model_permissions(self):
        self.client.force_login(self.manager)
        self.assertFalse(self.manager.has_perm("accounts.add_userban"))
        self.assertEqual(self.confirm().status_code, 302)
        ban = self.user.bans.get()
        self.assertEqual(ban.issued_by, self.manager)
        response = self.preview(self.lift_url)
        self.assertContains(response, "offense 1")
        self.assertContains(response, "history will remain")
        self.assertEqual(
            self.confirm(
                self.lift_url, response.context["confirmation_token"]
            ).status_code,
            302,
        )
        ban.refresh_from_db()
        self.assertEqual(ban.lifted_by, self.manager)
        self.assertIsNotNone(ban.lifted_at)
        self.assertIsNone(get_active_ban(self.user))
        self.assertEqual(LogEntry.objects.count(), 2)
        self.assertEqual(
            LogEntry.objects.first().change_message, "Lifted ban for offense 1."
        )

    def test_later_bans_are_indefinite_after_expiry_or_lifting(self):
        first, _ = apply_ban(self.admin, self.user)
        UserBan.objects.filter(pk=first.pk).update(expires_at=timezone.now())
        for offense in (2, 3):
            with self.subTest(offense=offense):
                response = self.preview()
                self.assertContains(response, f"offense {offense}")
                self.assertContains(response, "last indefinitely")
                self.assertNotContains(response, "120 hours")
                self.assertEqual(
                    self.confirm(
                        token=response.context["confirmation_token"]
                    ).status_code,
                    302,
                )
                ban = get_active_ban(self.user)
                self.assertEqual(ban.offense_number, offense)
                self.assertIsNone(ban.expires_at)
                self.assertEqual(self.confirm(self.lift_url).status_code, 302)
        self.assertEqual(self.user.bans.count(), 3)

    def test_controls_follow_active_status_expiry_and_lifting(self):
        response = self.client.get(self.change_url)
        self.assertContains(response, "Not banned")
        self.assertEqual(response.context["ban_url"], self.ban_url)
        self.assertIsNone(response.context["lift_ban_url"])
        self.assertContains(response, 'disabled aria-disabled="true">Lift ban')
        ban, _ = apply_ban(self.admin, self.user)
        response = self.client.get(self.change_url)
        self.assertContains(response, "Banned until")
        self.assertIsNone(response.context["ban_url"])
        self.assertEqual(response.context["lift_ban_url"], self.lift_url)
        UserBan.objects.filter(pk=ban.pk).update(expires_at=timezone.now())
        response = self.client.get(self.change_url)
        self.assertContains(response, "Not banned")
        self.assertEqual(response.context["ban_url"], self.ban_url)
        ban, _ = apply_ban(self.admin, self.user)
        self.assertContains(self.client.get(self.change_url), "Banned indefinitely")
        lift_ban(self.admin, ban)
        self.assertContains(self.client.get(self.change_url), "Not banned")

    def test_unavailable_previews_offer_no_confirmation(self):
        response = self.preview(self.lift_url)
        self.assertContains(response, "no active ban to lift")
        self.assertIsNone(response.context["confirmation_token"])
        self.assertNotContains(response, 'name="confirmation_token"')
        apply_ban(self.admin, self.user)
        response = self.preview()
        self.assertContains(response, "already has an active ban")
        self.assertIsNone(response.context["confirmation_token"])
        self.assertNotContains(response, 'name="confirmation_token"')
        self.assertFalse(LogEntry.objects.exists())

    def test_duplicate_ban_confirmation_cannot_add_an_offense(self):
        token = self.preview().context["confirmation_token"]
        self.assertEqual(self.confirm(token=token).status_code, 302)
        self.assert_stale(self.confirm(token=token))
        self.assertEqual(self.user.bans.count(), 1)
        self.assertEqual(LogEntry.objects.count(), 1)

    def test_old_ban_confirmation_cannot_reban_after_a_ban_was_lifted(self):
        token = self.preview().context["confirmation_token"]
        ban, _ = apply_ban(self.manager, self.user)
        lift_ban(self.manager, ban)
        self.assert_stale(self.confirm(token=token))
        self.assertEqual(self.user.bans.count(), 1)
        self.assertIsNone(get_active_ban(self.user))
        self.assertFalse(LogEntry.objects.exists())

    def test_stale_lift_cannot_lift_a_later_ban(self):
        first, _ = apply_ban(self.admin, self.user)
        token = self.preview(self.lift_url).context["confirmation_token"]
        lift_ban(self.manager, first)
        second, _ = apply_ban(self.manager, self.user)
        self.assert_stale(self.confirm(self.lift_url, token))
        self.assertEqual(get_active_ban(self.user), second)
        self.assertFalse(LogEntry.objects.exists())

    def test_duplicate_and_expired_lift_confirmations_are_rejected(self):
        ban, _ = apply_ban(self.admin, self.user)
        token = self.preview(self.lift_url).context["confirmation_token"]
        UserBan.objects.filter(pk=ban.pk).update(expires_at=timezone.now())
        self.assert_stale(self.confirm(self.lift_url, token))
        self.assertFalse(LogEntry.objects.exists())
        apply_ban(self.admin, self.user)
        token = self.preview(self.lift_url).context["confirmation_token"]
        self.assertEqual(self.confirm(self.lift_url, token).status_code, 302)
        self.assert_stale(self.confirm(self.lift_url, token))
        self.assertEqual(LogEntry.objects.count(), 1)

    def test_expiration_during_lift_does_not_log_a_success(self):
        ban, _ = apply_ban(self.admin, self.user)
        token = self.preview(self.lift_url).context["confirmation_token"]
        # The deadline passes between preview validation and the service check.
        with patch("accounts.admin.lift_ban", return_value=(ban, False)):
            self.assert_stale(self.confirm(self.lift_url, token))
        self.assertFalse(LogEntry.objects.exists())

    def test_missing_tampered_or_wrong_target_action_actor_tokens_do_not_write(self):
        token = self.preview().context["confirmation_token"]
        for bad_token in ("", "forged", token + "tampered"):
            with self.subTest(token=bad_token):
                self.assertEqual(self.confirm(token=bad_token).status_code, 400)
        other_url = reverse("admin:accounts_user_ban", args=[self.other.pk])
        self.assert_stale(self.confirm(other_url, token))
        self.assert_stale(self.confirm(self.lift_url, token))
        self.client.force_login(self.manager)
        self.assert_stale(self.confirm(token=token))
        self.assertFalse(UserBan.objects.exists())
        self.assertFalse(LogEntry.objects.exists())

    def test_get_and_post_require_staff_with_change_user_permission(self):
        token = self.preview().context["confirmation_token"]
        no_permissions = User.objects.create_user(
            "staff", "staff@example.com", is_staff=True
        )
        for actor in (self.viewer, no_permissions):
            self.client.force_login(actor)
            for url in (self.ban_url, self.lift_url):
                with self.subTest(actor=actor.pk, url=url):
                    self.assertEqual(self.client.get(url).status_code, 403)
                    self.assertEqual(
                        self.client.post(
                            url, {"confirmation_token": token}
                        ).status_code,
                        403,
                    )
        self.user.user_permissions.add(
            Permission.objects.get(
                codename="change_user", content_type__app_label="accounts"
            )
        )
        for actor in (self.user, None):
            if actor is None:
                self.client.logout()
            else:
                self.client.force_login(actor)
            for url in (self.ban_url, self.lift_url):
                self.assertEqual(self.client.get(url).status_code, 302)
                self.assertEqual(
                    self.client.post(url, {"confirmation_token": token}).status_code,
                    302,
                )
        self.assertFalse(UserBan.objects.exists())

    def test_view_only_admin_sees_history_without_action_links(self):
        apply_ban(self.admin, self.user)
        self.client.force_login(self.viewer)
        response = self.client.get(self.change_url)
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Ban history")
        self.assertContains(response, "Banned until")
        self.assertIsNone(response.context["ban_url"])
        self.assertIsNone(response.context["lift_ban_url"])
        self.assertNotContains(response, f'href="{self.lift_url}"')

    def test_staff_superuser_and_self_targets_have_disabled_controls(self):
        root = User.objects.create_user("root", "root@example.com", is_superuser=True)
        for target in (self.manager, self.admin, root):
            with self.subTest(target=target.pk):
                response = self.client.get(
                    reverse("admin:accounts_user_change", args=[target.pk])
                )
                self.assertIsNone(response.context["ban_url"])
                self.assertIsNone(response.context["lift_ban_url"])
                for route in ("ban", "lift_ban"):
                    url = reverse(f"admin:accounts_user_{route}", args=[target.pk])
                    self.assertEqual(self.client.get(url).status_code, 403)
                    self.assertEqual(self.client.post(url).status_code, 403)
        self.assertFalse(UserBan.objects.exists())

    def test_promoted_target_is_rechecked_on_ban_and_lift_submission(self):
        ban_token = self.preview().context["confirmation_token"]
        ban, _ = apply_ban(self.admin, self.user)
        lift_token = self.preview(self.lift_url).context["confirmation_token"]
        User.objects.filter(pk=self.user.pk).update(is_staff=True)
        self.assertEqual(self.confirm(token=ban_token).status_code, 403)
        self.assertEqual(self.confirm(self.lift_url, lift_token).status_code, 403)
        self.assertEqual(get_active_ban(self.user), ban)
        self.assertFalse(LogEntry.objects.exists())

    def test_revoked_admin_permissions_staff_status_and_activity_are_rechecked(self):
        self.client.force_login(self.manager)
        token = self.preview().context["confirmation_token"]
        self.manager.user_permissions.clear()
        self.assertEqual(self.confirm(token=token).status_code, 403)
        self.manager.user_permissions.add(
            Permission.objects.get(
                codename="change_user", content_type__app_label="accounts"
            )
        )
        for fields in ({"is_staff": False}, {"is_staff": True, "is_active": False}):
            with self.subTest(fields=fields):
                User.objects.filter(pk=self.manager.pk).update(**fields)
                self.assertEqual(self.confirm(token=token).status_code, 302)
        self.assertFalse(UserBan.objects.exists())
        self.assertFalse(LogEntry.objects.exists())

    def test_csrf_is_required_for_ban_and_lift(self):
        client = Client(enforce_csrf_checks=True)
        client.force_login(self.admin)
        for url in (self.ban_url, self.lift_url):
            with self.subTest(url=url):
                token = self.preview(url, client).context["confirmation_token"]
                self.assertEqual(
                    client.post(url, {"confirmation_token": token}).status_code, 403
                )
                response = client.post(
                    url,
                    {
                        "confirmation_token": token,
                        "csrfmiddlewaretoken": client.cookies["csrftoken"].value,
                    },
                )
                self.assertEqual(response.status_code, 302)
        self.assertEqual(self.user.bans.count(), 1)
        self.assertIsNone(get_active_ban(self.user))
        self.assertEqual(LogEntry.objects.count(), 2)

    def test_unknown_users_and_unsupported_methods(self):
        for route in ("ban", "lift_ban"):
            url = reverse(f"admin:accounts_user_{route}", args=[999999])
            self.assertEqual(self.client.get(url).status_code, 404)
            self.assertEqual(self.client.post(url).status_code, 404)
        for method in ("put", "patch", "delete"):
            self.assertEqual(
                getattr(self.client, method)(self.ban_url).status_code, 405
            )
        self.assertFalse(UserBan.objects.exists())

    def test_history_has_no_standalone_admin_and_only_readonly_inline_fields(self):
        ban, _ = apply_ban(self.admin, self.user)
        self.assertNotIn(UserBan, admin.site._registry)
        with self.assertRaises(NoReverseMatch):
            reverse("admin:accounts_userban_change", args=[ban.pk])
        self.client.force_login(self.manager)
        response = self.client.get(self.change_url)
        self.assertContains(response, "Ban history")
        inline = response.context["inline_admin_formsets"][0]
        self.assertFalse(inline.has_add_permission)
        self.assertFalse(inline.has_change_permission)
        self.assertFalse(inline.has_delete_permission)
        self.assertTrue(inline.has_view_permission)
        for field in (
            "offense_number",
            "started_at",
            "expires_at",
            "issued_by",
            "lifted_at",
            "lifted_by",
        ):
            self.assertNotContains(response, f'name="bans-0-{field}"')
        self.assertNotContains(response, 'name="bans-0-DELETE"')

    def test_forged_inline_edits_deletes_and_additions_do_not_change_history(self):
        ban, _ = apply_ban(self.admin, self.user)
        before = list(UserBan.objects.values())
        joined = timezone.localtime(self.user.date_joined)
        response = self.client.post(
            self.change_url,
            {
                "username": self.user.username,
                "email": self.user.email,
                "display_name": self.user.display_name,
                "is_active": "on",
                "date_joined_0": joined.strftime("%Y-%m-%d"),
                "date_joined_1": joined.strftime("%H:%M:%S"),
                "bans-TOTAL_FORMS": "2",
                "bans-INITIAL_FORMS": "1",
                "bans-MIN_NUM_FORMS": "0",
                "bans-MAX_NUM_FORMS": "1000",
                "bans-0-id": ban.pk,
                "bans-0-user": self.user.pk,
                "bans-0-offense_number": "50",
                "bans-0-expires_at": "",
                "bans-0-lifted_by": self.admin.pk,
                "bans-0-DELETE": "on",
                "bans-1-user": self.user.pk,
                "bans-1-offense_number": "51",
                "bans-1-issued_by": self.admin.pk,
                "bans-1-started_at": "2026-01-01 00:00:00",
                "_continue": "Save and continue editing",
            },
        )
        self.assertEqual(response.status_code, 302)
        self.assertEqual(list(UserBan.objects.values()), before)

    def test_audit_failure_rolls_back_ban(self):
        token = self.preview().context["confirmation_token"]
        with patch(
            "accounts.admin.UserAdmin.log_change",
            side_effect=RuntimeError("audit unavailable"),
        ):
            with self.assertRaisesMessage(RuntimeError, "audit unavailable"):
                self.confirm(token=token)
        self.assertFalse(UserBan.objects.exists())
        self.assertFalse(LogEntry.objects.exists())
