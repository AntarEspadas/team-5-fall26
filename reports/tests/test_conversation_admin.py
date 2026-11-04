"""Private conversation review is read-only and gated by open chat reports."""

from urllib.parse import parse_qs, urlsplit

from django.contrib import admin
from django.contrib.auth.models import Permission
from django.db import connection
from django.test import TestCase
from django.test.utils import CaptureQueriesContext
from django.urls import reverse
from django.utils import timezone

from accounts.models import User
from events.models import Conversation, DirectMessage
from reports.models import Report


class ConversationReviewTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.moderator = User.objects.create_superuser("moderator", "mod@example.com")
        cls.viewer = User.objects.create_user(
            "viewer", "viewer@example.com", is_staff=True
        )
        cls.editor = User.objects.create_user(
            "editor", "editor@example.com", is_staff=True
        )
        cls.staff = User.objects.create_user(
            "staff", "staff@example.com", is_staff=True
        )
        cls.viewer.user_permissions.add(Permission.objects.get(codename="view_report"))
        cls.editor.user_permissions.add(
            Permission.objects.get(codename="change_report")
        )
        cls.first = User.objects.create_user("first", "first@example.com")
        cls.second = User.objects.create_user("second", "second@example.com")
        cls.third = User.objects.create_user("third", "third@example.com")
        cls.thread, _ = Conversation.get_or_create_for_users(cls.first, cls.second)
        cls.other_thread, _ = Conversation.get_or_create_for_users(
            cls.third, cls.second
        )
        cls.message = DirectMessage.objects.create(
            conversation=cls.thread, sender=cls.second, body="Original context"
        )
        cls.other_message = DirectMessage.objects.create(
            conversation=cls.other_thread, sender=cls.second, body="UNREPORTED SECRET"
        )
        cls.report = Report.objects.create(
            kind=Report.Kind.USER,
            reporter=cls.first,
            reported_user=cls.second,
            conversation=cls.thread,
            context_message=cls.message,
            reason=Report.Reason.HARASSMENT_HATE,
            details="Please review this conversation.",
        )
        cls.url = reverse("admin:reports_report_conversation", args=[cls.report.pk])
        cls.feed_url = reverse("admin:reports_report_changelist")

    def setUp(self):
        self.client.force_login(self.moderator)

    def extra_report(self, **fields):
        defaults = {
            "kind": Report.Kind.USER,
            "reporter": self.second,
            "reported_user": self.first,
            "conversation": self.thread,
            "reason": Report.Reason.OTHER,
        }
        defaults.update(fields)
        return Report.objects.create(**defaults)

    def test_permission_matrix_and_read_only_methods(self):
        for user in (self.moderator, self.viewer, self.editor):
            self.client.force_login(user)
            self.assertContains(
                self.client.get(self.url, follow=True), "Original context"
            )
            self.assertEqual(self.client.post(self.url).status_code, 405)
        self.client.force_login(self.staff)
        self.assertEqual(self.client.get(self.url).status_code, 403)
        for user in (self.first, self.second):
            self.client.force_login(user)
            self.assertEqual(self.client.get(self.url).status_code, 302)
        self.moderator.is_active = False
        self.moderator.save()
        self.client.force_login(self.moderator)
        self.assertEqual(self.client.get(self.url).status_code, 302)
        self.client.logout()
        self.assertEqual(self.client.get(self.url).status_code, 302)

    def test_access_requires_open_chat_report_for_exact_conversation(self):
        other = self.extra_report(
            reporter=self.third,
            reported_user=self.second,
            conversation=self.other_thread,
            resolved_at=timezone.now(),
        )
        other_url = reverse("admin:reports_report_conversation", args=[other.pk])
        # Reporting the same account elsewhere does not unlock this thread.
        self.assertEqual(self.client.get(other_url).status_code, 404)
        self.assertEqual(
            self.client.get(other_url, {"message_page": 1}).status_code, 404
        )
        # Even a populated conversation field on a non-chat report is insufficient.
        profile = self.extra_report(
            kind=Report.Kind.PROFILE, conversation=self.other_thread
        )
        self.assertEqual(self.client.get(other_url).status_code, 404)
        profile_url = reverse("admin:reports_report_conversation", args=[profile.pk])
        self.assertEqual(self.client.get(profile_url).status_code, 404)
        self.assertNotIn(Conversation, admin.site._registry)
        self.assertNotIn(DirectMessage, admin.site._registry)

    def test_access_ends_after_last_resolution_and_links_disappear(self):
        other = self.extra_report()
        self.client.post(reverse("admin:reports_report_resolve", args=[self.report.pk]))
        # The selected entry can be resolved while another report keeps access open.
        response = self.client.get(self.url, follow=True)
        self.assertContains(response, "Original context")
        self.assertContains(response, "Status: Resolved")
        self.assertContains(self.client.get(self.feed_url, {"status": "all"}), self.url)
        self.client.post(reverse("admin:reports_report_resolve", args=[other.pk]))
        self.assertEqual(self.client.get(self.url).status_code, 404)
        self.assertEqual(
            self.client.get(self.url, {"message_page": 1}).status_code, 404
        )
        self.assertNotContains(
            self.client.get(self.feed_url, {"status": "all"}), self.url
        )

    def test_missing_reports_and_conversations(self):
        self.assertEqual(
            self.client.get(
                reverse("admin:reports_report_conversation", args=[99999])
            ).status_code,
            404,
        )
        self.thread.delete()
        self.assertEqual(self.client.get(self.url).status_code, 404)
        self.assertNotContains(self.client.get(self.feed_url), "Review conversation")

    def test_report_removal_revokes_access(self):
        self.report.delete()
        self.assertEqual(
            self.client.get(self.url, {"message_page": 1}).status_code, 404
        )

    def test_initial_jump_and_pagination_with_timestamp_ties(self):
        messages = [self.message]
        messages += DirectMessage.objects.bulk_create(
            [
                DirectMessage(
                    conversation=self.thread, sender=self.first, body=f"Message {n}"
                )
                for n in range(2, 106)
            ]
        )
        self.thread.messages.update(created_at=timezone.now())
        self.report.context_message = messages[50]
        self.report.save(update_fields=["context_message"])
        response = self.client.get(self.url + "?status=all&sort=most&page=3")
        parsed = urlsplit(response.url)
        self.assertEqual(parsed.fragment, f"message-{messages[50].pk}")
        self.assertEqual(
            parse_qs(parsed.query),
            {
                "status": ["all"],
                "sort": ["most"],
                "page": ["3"],
                "message_page": ["2"],
            },
        )
        response = self.client.get(response.url)
        page = response.context["message_page"]
        self.assertEqual([m.pk for m in page], [m.pk for m in messages[50:100]])
        self.assertContains(response, "Report context message")
        self.assertContains(
            response, 'class="module conversation-message report-context-message"'
        )
        self.assertContains(response, "Earlier messages")
        self.assertContains(response, "Later messages")
        self.assertEqual(
            response.context["feed_url"], self.feed_url + "?status=all&sort=most&page=3"
        )
        self.assertEqual(
            len(self.client.get(self.url, {"message_page": 3}).context["message_page"]),
            5,
        )
        for invalid in ("bad", "9999"):
            self.assertEqual(
                self.client.get(self.url, {"message_page": invalid}).status_code, 200
            )
        # The 50th message remains on page one.
        self.report.context_message = messages[49]
        self.report.save(update_fields=["context_message"])
        self.assertIn("message_page=1#", self.client.get(self.url).url)

    def test_missing_or_foreign_context_defaults_to_latest_page(self):
        DirectMessage.objects.bulk_create(
            [
                DirectMessage(conversation=self.thread, sender=self.first, body="Later")
                for _ in range(51)
            ]
        )
        for context in (None, self.other_message):
            self.report.context_message = context
            self.report.save(update_fields=["context_message"])
            response = self.client.get(self.url)
            self.assertTrue(response.url.endswith("message_page=2"))
            response = self.client.get(response.url)
            self.assertContains(response, "context message is unavailable")
            self.assertNotContains(response, self.other_message.body)
            self.assertNotContains(response, "Report context</a>")
        self.thread.messages.all().delete()
        self.assertContains(
            self.client.get(self.url, follow=True), "This conversation has no messages."
        )

    def test_live_messages_escaping_and_no_state_changes(self):
        later = DirectMessage.objects.create(
            conversation=self.thread,
            sender=self.first,
            body="<script>unsafe()</script>\nLater message",
        )
        before = {
            model: list(model.objects.order_by("pk").values())
            for model in (Conversation, DirectMessage, Report)
        }
        response = self.client.get(self.url, follow=True)
        self.assertContains(response, "Original context")
        self.assertContains(response, "Later message")
        self.assertContains(response, "&lt;script&gt;unsafe()&lt;/script&gt;")
        self.assertNotContains(response, "<script>unsafe()")
        self.assertNotContains(response, self.other_message.body)
        self.assertContains(response, f'id="message-{later.pk}"')
        for model, values in before.items():
            self.assertEqual(list(model.objects.order_by("pk").values()), values)
        for hop, _ in response.redirect_chain:
            self.assertTrue(hop.startswith(self.url))
        for header in ("no-cache", "no-store", "private"):
            self.assertIn(header, response.headers["Cache-Control"])
        # Moderation did not bypass participant access on the main site.
        self.assertEqual(
            self.client.get(reverse("conversation", args=[self.thread.pk])).status_code,
            404,
        )

    def test_grouped_reports_link_to_their_own_conversations_without_extra_queries(
        self,
    ):
        response = self.client.get(self.feed_url)
        self.assertContains(response, self.url)
        self.assertNotContains(response, self.message.body)
        with CaptureQueriesContext(connection) as initial:
            self.client.get(self.feed_url)
        other = self.extra_report(
            reporter=self.third,
            reported_user=self.second,
            conversation=self.other_thread,
            context_message=self.other_message,
        )
        with CaptureQueriesContext(connection) as expanded:
            response = self.client.get(self.feed_url)
        self.assertEqual(len(initial), len(expanded))
        self.assertEqual(len(response.context["groups"]), 1)
        self.assertContains(response, self.url)
        self.assertContains(
            response, reverse("admin:reports_report_conversation", args=[other.pk])
        )
        self.assertNotContains(response, self.other_message.body)
