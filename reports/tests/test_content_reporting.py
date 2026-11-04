"""Reporting public comments and users with reference-only chat context."""

from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from threading import Barrier
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.db import IntegrityError, connections, transaction
from django.test import Client, TestCase, TransactionTestCase
from django.urls import reverse
from django.utils import timezone

from events.models import (
    ContactRequest,
    Conversation,
    DirectMessage,
    Event,
    EventPost,
    EventReply,
)
from reports.forms import ReportForm
from reports.models import Report
from reports.services import record_report, unresolved_report_lookup

from .test_reporting import ReportTestData


class ContentReportTestData(ReportTestData):
    """An event discussion and an approved conversation with its author."""

    @classmethod
    def create_content_data(cls):
        cls.create_data()
        cls.outsider = get_user_model().objects.create_user(
            username="outsider", email="outsider@example.com"
        )
        cls.post = EventPost.objects.create(
            event=cls.event, author=cls.host, body="Public comment to report."
        )
        cls.reply = EventReply.objects.create(
            post=cls.post, author=cls.host, body="Public reply to report."
        )
        cls.own_post = EventPost.objects.create(
            event=cls.event, author=cls.reporter, body="My own comment."
        )
        cls.own_reply = EventReply.objects.create(
            post=cls.post, author=cls.reporter, body="My own reply."
        )
        cls.thread, _ = Conversation.get_or_create_for_users(cls.reporter, cls.host)
        cls.contact = ContactRequest.objects.create(
            event=cls.event,
            sender=cls.reporter,
            recipient=cls.host,
            status=ContactRequest.Status.ACCEPTED,
        )
        cls.targets = {
            Report.Kind.POST: cls.post,
            Report.Kind.REPLY: cls.reply,
            Report.Kind.USER: cls.host,
        }
        cls.urls = {
            Report.Kind.POST: reverse("event_post_report", args=[cls.post.pk]),
            Report.Kind.REPLY: reverse("event_reply_report", args=[cls.reply.pk]),
            Report.Kind.USER: reverse("conversation_report", args=[cls.thread.pk]),
        }
        cls.chat_url = reverse("conversation", args=[cls.thread.pk])

    def submit(self, report_kind, **fields):
        data = {"reason": Report.Reason.HARASSMENT_HATE, "details": "Please review."}
        data.update(fields)
        return self.client.post(self.urls[report_kind], data)

    def message(self, body="Private message", sender=None):
        return DirectMessage.objects.create(
            conversation=self.thread, sender=sender or self.host, body=body
        )


class PublicCommentReportingTests(ContentReportTestData, TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.create_content_data()

    def setUp(self):
        self.client.force_login(self.reporter)

    def test_menus_report_others_comments_and_replies_but_not_own_content(self):
        response = self.client.get(self.detail_url)
        self.assertContains(response, 'aria-label="Comment actions"')
        self.assertContains(response, 'aria-label="Reply actions"')
        for kind in (Report.Kind.POST, Report.Kind.REPLY):
            self.assertContains(response, f'href="{self.urls[kind]}"')
        self.assertNotContains(
            response, reverse("event_post_report", args=[self.own_post.pk])
        )
        self.assertNotContains(
            response, reverse("event_reply_report", args=[self.own_reply.pk])
        )
        self.assertContains(response, "/static/reports/menus.js")
        self.assertContains(response, "/static/reports/menus.css")

        self.client.logout()
        response = self.client.get(self.detail_url)
        for url in self.urls.values():
            if url != self.urls[Report.Kind.USER]:
                self.assertContains(response, f'href="{url}"')

    def test_shared_form_identifies_source_author_and_optional_details(self):
        for kind, comment in (
            (Report.Kind.POST, self.post),
            (Report.Kind.REPLY, self.reply),
        ):
            with self.subTest(kind=kind):
                response = self.client.get(self.urls[kind])
                self.assertTemplateUsed(response, "reports/report_form.html")
                self.assertContains(response, comment.body)
                self.assertContains(response, self.host.get_display_name())
                self.assertContains(response, f'<a href="{self.detail_url}">Cancel</a>')
                self.assertFalse(response.context["form"].fields["details"].required)
        self.assertFalse(Report.objects.exists())

    def test_post_and_reply_reports_derive_exact_target_author_and_event(self):
        for kind, target_field, comment in (
            (Report.Kind.POST, "reported_post", self.post),
            (Report.Kind.REPLY, "reported_reply", self.reply),
        ):
            with self.subTest(kind=kind):
                self.assertRedirects(self.submit(kind), self.detail_url)
                report = Report.objects.get(kind=kind)
                self.assertEqual(getattr(report, target_field), comment)
                self.assertEqual(report.reported_user, comment.author)
                self.assertEqual(report.reporter, self.reporter)
                self.assertEqual(report.event, self.event)
                self.assertEqual(report.event_title, self.event.title)
                self.assertIsNone(report.conversation_id)
                self.assertIsNone(report.context_message_id)

    def test_event_other_comments_replies_and_user_reports_are_independent(self):
        self.submit(Report.Kind.POST)
        self.assertRedirects(self.submit(Report.Kind.REPLY), self.detail_url)
        self.assertRedirects(
            self.client.post(self.report_url, {"reason": Report.Reason.OTHER}),
            self.detail_url,
        )
        another = EventPost.objects.create(
            event=self.event, author=self.host, body="Another comment."
        )
        self.assertRedirects(
            self.client.post(
                reverse("event_post_report", args=[another.pk]),
                {"reason": Report.Reason.OTHER},
            ),
            self.detail_url,
        )
        self.assertRedirects(self.submit(Report.Kind.USER), self.chat_url)
        self.assertEqual(Report.objects.count(), 5)

    def test_any_signed_in_user_can_report_comments_without_event_interest(self):
        self.client.force_login(self.outsider)
        for kind in (Report.Kind.POST, Report.Kind.REPLY):
            self.assertRedirects(self.submit(kind), self.detail_url)
        self.assertEqual(Report.objects.filter(reporter=self.outsider).count(), 2)

    def test_closed_and_ended_events_still_allow_comment_reports(self):
        for status in (Event.Status.DONE, Event.Status.CANCELLED):
            with self.subTest(status=status):
                self.event.status = status
                self.event.save(update_fields=["status"])
                response = self.client.get(self.detail_url)
                for kind in (Report.Kind.POST, Report.Kind.REPLY):
                    self.assertContains(response, f'href="{self.urls[kind]}"')
                    self.assertEqual(self.client.get(self.urls[kind]).status_code, 200)
                    self.assertRedirects(self.submit(kind), self.detail_url)
                Report.objects.update(resolved_at=timezone.now())

    def test_self_reports_are_rejected_for_comments_and_replies(self):
        urls = (
            reverse("event_post_report", args=[self.own_post.pk]),
            reverse("event_reply_report", args=[self.own_reply.pk]),
        )
        for url in urls:
            self.assertEqual(self.client.get(url).status_code, 404)
            self.assertEqual(
                self.client.post(url, {"reason": Report.Reason.OTHER}).status_code, 404
            )
        self.assertFalse(Report.objects.exists())

    def test_target_identity_and_context_cannot_be_forged(self):
        for kind in (Report.Kind.POST, Report.Kind.REPLY):
            with self.subTest(kind=kind):
                self.assertRedirects(
                    self.submit(
                        kind,
                        kind=Report.Kind.USER,
                        reported_post=self.own_post.pk,
                        reported_reply=self.own_reply.pk,
                        reported_user=self.outsider.pk,
                        reporter=self.host.pk,
                        event=self.event.pk + 100,
                        event_title="Forged title",
                        conversation=self.thread.pk,
                        context_message=9999,
                        resolved_at="2000-01-01T00:00:00Z",
                    ),
                    self.detail_url,
                )
                report = Report.objects.get(kind=kind)
                self.assertEqual(report.reported_user, self.host)
                self.assertEqual(report.reporter, self.reporter)
                self.assertEqual(report.event, self.event)
                self.assertIsNone(report.resolved_at)
                self.assertIsNone(report.conversation_id)

    def test_comment_excerpt_is_escaped(self):
        self.post.body = '<script>alert("comment")</script>'
        self.post.save(update_fields=["body"])
        response = self.client.get(self.urls[Report.Kind.POST])
        self.assertContains(response, "&lt;script&gt;")
        self.assertNotContains(response, self.post.body)


class ContentReportValidationTests(ContentReportTestData, TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.create_content_data()

    def setUp(self):
        self.client.force_login(self.reporter)

    def test_new_endpoints_require_login_and_preserve_return_destination(self):
        self.client.logout()
        for url in self.urls.values():
            for response in (
                self.client.get(url),
                self.client.post(url, {"reason": Report.Reason.OTHER}),
            ):
                self.assertRedirects(response, reverse("login") + "?next=" + url)
        self.assertFalse(Report.objects.exists())

    def test_all_new_forms_validate_reason_and_details_and_preserve_input(self):
        for kind, url in self.urls.items():
            for data, error_field in (
                ({"details": "Keep this explanation."}, "reason"),
                ({"reason": "invalid", "details": "Keep this explanation."}, "reason"),
                ({"reason": Report.Reason.OTHER, "details": "x" * 5001}, "details"),
            ):
                with self.subTest(kind=kind, error_field=error_field):
                    response = self.client.post(url, data)
                    self.assertEqual(response.status_code, 200)
                    self.assertIn(error_field, response.context["form"].errors)
                    self.assertContains(response, data["details"])
                    self.assertFalse(Report.objects.exists())
            response = self.client.post(url, {"reason": Report.Reason.OTHER})
            expected_url = (
                self.chat_url if kind == Report.Kind.USER else self.detail_url
            )
            self.assertRedirects(response, expected_url)
            self.assertEqual(Report.objects.get(kind=kind).details, "")
            # Keep the no-write assertions above scoped to each form.
            Report.objects.all().delete()

    def test_missing_targets_return_404(self):
        for name in ("event_post_report", "event_reply_report", "conversation_report"):
            url = reverse(name, args=[999999])
            self.assertEqual(self.client.get(url).status_code, 404)
            self.assertEqual(
                self.client.post(url, {"reason": Report.Reason.OTHER}).status_code, 404
            )

    def test_new_endpoints_reject_other_http_methods(self):
        for url in self.urls.values():
            for method in ("put", "patch", "delete", "head", "options"):
                self.assertEqual(getattr(self.client, method)(url).status_code, 405)
        self.assertFalse(Report.objects.exists())

    def test_all_new_submissions_require_csrf_and_accept_a_valid_token(self):
        client = Client(enforce_csrf_checks=True)
        client.force_login(self.reporter)
        for kind, url in self.urls.items():
            with self.subTest(kind=kind):
                data = {"reason": Report.Reason.OTHER}
                self.assertEqual(client.post(url, data).status_code, 403)
                self.assertFalse(Report.objects.filter(kind=kind).exists())
                client.get(url)
                data["csrfmiddlewaretoken"] = client.cookies["csrftoken"].value
                self.assertEqual(client.post(url, data).status_code, 302)
                self.assertTrue(Report.objects.filter(kind=kind).exists())

    def test_duplicate_reports_preserve_original_and_resolution_allows_another(self):
        for kind, url in self.urls.items():
            with self.subTest(kind=kind):
                self.submit(kind)
                original = Report.objects.values().get(kind=kind)
                expected_url = (
                    self.chat_url if kind == Report.Kind.USER else self.detail_url
                )
                for response in (
                    self.client.get(url),
                    self.submit(kind, details="Replacement"),
                ):
                    self.assertRedirects(response, expected_url)
                    self.assertEqual(Report.objects.values().get(kind=kind), original)
                Report.objects.filter(kind=kind).update(resolved_at=timezone.now())
                self.assertEqual(self.client.get(url).status_code, 200)
                self.assertRedirects(
                    self.submit(kind, details="New incident"), expected_url
                )
                self.assertEqual(Report.objects.filter(kind=kind).count(), 2)
                self.assertEqual(
                    Report.objects.get(kind=kind, resolved_at=None).details,
                    "New incident",
                )


class ChatUserReportingTests(ContentReportTestData, TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.create_content_data()

    def setUp(self):
        self.client.force_login(self.reporter)

    def test_chat_menu_and_form_identify_other_user_without_showing_chat_contents(self):
        response = self.client.get(self.chat_url)
        self.assertContains(response, 'aria-label="Conversation actions"')
        self.assertContains(response, f'href="{self.urls[Report.Kind.USER]}"')
        self.assertContains(response, "Report user")
        message = self.message("Sensitive message not copied into report page.")
        response = self.client.get(self.urls[Report.Kind.USER])
        self.assertContains(response, self.host.get_display_name())
        self.assertContains(response, f'<a href="{self.chat_url}">Cancel</a>')
        self.assertNotContains(response, message.body)
        message.refresh_from_db()
        self.assertIsNone(message.read_at)
        self.assertFalse(Report.objects.exists())

    def test_submission_anchors_latest_message_at_submit_even_if_it_is_reporters(self):
        self.message("Message present when form opened.")
        self.client.get(self.urls[Report.Kind.USER])
        latest = self.message(
            "Reporter replies while report form is open.", sender=self.reporter
        )
        backdated = self.message("Inserted later but earlier in conversation order.")
        DirectMessage.objects.filter(pk=backdated.pk).update(
            created_at=timezone.now() - timedelta(days=1)
        )
        self.assertRedirects(self.submit(Report.Kind.USER), self.chat_url)
        report = Report.objects.get()
        self.assertEqual(report.kind, Report.Kind.USER)
        self.assertEqual(report.reported_user, self.host)
        self.assertEqual(report.conversation, self.thread)
        self.assertEqual(report.context_message, latest)
        self.assertEqual(report.event_title, "")
        self.assertIsNone(report.event_id)
        self.assertIsNone(report.reported_post_id)
        self.assertIsNone(report.reported_reply_id)
        self.assertNotIn(latest.body, Report.objects.values().get().values())

        self.message("Later arrival does not move the saved anchor.")
        self.submit(Report.Kind.USER, details="Replacement")
        report.refresh_from_db()
        self.assertEqual(report.context_message, latest)
        self.assertEqual(report.details, "Please review.")

    def test_message_id_breaks_timestamp_ties(self):
        first = self.message()
        latest = self.message()
        DirectMessage.objects.filter(pk__in=[first.pk, latest.pk]).update(
            created_at=first.created_at
        )
        self.submit(Report.Kind.USER)
        self.assertEqual(Report.objects.get().context_message, latest)

    def test_empty_conversation_has_user_and_conversation_but_no_anchor(self):
        self.assertRedirects(self.submit(Report.Kind.USER), self.chat_url)
        report = Report.objects.get()
        self.assertEqual(report.reported_user, self.host)
        self.assertEqual(report.conversation, self.thread)
        self.assertIsNone(report.context_message_id)

    def test_reporting_does_not_change_messages_or_conversation_activity(self):
        message = self.message()
        before_message = DirectMessage.objects.values().get(pk=message.pk)
        before_thread = Conversation.objects.values().get(pk=self.thread.pk)
        self.client.get(self.urls[Report.Kind.USER])
        self.submit(Report.Kind.USER)
        self.assertEqual(
            DirectMessage.objects.values().get(pk=message.pk), before_message
        )
        self.assertEqual(
            Conversation.objects.values().get(pk=self.thread.pk), before_thread
        )
        self.assertEqual(DirectMessage.objects.count(), 1)

    def test_nonparticipants_including_staff_cannot_report_chat_users(self):
        self.outsider.is_staff = True
        self.outsider.is_superuser = True
        self.outsider.save(update_fields=["is_staff", "is_superuser"])
        self.client.force_login(self.outsider)
        self.assertEqual(self.client.get(self.urls[Report.Kind.USER]).status_code, 404)
        self.assertEqual(self.submit(Report.Kind.USER).status_code, 404)
        self.assertFalse(Report.objects.exists())

    def test_participants_need_an_accepted_contact_request(self):
        self.contact.status = ContactRequest.Status.PENDING
        self.contact.save(update_fields=["status"])
        self.assertEqual(self.client.get(self.urls[Report.Kind.USER]).status_code, 404)
        self.assertEqual(self.submit(Report.Kind.USER).status_code, 404)
        self.assertFalse(Report.objects.exists())

    def test_forged_user_conversation_and_message_ids_are_ignored(self):
        other_thread, _ = Conversation.get_or_create_for_users(self.host, self.outsider)
        other_message = DirectMessage.objects.create(
            conversation=other_thread,
            sender=self.outsider,
            body="Unrelated private message.",
        )
        anchor = self.message()
        self.assertRedirects(
            self.submit(
                Report.Kind.USER,
                reported_user=self.reporter.pk,
                conversation=other_thread.pk,
                context_message=other_message.pk,
                event=self.event.pk,
                reported_post=self.post.pk,
                reported_reply=self.reply.pk,
            ),
            self.chat_url,
        )
        report = Report.objects.get()
        self.assertEqual(report.reported_user, self.host)
        self.assertEqual(report.conversation, self.thread)
        self.assertEqual(report.context_message, anchor)
        self.assertIsNone(report.event_id)

    def test_both_participants_can_report_each_other(self):
        self.submit(Report.Kind.USER)
        self.client.force_login(self.host)
        self.assertRedirects(self.submit(Report.Kind.USER), self.chat_url)
        self.assertEqual(
            Report.objects.get(reporter=self.host).reported_user, self.reporter
        )
        self.assertEqual(Report.objects.count(), 2)

    def test_resolution_captures_fresh_context_without_changing_original(self):
        first = self.message("Original context")
        self.submit(Report.Kind.USER)
        original = Report.objects.get()
        original.resolved_at = timezone.now()
        original.save(update_fields=["resolved_at"])
        latest = self.message("New context")
        self.submit(Report.Kind.USER)
        self.assertEqual(Report.objects.get(resolved_at=None).context_message, latest)
        original.refresh_from_db()
        self.assertEqual(original.context_message, first)

    def test_confirmations_and_duplicate_feedback_are_rendered_in_chat(self):
        for expected in (
            "Your report has been submitted.",
            "You have already reported this user.",
        ):
            response = self.client.post(
                self.urls[Report.Kind.USER],
                {"reason": Report.Reason.OTHER},
                follow=True,
            )
            self.assertRedirects(response, self.chat_url)
            self.assertContains(response, expected)
            self.assertContains(response, 'class="message-alerts" role="status"')

    def test_recreated_conversation_does_not_bypass_unresolved_user_report(self):
        self.submit(Report.Kind.USER)
        original = Report.objects.get()
        self.thread.delete()
        thread, _ = Conversation.get_or_create_for_users(self.reporter, self.host)
        response = self.client.post(
            reverse("conversation_report", args=[thread.pk]),
            {"reason": Report.Reason.OTHER},
        )
        self.assertRedirects(response, reverse("conversation", args=[thread.pk]))
        original.refresh_from_db()
        self.assertIsNone(original.conversation_id)
        self.assertEqual(Report.objects.count(), 1)


class ContentReportHistoryTests(ContentReportTestData, TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.create_content_data()

    def setUp(self):
        self.client.force_login(self.reporter)

    def test_database_enforces_each_target_constraint_independently(self):
        targets = {Report.Kind.EVENT: self.event, **self.targets}
        data = {"reason": Report.Reason.OTHER, "details": "Original"}
        for kind, target in targets.items():
            with self.subTest(kind=kind):
                original, created = record_report(
                    self.reporter, kind, target, data, conversation=self.thread
                )
                self.assertTrue(created)
                with self.assertRaises(IntegrityError), transaction.atomic():
                    Report.objects.create(
                        **unresolved_report_lookup(self.reporter, kind, target),
                        reason=Report.Reason.SPAM_SCAM,
                    )
                self.assertEqual(Report.objects.get(kind=kind), original)
                if kind != Report.Kind.EVENT:
                    self.assertIn(
                        f"{original.get_kind_display()} report #{original.pk}",
                        str(original),
                    )
        self.assertEqual(Report.objects.count(), 4)

    def test_deleting_parent_comment_clears_post_and_reply_references(self):
        self.submit(Report.Kind.POST)
        self.submit(Report.Kind.REPLY)
        self.post.delete()
        for report in Report.objects.all():
            self.assertIsNone(report.reported_post_id)
            self.assertIsNone(report.reported_reply_id)
            self.assertEqual(report.reported_user, self.host)
            self.assertEqual(report.event, self.event)
            self.assertEqual(report.details, "Please review.")
        self.assertEqual(Report.objects.count(), 2)

    def test_deleting_event_keeps_comment_reports_and_clears_deleted_sources(self):
        self.submit(Report.Kind.POST)
        self.submit(Report.Kind.REPLY)
        self.event.delete()
        for report in Report.objects.all():
            self.assertIsNone(report.event_id)
            self.assertIsNone(report.reported_post_id)
            self.assertIsNone(report.reported_reply_id)
            self.assertEqual(report.reported_user, self.host)
        self.assertEqual(Report.objects.count(), 2)

    def test_deleting_anchor_keeps_report_and_conversation(self):
        message = self.message()
        self.submit(Report.Kind.USER)
        message.delete()
        report = Report.objects.get()
        self.assertIsNone(report.context_message_id)
        self.assertEqual(report.conversation, self.thread)
        self.assertEqual(report.details, "Please review.")

    def test_deleting_conversation_clears_chat_context_but_keeps_report(self):
        self.message()
        self.submit(Report.Kind.USER)
        self.thread.delete()
        report = Report.objects.get()
        self.assertIsNone(report.conversation_id)
        self.assertIsNone(report.context_message_id)
        self.assertEqual(report.reported_user, self.host)
        self.assertIsNotNone(report.created_at)

    def test_deleting_reported_user_retains_all_reports_with_cleared_links(self):
        self.message()
        for kind in self.urls:
            self.submit(kind)
        self.host.delete()
        for report in Report.objects.all():
            self.assertIsNone(report.reported_user_id)
            self.assertIsNone(report.reported_post_id)
            self.assertIsNone(report.reported_reply_id)
            self.assertIsNone(report.conversation_id)
            self.assertIsNone(report.context_message_id)
            self.assertEqual(report.reporter, self.reporter)
        self.assertEqual(Report.objects.count(), 3)

    def test_deleting_reporter_retains_all_report_types(self):
        for kind in self.urls:
            self.submit(kind)
        self.reporter.delete()
        for report in Report.objects.all():
            self.assertIsNone(report.reporter_id)
            self.assertEqual(report.reported_user, self.host)
            self.assertEqual(report.details, "Please review.")
        self.assertEqual(Report.objects.count(), 3)


class ConcurrentContentReportTests(ContentReportTestData, TransactionTestCase):
    def setUp(self):
        self.create_content_data()

    def test_concurrent_submissions_preserve_one_report_per_new_target_kind(self):
        for kind, url in self.urls.items():
            with self.subTest(kind=kind):
                clients = [Client(), Client()]
                for client in clients:
                    client.force_login(self.reporter)
                ready = Barrier(2, timeout=15)
                original_is_valid = ReportForm.is_valid

                def validate_together(form):
                    valid = original_is_valid(form)
                    ready.wait()
                    return valid

                def submit(item):
                    client, details = item
                    try:
                        return client.post(
                            url, {"reason": Report.Reason.OTHER, "details": details}
                        )
                    finally:
                        connections.close_all()

                with patch.object(ReportForm, "is_valid", validate_together):
                    with ThreadPoolExecutor(max_workers=2) as pool:
                        responses = list(
                            pool.map(submit, zip(clients, ("First", "Second")))
                        )
                expected_url = (
                    self.chat_url if kind == Report.Kind.USER else self.detail_url
                )
                for response in responses:
                    self.assertRedirects(
                        response, expected_url, fetch_redirect_response=False
                    )
                self.assertIn(
                    Report.objects.get(kind=kind).details, ("First", "Second")
                )
        self.assertEqual(Report.objects.count(), 3)
