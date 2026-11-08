"""Ban history, admin authorization, expiration, and concurrent moderation."""

from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from threading import Barrier
from unittest.mock import patch

from django.contrib.auth.models import AnonymousUser, Permission
from django.core.exceptions import PermissionDenied
from django.db import IntegrityError, connections, transaction
from django.test import TestCase, TransactionTestCase, skipUnlessDBFeature
from django.utils import timezone

from accounts.ban_services import apply_ban, get_active_ban, lift_ban
from accounts.models import User, UserBan


class BanTestData:
    def setUp(self):
        super().setUp()
        self.admin = User.objects.create_superuser("admin", "admin@example.com")
        self.manager = User.objects.create_user(
            "manager", "manager@example.com", is_staff=True
        )
        self.manager.user_permissions.add(
            Permission.objects.get(
                content_type__app_label="accounts", codename="change_user"
            )
        )
        self.user = User.objects.create_user("member", "member@example.com")
        self.now = timezone.now()
        self.clock = self.enterContext(
            patch("accounts.ban_services.timezone.now", return_value=self.now)
        )


class BanLifecycleTests(BanTestData, TestCase):
    def test_existing_user_has_no_ban(self):
        self.assertIsNone(get_active_ban(self.user))
        self.assertFalse(self.user.bans.exists())

    def test_anonymous_and_unsaved_users_have_no_ban(self):
        for user in (AnonymousUser(), User(username="unsaved")):
            with self.subTest(user=user):
                self.assertIsNone(get_active_ban(user))

    def test_first_ban_lasts_exactly_120_hours_and_keeps_login_active(self):
        ban, created = apply_ban(self.manager, self.user)

        self.assertTrue(created)
        self.assertEqual(ban.user, self.user)
        self.assertEqual(ban.issued_by, self.manager)
        self.assertEqual(ban.offense_number, 1)
        self.assertEqual(ban.started_at, self.now)
        self.assertEqual(ban.expires_at, self.now + timedelta(hours=120))
        self.assertIsNone(ban.lifted_at)
        self.assertIsNone(ban.lifted_by)
        self.assertTrue(ban.is_active)
        self.assertEqual(get_active_ban(self.user), ban)
        self.user.refresh_from_db()
        self.assertTrue(self.user.is_active)
        self.assertEqual(str(ban), f"Ban 1 for user {self.user.pk}")

    def test_active_ban_submission_does_not_add_offense_or_extend_duration(self):
        original, _ = apply_ban(self.admin, self.user)
        self.clock.return_value += timedelta(days=1)

        ban, created = apply_ban(self.manager, self.user)

        self.assertFalse(created)
        self.assertEqual(ban.pk, original.pk)
        self.assertEqual(ban.started_at, original.started_at)
        self.assertEqual(ban.expires_at, original.expires_at)
        self.assertEqual(ban.issued_by, self.admin)
        self.assertEqual(self.user.bans.count(), 1)

    def test_temporary_ban_expires_at_exact_boundary_without_changing_history(self):
        ban, _ = apply_ban(self.admin, self.user)
        before_expiry = ban.expires_at - timedelta(microseconds=1)
        self.assertEqual(get_active_ban(self.user, at=before_expiry), ban)

        for at in (ban.expires_at, ban.expires_at + timedelta(days=1)):
            with self.subTest(at=at):
                self.clock.return_value = at
                self.assertIsNone(get_active_ban(self.user))
                self.assertFalse(ban.is_active)
        ban.refresh_from_db()
        self.assertEqual(ban.offense_number, 1)
        self.assertEqual(ban.expires_at, self.now + timedelta(hours=120))
        self.assertIsNone(ban.lifted_at)

    def test_second_offense_after_expiry_is_indefinite(self):
        first, _ = apply_ban(self.admin, self.user)
        self.clock.return_value = first.expires_at

        second, created = apply_ban(self.manager, self.user)

        self.assertTrue(created)
        self.assertEqual(second.offense_number, 2)
        self.assertIsNone(second.expires_at)
        self.assertEqual(second.started_at, first.expires_at)
        self.clock.return_value += timedelta(days=3650)
        self.assertTrue(second.is_active)
        self.assertEqual(get_active_ban(self.user), second)
        self.assertEqual(self.user.bans.count(), 2)

    def test_lifting_records_admin_without_rewriting_original_offense(self):
        original, _ = apply_ban(self.admin, self.user)
        self.clock.return_value += timedelta(hours=1)

        lifted, changed = lift_ban(self.manager, original)

        self.assertTrue(changed)
        self.assertEqual(lifted.lifted_at, self.clock.return_value)
        self.assertEqual(lifted.lifted_by, self.manager)
        self.assertEqual(lifted.issued_by, self.admin)
        self.assertEqual(lifted.started_at, original.started_at)
        self.assertEqual(lifted.expires_at, original.expires_at)
        self.assertEqual(lifted.offense_number, 1)
        self.assertFalse(lifted.is_active)
        self.assertIsNone(get_active_ban(self.user))
        self.assertEqual(self.user.bans.count(), 1)

    def test_lifting_does_not_reset_escalation_even_after_indefinite_ban(self):
        for offense in range(1, 4):
            with self.subTest(offense=offense):
                ban, created = apply_ban(self.admin, self.user)
                self.assertTrue(created)
                self.assertEqual(ban.offense_number, offense)
                if offense > 1:
                    self.assertIsNone(ban.expires_at)
                _, changed = lift_ban(self.manager, ban)
                self.assertTrue(changed)
        self.assertEqual(self.user.bans.count(), 3)

    def test_repeated_lift_retains_first_lifting_admin_and_timestamp(self):
        original, _ = apply_ban(self.admin, self.user)
        lifted, _ = lift_ban(self.manager, original)
        self.clock.return_value += timedelta(hours=1)

        repeated, changed = lift_ban(self.admin, original)

        self.assertFalse(changed)
        self.assertEqual(repeated.lifted_at, lifted.lifted_at)
        self.assertEqual(repeated.lifted_by, self.manager)

    def test_expired_ban_is_not_marked_as_lifted(self):
        original, _ = apply_ban(self.admin, self.user)
        self.clock.return_value = original.expires_at

        ban, changed = lift_ban(self.manager, original)

        self.assertFalse(changed)
        self.assertIsNone(ban.lifted_at)
        self.assertIsNone(ban.lifted_by)

    def test_stale_lift_cannot_lift_a_later_offense(self):
        first, _ = apply_ban(self.admin, self.user)
        lift_ban(self.admin, first)
        second, _ = apply_ban(self.admin, self.user)

        _, changed = lift_ban(self.manager, first)

        self.assertFalse(changed)
        self.assertEqual(get_active_ban(self.user), second)

    def test_lookup_reads_database_instead_of_stale_ban_instance(self):
        original, _ = apply_ban(self.admin, self.user)
        self.assertTrue(original.is_active)
        lift_ban(self.manager, original)
        self.assertIsNone(get_active_ban(self.user))

    def test_deleting_issuing_and_lifting_admins_preserves_history(self):
        ban, _ = apply_ban(self.admin, self.user)
        lift_ban(self.manager, ban)

        self.admin.delete()
        self.manager.delete()

        ban.refresh_from_db()
        self.assertIsNone(ban.issued_by)
        self.assertIsNone(ban.lifted_by)
        self.assertEqual(ban.user, self.user)
        self.assertEqual(ban.started_at, self.now)
        self.assertEqual(ban.expires_at, self.now + timedelta(hours=120))
        self.assertEqual(ban.lifted_at, self.now)
        self.assertEqual(ban.offense_number, 1)

    def test_database_rejects_duplicate_offense_for_same_user(self):
        apply_ban(self.admin, self.user)
        with self.assertRaises(IntegrityError), transaction.atomic():
            UserBan.objects.create(
                user=self.user, offense_number=1, issued_by=self.admin
            )

    def test_database_rejects_nonpositive_offense_numbers(self):
        for number in (0, -1):
            with self.subTest(number=number):
                with self.assertRaises(IntegrityError), transaction.atomic():
                    UserBan.objects.create(
                        user=self.user, offense_number=number, issued_by=self.admin
                    )

    def test_users_have_independent_offense_histories(self):
        other = User.objects.create_user("other", "other@example.com")
        apply_ban(self.admin, self.user)

        ban, _ = apply_ban(self.manager, other)

        self.assertEqual(ban.offense_number, 1)
        self.assertEqual(ban.expires_at, self.now + timedelta(hours=120))


class BanAuthorizationTests(BanTestData, TestCase):
    def test_unauthorized_actors_cannot_apply_or_lift_bans(self):
        ban, _ = apply_ban(self.admin, self.user)
        staff = User.objects.create_user("staff", "staff@example.com", is_staff=True)
        inactive = User.objects.create_superuser(
            "inactive", "inactive@example.com", is_active=False
        )
        self.user.user_permissions.add(
            Permission.objects.get(
                content_type__app_label="accounts", codename="change_user"
            )
        )
        for actor in (AnonymousUser(), User(), self.user, staff, inactive):
            with self.subTest(actor=actor):
                with self.assertRaises(PermissionDenied):
                    apply_ban(actor, self.user)
                with self.assertRaises(PermissionDenied):
                    lift_ban(actor, ban)
        self.assertEqual(self.user.bans.count(), 1)
        self.assertEqual(get_active_ban(self.user), ban)

    def test_cached_permission_cannot_authorize_after_revocation(self):
        self.assertTrue(self.manager.has_perm("accounts.change_user"))
        ban, _ = apply_ban(self.admin, self.user)
        self.manager.user_permissions.clear()

        with self.assertRaises(PermissionDenied):
            apply_ban(self.manager, self.user)
        with self.assertRaises(PermissionDenied):
            lift_ban(self.manager, ban)

    def test_stale_actor_cannot_authorize_after_deactivation_or_staff_removal(self):
        ban, _ = apply_ban(self.admin, self.user)
        for fields in ({"is_active": False}, {"is_active": True, "is_staff": False}):
            with self.subTest(fields=fields):
                User.objects.filter(pk=self.manager.pk).update(**fields)
                with self.assertRaises(PermissionDenied):
                    apply_ban(self.manager, self.user)
                with self.assertRaises(PermissionDenied):
                    lift_ban(self.manager, ban)

    def test_staff_superusers_and_self_cannot_be_banned(self):
        root = User.objects.create_user(
            "root", "root@example.com", is_superuser=True, is_staff=False
        )
        for target in (self.manager, self.admin, root):
            with self.subTest(target=target):
                with self.assertRaises(PermissionDenied):
                    apply_ban(self.admin, target)
        self.assertFalse(UserBan.objects.exists())

    def test_target_eligibility_is_reloaded_before_applying_or_lifting(self):
        ban, _ = apply_ban(self.admin, self.user)
        User.objects.filter(pk=self.user.pk).update(is_staff=True)

        with self.assertRaises(PermissionDenied):
            apply_ban(self.admin, self.user)
        with self.assertRaises(PermissionDenied):
            lift_ban(self.admin, ban)
        self.assertEqual(get_active_ban(self.user), ban)


@skipUnlessDBFeature("has_select_for_update")
class ConcurrentBanTests(BanTestData, TransactionTestCase):
    """Use independent database connections to exercise real row locking."""

    def run_concurrently(self, operation):
        ready = Barrier(2, timeout=15)

        def run(actor):
            try:
                ready.wait()
                ban, changed = operation(actor)
                return ban.pk, changed
            finally:
                connections.close_all()

        with ThreadPoolExecutor(max_workers=2) as pool:
            futures = [pool.submit(run, actor) for actor in (self.admin, self.manager)]
            return [future.result(timeout=30) for future in futures]

    def test_concurrent_first_bans_create_one_temporary_offense(self):
        results = self.run_concurrently(lambda actor: apply_ban(actor, self.user))

        ban = self.user.bans.get()
        self.assertEqual([pk for pk, _ in results], [ban.pk, ban.pk])
        self.assertEqual(sorted(changed for _, changed in results), [False, True])
        self.assertEqual(ban.offense_number, 1)
        self.assertEqual(ban.expires_at, self.now + timedelta(hours=120))

    def test_concurrent_bans_after_expiry_create_one_indefinite_offense(self):
        first, _ = apply_ban(self.admin, self.user)
        self.clock.return_value = first.expires_at

        results = self.run_concurrently(lambda actor: apply_ban(actor, self.user))

        ban = get_active_ban(self.user)
        self.assertEqual([pk for pk, _ in results], [ban.pk, ban.pk])
        self.assertEqual(sorted(changed for _, changed in results), [False, True])
        self.assertEqual(self.user.bans.count(), 2)
        self.assertEqual(ban.offense_number, 2)
        self.assertIsNone(ban.expires_at)

    def test_concurrent_lifts_record_only_one_admin(self):
        ban, _ = apply_ban(self.admin, self.user)

        results = self.run_concurrently(lambda actor: lift_ban(actor, ban))

        ban.refresh_from_db()
        winner = [self.admin, self.manager][
            [changed for _, changed in results].index(True)
        ]
        self.assertEqual(sorted(changed for _, changed in results), [False, True])
        self.assertEqual(ban.lifted_by, winner)
        self.assertEqual(ban.lifted_at, self.now)
        self.assertIsNone(get_active_ban(self.user))

    def test_concurrent_apply_and_lift_leave_a_consistent_history(self):
        first, _ = apply_ban(self.admin, self.user)

        def operation(actor):
            if actor.pk == self.admin.pk:
                return apply_ban(actor, self.user)
            return lift_ban(actor, first)

        self.run_concurrently(operation)

        first.refresh_from_db()
        self.assertEqual(first.lifted_by, self.manager)
        self.assertEqual(first.lifted_at, self.now)
        if self.user.bans.count() == 2:
            active = get_active_ban(self.user)
            self.assertEqual(active.offense_number, 2)
            self.assertIsNone(active.expires_at)
        else:
            self.assertEqual(self.user.bans.count(), 1)
            self.assertIsNone(get_active_ban(self.user))
