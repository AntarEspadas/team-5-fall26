"""Existing event reports retain their identity and fields across the upgrade."""

from django.contrib.auth import get_user_model
from django.db import connection
from django.db.migrations.executor import MigrationExecutor
from django.test import TransactionTestCase
from django.utils import timezone

from events.models import Conversation, DirectMessage, Event, EventPost, EventReply
from reports.models import Report


class ReportKindMigrationTests(TransactionTestCase):
    def restore_latest(self):
        executor = MigrationExecutor(connection)
        executor.migrate(executor.loader.graph.leaf_nodes())

    def test_existing_reports_become_event_reports_without_losing_history(self):
        self.addCleanup(self.restore_latest)
        before = [("reports", "0001_initial")]
        after = [("reports", "0002_comment_and_chat_reports")]
        executor = MigrationExecutor(connection)
        executor.migrate(before)
        old_report = executor.loader.project_state(before).apps.get_model(
            "reports", "Report"
        )
        user = get_user_model().objects.create_user(
            "migration-reporter", "migration@example.com"
        )
        event = Event.objects.create(
            guid="report-migration", title="Original event", content_hash=""
        )
        existing = old_report.objects.create(
            reporter_id=user.pk,
            event_id=event.pk,
            event_title=event.title,
            reason="spam_scam",
            details="Original details",
        )
        deleted_source = old_report.objects.create(
            event_title="Deleted event",
            reason="other",
            details="Resolved history",
            resolved_at=timezone.now(),
        )
        executor = MigrationExecutor(connection)
        executor.migrate(after)
        upgraded = Report.objects.get(pk=existing.pk)
        self.assertEqual(upgraded.kind, Report.Kind.EVENT)
        self.assertEqual(upgraded.event_id, event.pk)
        self.assertEqual(upgraded.reporter_id, user.pk)
        self.assertEqual(upgraded.event_title, existing.event_title)
        self.assertEqual(upgraded.reason, existing.reason)
        self.assertEqual(upgraded.details, existing.details)
        self.assertEqual(upgraded.created_at, existing.created_at)
        self.assertIsNone(upgraded.reported_user_id)
        self.assertIsNone(upgraded.context_message_id)
        historical = Report.objects.get(pk=deleted_source.pk)
        self.assertEqual(historical.kind, Report.Kind.EVENT)
        self.assertEqual(historical.event_title, "Deleted event")
        self.assertEqual(historical.resolved_at, deleted_source.resolved_at)
        self.assertIsNone(historical.event_id)

    def test_profile_migration_preserves_existing_report_kinds_and_context(self):
        self.addCleanup(self.restore_latest)
        before = [("reports", "0002_comment_and_chat_reports")]
        after = [("reports", "0003_profile_reports")]
        executor = MigrationExecutor(connection)
        executor.migrate(before)
        old_report = executor.loader.project_state(before).apps.get_model(
            "reports", "Report"
        )
        User = get_user_model()
        reporter = User.objects.create_user(
            "migration-viewer", "migration-viewer@example.com"
        )
        subject = User.objects.create_user(
            "migration-subject", "migration-subject@example.com"
        )
        event = Event.objects.create(
            guid="profile-migration", title="Shared event", content_hash=""
        )
        post = EventPost.objects.create(event=event, author=subject, body="Comment")
        reply = EventReply.objects.create(post=post, author=subject, body="Reply")
        thread, _ = Conversation.get_or_create_for_users(reporter, subject)
        message = DirectMessage.objects.create(
            conversation=thread, sender=subject, body="Chat message"
        )
        for kind, fields in (
            ("event", {"event_id": event.pk, "event_title": event.title}),
            (
                "post",
                {
                    "event_id": event.pk,
                    "reported_post_id": post.pk,
                    "reported_user_id": subject.pk,
                },
            ),
            (
                "reply",
                {
                    "event_id": event.pk,
                    "reported_reply_id": reply.pk,
                    "reported_user_id": subject.pk,
                },
            ),
            (
                "user",
                {
                    "reported_user_id": subject.pk,
                    "conversation_id": thread.pk,
                    "context_message_id": message.pk,
                },
            ),
        ):
            old_report.objects.create(
                kind=kind,
                reporter_id=reporter.pk,
                reason="other",
                details="Existing concern",
                **fields
            )
        original = list(old_report.objects.order_by("pk").values())
        MigrationExecutor(connection).migrate(after)
        self.assertEqual(list(Report.objects.order_by("pk").values()), original)
        profile_report = Report.objects.create(
            kind=Report.Kind.PROFILE,
            reporter=reporter,
            reported_user=subject,
            reason=Report.Reason.OTHER,
        )
        self.assertEqual(profile_report.reported_user, subject)
        self.assertIsNone(profile_report.conversation_id)
        self.assertEqual(Report.objects.count(), 5)
