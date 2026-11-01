"""Exercise the historical-model backfill against the real migration graph."""

from datetime import timedelta
from importlib import import_module
from types import SimpleNamespace

from django.db import connection
from django.db.migrations.executor import MigrationExecutor
from django.test import TransactionTestCase
from django.utils import timezone


class ContactNotificationMigrationTests(TransactionTestCase):
    """The backfill preserves timestamps and existing event notifications."""

    before = [
        ("events", "0006_eventinterest_status"),
        ("accounts", "0005_user_interests"),
    ]
    after = [
        ("events", "0008_backfill_contact_notifications"),
        ("accounts", "0005_user_interests"),
    ]

    def setUp(self):
        self.addCleanup(self.restore_latest)
        executor = MigrationExecutor(connection)
        executor.migrate(self.before)
        self.old_apps = executor.loader.project_state(self.before).apps

    def restore_latest(self):
        executor = MigrationExecutor(connection)
        executor.migrate(executor.loader.graph.leaf_nodes())

    def test_backfill_is_pending_only_idempotent_and_reversible(self):
        user_model = self.old_apps.get_model("accounts", "User")
        event_model = self.old_apps.get_model("events", "Event")
        request_model = self.old_apps.get_model("events", "ContactRequest")
        notification_model = self.old_apps.get_model("events", "Notification")
        sender = user_model.objects.create(
            username="sender",
            email="sender@example.com",
            display_name="Park Pals",
            interests=["fitness"],
        )
        recipient = user_model.objects.create(
            username="recipient", email="recipient@example.com"
        )
        event = event_model.objects.create(
            guid="migration", title="Migration event", content_hash="v" * 64
        )
        attendance = self.old_apps.get_model("events", "EventInterest").objects.create(
            event=event, user=recipient, status="want_to_attend"
        )
        original = notification_model.objects.create(
            recipient=recipient, event=event, subject="Original event update", body=""
        )
        requests = [
            request_model.objects.create(
                sender=sender,
                recipient=recipient,
                event=event,
                status=status,
                message="Hello",
            )
            for status in ("pending", "accepted", "declined")
        ]
        timestamp = timezone.now() - timedelta(days=3)
        request_model.objects.filter(pk=requests[0].pk).update(created_at=timestamp)
        executor = MigrationExecutor(connection)
        executor.migrate(self.after)
        apps = executor.loader.project_state(self.after).apps
        notification_model = apps.get_model("events", "Notification")
        self.assertEqual(
            apps.get_model("events", "EventInterest")
            .objects.get(pk=attendance.pk)
            .status,
            "want_to_attend",
        )
        self.assertEqual(
            apps.get_model("accounts", "User").objects.get(pk=sender.pk).interests,
            ["fitness"],
        )
        self.assertEqual(notification_model.objects.count(), 2)
        notification = notification_model.objects.get(contact_request_id=requests[0].pk)
        self.assertEqual(notification.recipient_id, recipient.pk)
        self.assertEqual(notification.event_id, event.pk)
        self.assertEqual(notification.created_at, timestamp)
        self.assertEqual(notification.subject, "Contact request from Park Pals")
        self.assertEqual(notification.body, "Hello")
        self.assertIsNone(notification.read_at)
        migration = import_module(
            "events.migrations.0008_backfill_contact_notifications"
        )
        editor = SimpleNamespace(connection=connection)
        migration.backfill_contact_notifications(apps, editor)
        self.assertEqual(notification_model.objects.count(), 2)
        sender.display_name = ""
        sender.save()
        # Verify the username fallback on another historical pending request.
        request_model = apps.get_model("events", "ContactRequest")
        extra = request_model.objects.create(
            sender_id=sender.pk,
            recipient_id=recipient.pk,
            event_id=event.pk,
            status="pending",
        )
        migration.backfill_contact_notifications(apps, editor)
        self.assertEqual(
            notification_model.objects.get(contact_request_id=extra.pk).subject,
            "Contact request from sender",
        )
        MigrationExecutor(connection).migrate(self.before)
        self.assertEqual(
            list(
                self.old_apps.get_model("events", "Notification").objects.values_list(
                    "pk", flat=True
                )
            ),
            [original.pk],
        )
