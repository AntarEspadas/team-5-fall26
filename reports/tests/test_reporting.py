"""Tests for event reports and the history retained for future moderation."""

from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from threading import Barrier
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.core import mail
from django.db import IntegrityError, connections, transaction
from django.test import Client, TestCase, TransactionTestCase
from django.urls import reverse
from django.utils import timezone

from events.models import Event, Notification
from reports.forms import EventReportForm
from reports.models import Report


class ReportTestData:
    """Create an event, its host, and a user who has not joined the event."""

    @classmethod
    def create_data(cls):
        User = get_user_model()
        cls.reporter = User.objects.create_user(
            username="reporter", email="reporter@example.com"
        )
        cls.host = User.objects.create_user(username="host", email="host@example.com")
        cls.event = Event.objects.create(
            guid="host-reportable",
            title="Community Walk",
            host=cls.host,
            content_hash="",
            start_time=timezone.now() + timedelta(days=1),
        )
        cls.report_url = reverse("event_report", args=[cls.event.pk])
        cls.detail_url = reverse("event_detail", args=[cls.event.pk])

    def create_report(self, **fields):
        defaults = {
            "reporter": self.reporter,
            "event": self.event,
            "event_title": self.event.title,
            "reason": Report.Reason.SPAM_SCAM,
            "details": "Original concern",
        }
        defaults.update(fields)
        return Report.objects.create(**defaults)


class EventReportingTests(ReportTestData, TestCase):
    """Exercise the authenticated reporting flow through HTTP requests."""

    @classmethod
    def setUpTestData(cls):
        cls.create_data()

    def setUp(self):
        self.client.force_login(self.reporter)

    def test_event_page_has_report_button_for_members_and_visitors(self):
        for client in (self.client, Client()):
            with self.subTest(authenticated=client is self.client):
                response = client.get(self.detail_url)
                self.assertContains(response, f'href="{self.report_url}"')
                self.assertContains(response, "Report event")

    def test_form_has_unselected_reasons_optional_details_and_cancel_link(self):
        response = self.client.get(self.report_url)
        self.assertTemplateUsed(response, "reports/report_form.html")
        self.assertContains(response, self.event.title)
        self.assertContains(
            response, '<option value="" selected>Select a reason</option>'
        )
        for value, label in Report.Reason.choices:
            self.assertContains(response, f'<option value="{value}">{label}</option>')
        self.assertContains(response, 'name="details"')
        self.assertContains(response, 'maxlength="5000"')
        self.assertFalse(response.context["form"].fields["details"].required)
        self.assertContains(response, f'<a href="{self.detail_url}">Cancel</a>')
        self.assertFalse(Report.objects.exists())

    def test_submission_records_report_and_confirms_without_changing_event(self):
        before = Event.objects.values().get(pk=self.event.pk)
        response = self.client.post(
            self.report_url,
            {
                "reason": Report.Reason.SAFETY_CONCERN,
                "details": "  Unsafe location.\n  ",
            },
            follow=True,
        )
        self.assertRedirects(response, self.detail_url)
        self.assertContains(response, "Your report has been submitted.")
        report = Report.objects.get()
        self.assertEqual(report.reporter, self.reporter)
        self.assertEqual(report.event, self.event)
        self.assertEqual(report.event_title, self.event.title)
        self.assertEqual(report.reason, Report.Reason.SAFETY_CONCERN)
        self.assertEqual(report.details, "Unsafe location.")
        self.assertIsNotNone(report.created_at)
        self.assertIsNone(report.resolved_at)
        self.assertEqual(Event.objects.values().get(pk=self.event.pk), before)
        self.assertFalse(Notification.objects.exists())
        self.assertEqual(mail.outbox, [])

    def test_every_reason_accepts_omitted_details_including_other(self):
        for reason in Report.Reason.values:
            with self.subTest(reason=reason):
                response = self.client.post(self.report_url, {"reason": reason})
                self.assertRedirects(response, self.detail_url)
                report = Report.objects.get(resolved_at=None)
                self.assertEqual(report.reason, reason)
                self.assertEqual(report.details, "")
                report.resolved_at = timezone.now()
                report.save(update_fields=["resolved_at"])

    def test_whitespace_only_details_are_saved_as_empty(self):
        self.client.post(
            self.report_url, {"reason": Report.Reason.OTHER, "details": " \n\t "}
        )
        self.assertEqual(Report.objects.get().details, "")

    def test_missing_or_invalid_reason_preserves_details_without_saving(self):
        for reason in (None, "", "unrecognized"):
            with self.subTest(reason=reason):
                data = {"details": "Please keep this explanation."}
                if reason is not None:
                    data["reason"] = reason
                response = self.client.post(self.report_url, data)
                self.assertEqual(response.status_code, 200)
                self.assertIn("reason", response.context["form"].errors)
                self.assertContains(response, data["details"])
                self.assertFalse(Report.objects.exists())

    def test_details_limit_is_enforced_and_invalid_input_is_preserved(self):
        reason = Report.Reason.MISLEADING_INFORMATION
        response = self.client.post(
            self.report_url, {"reason": reason, "details": "x" * 5001}
        )
        self.assertEqual(response.status_code, 200)
        self.assertIn("details", response.context["form"].errors)
        self.assertContains(response, "x" * 5001)
        self.assertContains(response, f'<option value="{reason}" selected>')
        self.assertFalse(Report.objects.exists())

        self.assertRedirects(
            self.client.post(
                self.report_url, {"reason": reason, "details": "x" * 5000}
            ),
            self.detail_url,
        )
        self.assertEqual(len(Report.objects.get().details), 5000)

    def test_event_title_and_invalid_form_details_are_escaped(self):
        self.event.title = '<script>alert("event")</script>'
        self.event.save(update_fields=["title"])
        details = '<script>alert("details")</script>'
        response = self.client.post(self.report_url, {"details": details})
        self.assertContains(response, "&lt;script&gt;")
        self.assertNotContains(response, self.event.title)
        self.assertNotContains(response, details)

    def test_reporter_target_snapshot_and_lifecycle_cannot_be_forged(self):
        other_event = Event.objects.create(guid="other", title="Other", content_hash="")
        response = self.client.post(
            self.report_url,
            {
                "reason": Report.Reason.OTHER,
                "reporter": self.host.pk,
                "event": other_event.pk,
                "event_title": "Forged title",
                "created_at": "2000-01-01T00:00:00Z",
                "resolved_at": "2000-01-01T00:00:00Z",
            },
        )
        self.assertRedirects(response, self.detail_url)
        report = Report.objects.get()
        self.assertEqual(report.reporter, self.reporter)
        self.assertEqual(report.event, self.event)
        self.assertEqual(report.event_title, self.event.title)
        self.assertGreater(report.created_at.year, 2000)
        self.assertIsNone(report.resolved_at)

    def test_existing_unresolved_report_redirects_get_and_post_without_changes(self):
        report = self.create_report()
        original = Report.objects.values().get(pk=report.pk)
        for method in (self.client.get, self.client.post):
            response = method(
                self.report_url,
                {"reason": Report.Reason.OTHER, "details": "Replacement"},
                follow=True,
            )
            self.assertRedirects(response, self.detail_url)
            self.assertContains(response, "You have already reported this event.")
            self.assertEqual(Report.objects.count(), 1)
            self.assertEqual(Report.objects.values().get(pk=report.pk), original)

    def test_resolution_allows_a_new_report_and_preserves_the_old_one(self):
        original = self.create_report(resolved_at=timezone.now())
        self.assertEqual(self.client.get(self.report_url).status_code, 200)
        response = self.client.post(self.report_url, {"reason": Report.Reason.OTHER})
        self.assertRedirects(response, self.detail_url)
        self.assertEqual(Report.objects.count(), 2)
        self.assertEqual(
            Report.objects.get(resolved_at=None).reason, Report.Reason.OTHER
        )
        original.refresh_from_db()
        self.assertEqual(original.details, "Original concern")
        self.assertIsNotNone(original.resolved_at)

    def test_different_users_can_report_the_same_event_including_its_host(self):
        self.create_report()
        self.client.force_login(self.host)
        self.assertRedirects(
            self.client.post(self.report_url, {"reason": Report.Reason.OTHER}),
            self.detail_url,
        )
        self.assertEqual(Report.objects.count(), 2)

    def test_hosted_imported_ended_and_cancelled_events_can_be_reported(self):
        past = timezone.now() - timedelta(days=2)
        variants = (
            {"host": self.host},
            {"host": None},
            {"status": Event.Status.DONE, "start_time": past},
            {"status": Event.Status.CANCELLED},
            {"start_time": past, "end_time": past + timedelta(hours=1)},
        )
        for index, fields in enumerate(variants):
            with self.subTest(fields=fields):
                event = Event.objects.create(
                    guid=f"reportable-{index}",
                    title="Reportable event",
                    content_hash="",
                    **fields,
                )
                report_url = reverse("event_report", args=[event.pk])
                detail_url = reverse("event_detail", args=[event.pk])
                self.assertContains(self.client.get(detail_url), f'href="{report_url}"')
                self.assertEqual(self.client.get(report_url).status_code, 200)
                self.assertRedirects(
                    self.client.post(report_url, {"reason": Report.Reason.OTHER}),
                    detail_url,
                )
                self.assertTrue(Report.objects.filter(event=event).exists())

    def test_one_user_can_report_different_events(self):
        self.create_report()
        event = Event.objects.create(guid="second", title="Second", content_hash="")
        response = self.client.post(
            reverse("event_report", args=[event.pk]), {"reason": Report.Reason.OTHER}
        )
        self.assertRedirects(response, reverse("event_detail", args=[event.pk]))
        self.assertEqual(Report.objects.filter(reporter=self.reporter).count(), 2)

    def test_login_required_for_get_and_post_and_returns_to_reporting_form(self):
        self.client.logout()
        for method in (self.client.get, self.client.post):
            response = method(self.report_url)
            self.assertRedirects(
                response, reverse("login") + "?next=" + self.report_url
            )
        self.assertFalse(Report.objects.exists())

    def test_missing_events_return_404(self):
        missing_url = reverse("event_report", args=[self.event.pk + 1000])
        for method in (self.client.get, self.client.post):
            self.assertEqual(method(missing_url).status_code, 404)
        self.assertFalse(Report.objects.exists())

    def test_other_http_methods_are_rejected(self):
        for method in ("put", "patch", "delete", "head", "options"):
            with self.subTest(method=method):
                self.assertEqual(
                    getattr(self.client, method)(self.report_url).status_code, 405
                )
        self.assertFalse(Report.objects.exists())

    def test_csrf_token_is_required_and_a_valid_token_allows_submission(self):
        client = Client(enforce_csrf_checks=True)
        client.force_login(self.reporter)
        data = {"reason": Report.Reason.SPAM_SCAM}
        self.assertEqual(client.post(self.report_url, data).status_code, 403)
        self.assertFalse(Report.objects.exists())
        page = client.get(self.report_url)
        self.assertContains(page, 'name="csrfmiddlewaretoken"')
        data["csrfmiddlewaretoken"] = client.cookies["csrftoken"].value
        self.assertRedirects(client.post(self.report_url, data), self.detail_url)
        self.assertEqual(Report.objects.count(), 1)


class ReportHistoryTests(ReportTestData, TestCase):
    """Reports preserve evidence and enforce unresolved-report uniqueness."""

    @classmethod
    def setUpTestData(cls):
        cls.create_data()

    def test_database_rejects_duplicate_unresolved_reports(self):
        report = self.create_report()
        with self.assertRaises(IntegrityError), transaction.atomic():
            self.create_report(reason=Report.Reason.OTHER)
        self.assertEqual(Report.objects.get(), report)

    def test_deleting_event_preserves_report_and_title_snapshot(self):
        report = self.create_report()
        self.event.title = "Changed title"
        self.event.save(update_fields=["title"])
        self.event.delete()
        report.refresh_from_db()
        self.assertIsNone(report.event_id)
        self.assertEqual(report.event_title, "Community Walk")
        self.assertEqual(report.reason, Report.Reason.SPAM_SCAM)
        self.assertEqual(report.details, "Original concern")
        self.assertIsNotNone(report.created_at)
        self.assertEqual(str(report), "Spam or scam — Community Walk")

    def test_deleting_reporter_preserves_report_and_clears_reference(self):
        report = self.create_report()
        self.reporter.delete()
        report.refresh_from_db()
        self.assertIsNone(report.reporter_id)
        self.assertEqual(report.event, self.event)
        self.assertEqual(report.details, "Original concern")

    def test_deleting_host_also_retains_reports_of_their_cascaded_events(self):
        report = self.create_report(reporter=self.host)
        self.host.delete()
        report.refresh_from_db()
        self.assertIsNone(report.reporter_id)
        self.assertIsNone(report.event_id)
        self.assertEqual(report.event_title, "Community Walk")


class ConcurrentReportTests(ReportTestData, TransactionTestCase):
    """Two simultaneous valid submissions produce one report and two redirects."""

    def setUp(self):
        self.create_data()

    def test_concurrent_submissions_keep_the_first_report(self):
        clients = [Client(), Client()]
        for client in clients:
            client.force_login(self.reporter)

        ready = Barrier(2, timeout=15)
        original_is_valid = EventReportForm.is_valid

        def validate_together(form):
            valid = original_is_valid(form)
            # Both requests have passed the existing-report check before saving.
            ready.wait()
            return valid

        def submit(item):
            client, details = item
            try:
                return client.post(
                    self.report_url,
                    {"reason": Report.Reason.SPAM_SCAM, "details": details},
                )
            finally:
                connections.close_all()

        with patch.object(EventReportForm, "is_valid", validate_together):
            with ThreadPoolExecutor(max_workers=2) as pool:
                responses = list(pool.map(submit, zip(clients, ("First", "Second"))))

        for response in responses:
            self.assertRedirects(
                response, self.detail_url, fetch_redirect_response=False
            )
        report = Report.objects.get()
        self.assertIn(report.details, ("First", "Second"))
        confirmations = [
            str(message)
            for response in responses
            for message in response.wsgi_request._messages
        ]
        self.assertCountEqual(
            confirmations,
            [
                "Your report has been submitted.",
                "You have already reported this event.",
            ],
        )
