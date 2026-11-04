"""Profile-content reporting is public-profile scoped and independent of chats."""

from concurrent.futures import ThreadPoolExecutor
from threading import Barrier
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.core import mail
from django.db import IntegrityError, connections, transaction
from django.test import Client, TestCase, TransactionTestCase
from django.urls import reverse
from django.utils import timezone

from events.models import (
    ContactRequest,
    Conversation,
    DirectMessage,
    Event,
    Notification,
)
from reports.forms import ReportForm
from reports.models import Report


class ProfileReportTestData:
    @classmethod
    def create_data(cls):
        User = get_user_model()
        cls.viewer = User.objects.create_user(
            username="profile-viewer", email="viewer@example.com"
        )
        cls.target = User.objects.create_user(
            username="profile-subject",
            email="subject@example.com",
            display_name="Jordan Park",
            share_profile_with_attendees=True,
            bio="Public profile biography.",
        )
        cls.hidden = User.objects.create_user(
            username="hidden-profile", email="hidden@example.com"
        )
        cls.report_url = reverse("profile_report", args=[cls.target.username])
        cls.profile_url = reverse("public_profile", args=[cls.target.username])

    def submit(self, **fields):
        data = {
            "reason": Report.Reason.INAPPROPRIATE_CONTENT,
            "details": "Please review the bio.",
        }
        data.update(fields)
        return self.client.post(self.report_url, data)

    def approved_chat(self):
        event = Event.objects.create(
            guid="profile-report-chat", title="Shared event", content_hash=""
        )
        ContactRequest.objects.create(
            event=event,
            sender=self.viewer,
            recipient=self.target,
            status=ContactRequest.Status.ACCEPTED,
        )
        thread, _ = Conversation.get_or_create_for_users(self.viewer, self.target)
        message = DirectMessage.objects.create(
            conversation=thread, sender=self.target, body="Private chat context."
        )
        return thread, message


class ProfileReportingTests(ProfileReportTestData, TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.create_data()

    def setUp(self):
        self.client.force_login(self.viewer)

    def test_profile_menu_does_not_require_messaging_access(self):
        response = self.client.get(self.profile_url)
        self.assertFalse(response.context["can_message"])
        self.assertContains(response, 'aria-label="Profile actions"')
        self.assertContains(response, f'href="{self.report_url}"')
        self.assertContains(response, "Report profile")
        self.assertContains(response, "/static/reports/menus.css")
        self.assertContains(response, "/static/reports/menus.js")
        self.assertNotContains(response, "Send Message")
        self.assertFalse(ContactRequest.objects.exists())
        self.assertFalse(Conversation.objects.exists())

        self.assertEqual(self.client.get(self.report_url).status_code, 200)
        self.assertRedirects(self.submit(), self.profile_url)

    def test_messaging_and_reporting_actions_can_both_be_shown(self):
        self.approved_chat()
        response = self.client.get(self.profile_url)
        self.assertContains(response, "Send Message")
        self.assertContains(response, f'href="{self.report_url}"')

    def test_own_profile_keeps_edit_action_and_hides_report_menu(self):
        response = self.client.get(
            reverse("public_profile", args=[self.viewer.username])
        )
        self.assertContains(response, "Edit profile")
        self.assertNotContains(response, "Report profile")
        self.assertNotContains(response, 'aria-label="Profile actions"')

    def test_hidden_profile_does_not_offer_reporting_or_reveal_private_fields(self):
        response = self.client.get(
            reverse("public_profile", args=[self.hidden.username])
        )
        self.assertContains(response, "Profile Unavailable")
        self.assertNotContains(response, "Report profile")
        self.assertNotContains(
            response, reverse("profile_report", args=[self.hidden.username])
        )

    def test_shared_form_identifies_profile_and_has_cancel_link(self):
        response = self.client.get(self.report_url)
        self.assertTemplateUsed(response, "reports/report_form.html")
        self.assertContains(response, "Report profile")
        self.assertContains(response, self.target.display_name)
        self.assertContains(response, f"@{self.target.username}")
        self.assertContains(response, f'<a href="{self.profile_url}">Cancel</a>')
        self.assertFalse(response.context["form"].fields["details"].required)
        for value, label in Report.Reason.choices:
            self.assertContains(response, f'<option value="{value}">{label}</option>')
        self.assertFalse(Report.objects.exists())

    def test_submission_records_profile_reference_without_changing_user_or_notifying(
        self,
    ):
        before = get_user_model().objects.values().get(pk=self.target.pk)
        response = self.client.post(
            self.report_url,
            {
                "reason": Report.Reason.INAPPROPRIATE_CONTENT,
                "details": "  Please review.\n  ",
            },
            follow=True,
        )
        self.assertRedirects(response, self.profile_url)
        self.assertContains(response, "Your report has been submitted.")
        self.assertContains(response, 'class="profile-alerts" role="status"')
        report = Report.objects.get()
        self.assertEqual(report.kind, Report.Kind.PROFILE)
        self.assertEqual(report.reporter, self.viewer)
        self.assertEqual(report.reported_user, self.target)
        self.assertEqual(report.details, "Please review.")
        self.assertEqual(report.event_title, "")
        for field in (
            "event_id",
            "reported_post_id",
            "reported_reply_id",
            "conversation_id",
            "context_message_id",
            "resolved_at",
        ):
            self.assertIsNone(getattr(report, field))
        self.assertIsNotNone(report.created_at)
        self.assertEqual(
            get_user_model().objects.values().get(pk=self.target.pk), before
        )
        self.assertFalse(Notification.objects.exists())
        self.assertEqual(mail.outbox, [])

    def test_profile_report_follows_live_content_without_snapshotting_text_or_photo(
        self,
    ):
        self.target.profile_photo = "profile_photos/original.jpg"
        self.target.save(update_fields=["profile_photo"])
        self.submit()
        report = Report.objects.get()
        saved = Report.objects.values().get()
        self.assertNotIn(self.target.bio, saved.values())
        self.assertNotIn(self.target.profile_photo.name, saved.values())
        self.assertNotIn(self.target.display_name, saved.values())

        self.target.bio = "Updated biography."
        self.target.profile_photo = "profile_photos/replacement.jpg"
        self.target.save(update_fields=["bio", "profile_photo"])
        report.refresh_from_db()
        self.assertEqual(report.reported_user.bio, "Updated biography.")
        self.assertEqual(
            report.reported_user.profile_photo.name, "profile_photos/replacement.jpg"
        )

    def test_login_is_required_with_return_to_report_form(self):
        self.client.logout()
        for response in (
            self.client.get(self.report_url),
            self.submit(),
        ):
            self.assertRedirects(
                response, reverse("login") + "?next=" + self.report_url
            )
        self.assertFalse(Report.objects.exists())

    def test_self_hidden_and_missing_targets_are_rejected_on_get_and_post(self):
        for username in (self.viewer.username, self.hidden.username, "missing-member"):
            with self.subTest(username=username):
                url = reverse("profile_report", args=[username])
                self.assertEqual(self.client.get(url).status_code, 404)
                self.assertEqual(
                    self.client.post(url, {"reason": Report.Reason.OTHER}).status_code,
                    404,
                )
        self.assertFalse(Report.objects.exists())

    def test_sharing_disabled_while_form_is_open_prevents_submission(self):
        self.assertEqual(self.client.get(self.report_url).status_code, 200)
        self.target.share_profile_with_attendees = False
        self.target.save(update_fields=["share_profile_with_attendees"])
        self.assertEqual(self.submit().status_code, 404)
        self.assertFalse(Report.objects.exists())

    def test_invalid_reason_or_overlong_details_preserve_input_without_saving(self):
        for data, error_field in (
            ({"details": "Keep my explanation."}, "reason"),
            ({"reason": "invalid", "details": "Keep my explanation."}, "reason"),
            ({"reason": Report.Reason.OTHER, "details": "x" * 5001}, "details"),
        ):
            with self.subTest(error_field=error_field):
                response = self.client.post(self.report_url, data)
                self.assertEqual(response.status_code, 200)
                self.assertIn(error_field, response.context["form"].errors)
                self.assertContains(response, data["details"])
                self.assertFalse(Report.objects.exists())

    def test_other_reason_allows_omitted_or_empty_details(self):
        self.assertRedirects(
            self.client.post(self.report_url, {"reason": Report.Reason.OTHER}),
            self.profile_url,
        )
        self.assertEqual(Report.objects.get().details, "")

    def test_profile_identity_and_invalid_details_are_escaped(self):
        self.target.display_name = '<script>alert("name")</script>'
        self.target.save(update_fields=["display_name"])
        details = '<script>alert("details")</script>'
        response = self.client.post(self.report_url, {"details": details})
        self.assertContains(response, "&lt;script&gt;")
        self.assertNotContains(response, self.target.display_name)
        self.assertNotContains(response, details)

    def test_submitted_identity_and_chat_context_are_ignored(self):
        thread, message = self.approved_chat()
        response = self.submit(
            kind=Report.Kind.USER,
            reporter=self.target.pk,
            reported_user=self.hidden.pk,
            conversation=thread.pk,
            context_message=message.pk,
            event=99999,
            reported_post=99999,
            reported_reply=99999,
            event_title="Forged context",
            resolved_at="2000-01-01T00:00:00Z",
        )
        self.assertRedirects(response, self.profile_url)
        report = Report.objects.get()
        self.assertEqual(report.kind, Report.Kind.PROFILE)
        self.assertEqual(report.reporter, self.viewer)
        self.assertEqual(report.reported_user, self.target)
        self.assertEqual(report.event_title, "")
        for field in (
            "event_id",
            "reported_post_id",
            "reported_reply_id",
            "conversation_id",
            "context_message_id",
            "resolved_at",
        ):
            self.assertIsNone(getattr(report, field))
        message.refresh_from_db()
        self.assertIsNone(message.read_at)

    def test_profile_and_chat_reports_are_independent_in_both_submission_orders(self):
        thread, anchor = self.approved_chat()
        chat_url = reverse("conversation_report", args=[thread.pk])
        for urls in ((self.report_url, chat_url), (chat_url, self.report_url)):
            with self.subTest(urls=urls):
                for url in urls:
                    self.assertEqual(
                        self.client.post(
                            url, {"reason": Report.Reason.OTHER}
                        ).status_code,
                        302,
                    )
                self.assertEqual(Report.objects.count(), 2)
                profile_report = Report.objects.get(kind=Report.Kind.PROFILE)
                chat_report = Report.objects.get(kind=Report.Kind.USER)
                self.assertEqual(profile_report.reported_user, self.target)
                self.assertIsNone(profile_report.conversation_id)
                self.assertIsNone(profile_report.context_message_id)
                self.assertEqual(chat_report.context_message, anchor)
                self.assertEqual(chat_report.conversation, thread)
                Report.objects.all().delete()

    def test_duplicate_reports_preserve_original_and_show_feedback(self):
        self.submit()
        original = Report.objects.values().get()
        for response in (
            self.client.get(self.report_url, follow=True),
            self.client.post(
                self.report_url,
                {"reason": Report.Reason.OTHER, "details": "Replacement"},
                follow=True,
            ),
        ):
            self.assertRedirects(response, self.profile_url)
            self.assertContains(response, "You have already reported this profile.")
            self.assertEqual(Report.objects.values().get(), original)

    def test_resolving_profile_report_allows_another_without_resolving_chat_report(
        self,
    ):
        thread, _ = self.approved_chat()
        self.client.post(
            reverse("conversation_report", args=[thread.pk]),
            {"reason": Report.Reason.OTHER},
        )
        self.submit()
        original = Report.objects.get(kind=Report.Kind.PROFILE)
        original.resolved_at = timezone.now()
        original.save(update_fields=["resolved_at"])
        self.assertEqual(self.client.get(self.report_url).status_code, 200)
        self.assertRedirects(
            self.submit(details="New profile concern"), self.profile_url
        )
        self.assertEqual(Report.objects.filter(kind=Report.Kind.PROFILE).count(), 2)
        self.assertEqual(
            Report.objects.get(kind=Report.Kind.PROFILE, resolved_at=None).details,
            "New profile concern",
        )
        self.assertIsNone(Report.objects.get(kind=Report.Kind.USER).resolved_at)
        original.refresh_from_db()
        self.assertEqual(original.details, "Please review the bio.")

    def test_database_enforces_profile_report_limit(self):
        self.submit()
        with self.assertRaises(IntegrityError), transaction.atomic():
            Report.objects.create(
                kind=Report.Kind.PROFILE,
                reporter=self.viewer,
                reported_user=self.target,
                reason=Report.Reason.OTHER,
            )
        self.assertEqual(Report.objects.count(), 1)

    def test_other_reporters_and_other_shared_profiles_are_independent(self):
        self.submit()
        self.client.force_login(self.hidden)
        self.assertRedirects(self.submit(), self.profile_url)
        self.assertEqual(Report.objects.count(), 2)

        self.client.force_login(self.viewer)
        self.hidden.share_profile_with_attendees = True
        self.hidden.save(update_fields=["share_profile_with_attendees"])
        other_url = reverse("profile_report", args=[self.hidden.username])
        self.assertEqual(
            self.client.post(other_url, {"reason": Report.Reason.OTHER}).status_code,
            302,
        )
        self.assertEqual(Report.objects.filter(reporter=self.viewer).count(), 2)

    def test_deleting_reported_account_clears_link_and_keeps_report(self):
        self.submit()
        self.target.delete()
        report = Report.objects.get()
        self.assertIsNone(report.reported_user_id)
        self.assertEqual(report.reporter, self.viewer)
        self.assertEqual(report.kind, Report.Kind.PROFILE)
        self.assertEqual(report.details, "Please review the bio.")
        self.assertIsNotNone(report.created_at)

    def test_deleting_reporter_keeps_report_and_target(self):
        self.submit()
        self.viewer.delete()
        report = Report.objects.get()
        self.assertIsNone(report.reporter_id)
        self.assertEqual(report.reported_user, self.target)

    def test_csrf_required_and_valid_token_allows_submission(self):
        client = Client(enforce_csrf_checks=True)
        client.force_login(self.viewer)
        data = {"reason": Report.Reason.OTHER}
        self.assertEqual(client.post(self.report_url, data).status_code, 403)
        self.assertFalse(Report.objects.exists())
        response = client.get(self.report_url)
        self.assertContains(response, 'name="csrfmiddlewaretoken"')
        data["csrfmiddlewaretoken"] = client.cookies["csrftoken"].value
        self.assertRedirects(client.post(self.report_url, data), self.profile_url)

    def test_unsupported_methods_are_rejected(self):
        for method in ("put", "patch", "delete", "head", "options"):
            self.assertEqual(
                getattr(self.client, method)(self.report_url).status_code, 405
            )
        self.assertFalse(Report.objects.exists())


class ConcurrentProfileReportTests(ProfileReportTestData, TransactionTestCase):
    def setUp(self):
        self.create_data()

    def test_concurrent_profile_reports_keep_one_original_record(self):
        clients = [Client(), Client()]
        for client in clients:
            client.force_login(self.viewer)
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
                    self.report_url, {"reason": Report.Reason.OTHER, "details": details}
                )
            finally:
                connections.close_all()

        with patch.object(ReportForm, "is_valid", validate_together):
            with ThreadPoolExecutor(max_workers=2) as pool:
                responses = list(pool.map(submit, zip(clients, ("First", "Second"))))
        for response in responses:
            self.assertRedirects(
                response, self.profile_url, fetch_redirect_response=False
            )
        self.assertIn(Report.objects.get().details, ("First", "Second"))
        self.assertCountEqual(
            [
                str(message)
                for response in responses
                for message in response.wsgi_request._messages
            ],
            [
                "Your report has been submitted.",
                "You have already reported this profile.",
            ],
        )
