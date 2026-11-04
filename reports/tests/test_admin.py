"""Moderation queue grouping, authorization, links, and resolution lifecycle."""

from datetime import timedelta

from django.contrib.auth.models import Permission
from django.db import connection
from django.test import Client, TestCase
from django.test.utils import CaptureQueriesContext
from django.urls import reverse
from django.utils import timezone

from accounts.models import User
from events.models import (
    ContactRequest,
    Conversation,
    DirectMessage,
    Event,
    EventPost,
    EventReply,
)
from reports.models import Report

NOTICE = (
    "Conversations may be reviewed by administrators "
    "for moderation purposes if reported."
)


class ReportAdminTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.admin = User.objects.create_superuser(
            "moderator", "mod@example.com", "pass"
        )
        cls.viewer = User.objects.create_user(
            "viewer", "viewer@example.com", is_staff=True
        )
        cls.viewer.user_permissions.add(
            Permission.objects.get(
                codename="view_report", content_type__app_label="reports"
            )
        )
        cls.reporter = User.objects.create_user("reporter", "reporter@example.com")
        cls.subject = User.objects.create_user("subject", "subject@example.com")
        cls.event = Event.objects.create(guid="queue-event", title="Park walk")
        cls.post = EventPost.objects.create(
            event=cls.event, author=cls.subject, body="Reported comment"
        )
        cls.reply = EventReply.objects.create(
            post=cls.post, author=cls.subject, body="Reported reply"
        )
        cls.thread, _ = Conversation.get_or_create_for_users(cls.reporter, cls.subject)
        ContactRequest.objects.create(
            event=cls.event,
            sender=cls.reporter,
            recipient=cls.subject,
            status=ContactRequest.Status.ACCEPTED,
        )
        cls.message = DirectMessage.objects.create(
            conversation=cls.thread, sender=cls.subject, body="PRIVATE CHAT TEXT"
        )
        cls.url = reverse("admin:reports_report_changelist")

    def setUp(self):
        self.client.force_login(self.admin)

    def report(self, kind=Report.Kind.EVENT, **kwargs):
        fields = {
            "event": self.event,
            "event_title": self.event.title,
            "reporter": self.reporter,
            "reason": Report.Reason.OTHER,
            "details": "Report explanation",
            "kind": kind,
        }
        if kind == Report.Kind.POST:
            fields["reported_post"] = self.post
        elif kind == Report.Kind.REPLY:
            fields["reported_reply"] = self.reply
        elif kind in (Report.Kind.USER, Report.Kind.PROFILE):
            fields.update(event=None, reported_user=self.subject)
        fields.update(kwargs)
        return Report.objects.create(**fields)

    def groups(self, **params):
        response = self.client.get(self.url, params)
        self.assertEqual(response.status_code, 200)
        return response.context["groups"]

    def action(self, report, bulk=False, **params):
        name = "resolve_all" if bulk else "resolve"
        return self.client.post(
            reverse(f"admin:reports_report_{name}", args=[report.pk]), params
        )

    def test_all_kinds_group_by_primary_target_and_link_to_management(self):
        first = self.report()
        self.report(reporter=self.subject)
        for kind in ("post", "reply", "user", "profile"):
            self.report(kind)
        groups = self.groups()
        self.assertEqual(len(groups), 5)
        event_group = next(g for g in groups if g["kind"] == "event")
        self.assertEqual(event_group["report_count"], 2)
        self.assertEqual(len(event_group["reports"]), 2)
        self.assertIn(first, event_group["reports"])
        destinations = {
            "event": (
                "events_event",
                self.event.pk,
                reverse("event_detail", args=[self.event.pk]),
            ),
            "post": ("events_eventpost", self.post.pk, f"#comment-{self.post.pk}"),
            "reply": ("events_eventreply", self.reply.pk, f"#reply-{self.reply.pk}"),
            "user": (
                "accounts_user",
                self.subject.pk,
                reverse("public_profile", args=[self.subject.username]),
            ),
            "profile": (
                "accounts_user",
                self.subject.pk,
                reverse("public_profile", args=[self.subject.username]),
            ),
        }
        for group in groups:
            model, pk, public = destinations[group["kind"]]
            self.assertEqual(
                group["admin_url"], reverse(f"admin:{model}_change", args=[pk])
            )
            self.assertIn(public, group["public_url"])
            self.assertEqual(self.client.get(group["admin_url"]).status_code, 200)
        other = EventPost.objects.create(
            event=self.event, author=self.subject, body="Other"
        )
        self.report("post", reported_post=other)
        self.assertEqual(len(self.groups()), 6)

    def test_status_and_sorting_use_matching_reports(self):
        old = self.report()
        self.report(reporter=self.subject)
        other = Event.objects.create(guid="other", title="Other")
        recent = self.report(event=other)
        Report.objects.filter(pk=old.pk).update(
            created_at=timezone.now() - timedelta(days=3)
        )
        self.assertEqual(self.groups(sort="newest")[0]["target_id"], other.pk)
        self.assertEqual(self.groups(sort="oldest")[0]["target_id"], self.event.pk)
        self.assertEqual(self.groups(sort="most")[0]["report_count"], 2)
        self.assertEqual(self.groups(sort="fewest")[0]["report_count"], 1)
        self.action(recent)
        self.assertEqual(len(self.groups()), 1)
        self.assertEqual(self.groups(status="resolved")[0]["target_id"], other.pk)
        self.assertEqual(len(self.groups(status="all")), 2)
        self.assertEqual(len(self.groups(status="invalid", sort="invalid")), 1)

    def test_pagination_is_by_group_with_stable_ties(self):
        timestamp = timezone.now()
        for index in range(27):
            event = Event.objects.create(guid=f"page-{index}", title=f"Event {index}")
            self.report(event=event)
        Report.objects.update(created_at=timestamp)
        first = self.groups()
        second = self.groups(page=2)
        self.assertEqual(len(first), 25)
        self.assertEqual(len(second), 2)
        keys = [g["target_id"] for g in first + second]
        self.assertEqual(keys, sorted(set(keys)))

    def test_deleted_targets_are_singletons_and_deleted_users_are_safe(self):
        first = self.report()
        second = self.report(reporter=self.subject)
        self.event.delete()
        self.reporter.delete()
        groups = self.groups()
        self.assertEqual(len(groups), 2)
        self.assertTrue(
            all(g["summary"] == "Deleted target — Park walk" for g in groups)
        )
        self.assertTrue(
            all(g["admin_url"] is None and g["public_url"] is None for g in groups)
        )
        self.action(first, bulk=True)
        second.refresh_from_db()
        self.assertIsNone(second.resolved_at)
        self.assertContains(
            self.client.get(self.url, {"status": "all"}), "Deleted user"
        )

    def test_resolution_records_first_administrator_and_preserves_timestamp(self):
        report = self.report()
        response = self.action(report)
        self.assertEqual(response.status_code, 302)
        report.refresh_from_db()
        self.assertEqual(report.resolved_by, self.admin)
        original_time = report.resolved_at
        self.viewer.user_permissions.add(
            Permission.objects.get(codename="change_report")
        )
        self.client.force_login(self.viewer)
        self.action(report)
        report.refresh_from_db()
        self.assertEqual(report.resolved_by, self.admin)
        self.assertEqual(report.resolved_at, original_time)
        self.admin.delete()
        report.refresh_from_db()
        self.assertIsNone(report.resolved_by)
        self.assertEqual(report.resolved_at, original_time)
        self.assertContains(
            self.client.get(self.url, {"status": "resolved"}), "Unknown"
        )

    def test_bulk_only_resolves_exact_target_kind_and_includes_new_reports(self):
        report = self.report("user")
        second = self.report("user", reporter=self.subject)
        profile = self.report("profile")
        event = self.report()
        self.action(report)
        report.refresh_from_db()
        original_time = report.resolved_at
        newer = self.report("user")
        self.action(report, bulk=True)
        for item in (second, newer):
            item.refresh_from_db()
            self.assertEqual(item.resolved_by, self.admin)
            self.assertIsNotNone(item.resolved_at)
        for item in (profile, event):
            item.refresh_from_db()
            self.assertIsNone(item.resolved_at)
        report.refresh_from_db()
        self.assertEqual(report.resolved_at, original_time)

    def test_permissions_methods_csrf_and_no_generic_editing(self):
        report = self.report()
        action = reverse("admin:reports_report_resolve", args=[report.pk])
        self.assertEqual(self.client.get(action).status_code, 405)
        csrf_client = Client(enforce_csrf_checks=True)
        csrf_client.force_login(self.admin)
        self.assertEqual(csrf_client.post(action).status_code, 403)
        self.client.force_login(self.viewer)
        response = self.client.get(self.url)
        self.assertContains(response, "Report explanation")
        self.assertNotContains(response, "Manage event")
        self.assertNotContains(response, "Resolve report")
        self.assertEqual(self.action(report).status_code, 403)
        self.assertEqual(self.action(report, bulk=True).status_code, 403)
        self.client.force_login(self.reporter)
        self.assertEqual(self.client.get(self.url).status_code, 302)
        self.subject.is_staff = True
        self.subject.save()
        self.client.force_login(self.subject)
        self.assertEqual(self.client.get(self.url).status_code, 403)
        self.client.force_login(self.admin)
        self.assertEqual(
            self.client.get(reverse("admin:reports_report_add")).status_code, 403
        )
        self.client.post(
            reverse("admin:reports_report_change", args=[report.pk]),
            {"details": "Changed"},
        )
        report.refresh_from_db()
        self.assertEqual(report.details, "Report explanation")

    def test_escape_chat_privacy_and_notices(self):
        report = self.report(
            "user",
            conversation=self.thread,
            context_message=self.message,
            details="<script>alert('unsafe')</script>",
        )
        response = self.client.get(self.url)
        self.assertContains(response, "&lt;script&gt;")
        self.assertNotContains(response, "<script>alert")
        self.assertNotContains(response, self.message.body)
        self.assertNotContains(response, reverse("conversation", args=[self.thread.pk]))
        self.client.force_login(self.reporter)
        self.assertContains(
            self.client.get(reverse("conversation", args=[self.thread.pk])), NOTICE
        )
        report.delete()
        self.assertContains(
            self.client.get(reverse("conversation_report", args=[self.thread.pk])),
            NOTICE,
        )
        self.assertNotContains(
            self.client.get(reverse("event_report", args=[self.event.pk])), NOTICE
        )
        detail = self.client.get(reverse("event_detail", args=[self.event.pk]))
        self.assertContains(detail, f'id="comment-{self.post.pk}"')
        self.assertContains(detail, f'id="reply-{self.reply.pk}"')
        # The profile route still enforces the user's sharing preference.
        self.assertContains(
            self.client.get(reverse("public_profile", args=[self.subject.username])),
            "Profile Unavailable",
        )

    def test_queries_do_not_grow_with_group_count(self):
        self.report()
        with CaptureQueriesContext(connection) as initial:
            self.client.get(self.url)
        for index in range(5):
            event = Event.objects.create(guid=f"query-{index}", title="Extra")
            self.report(event=event)
        with CaptureQueriesContext(connection) as expanded:
            self.client.get(self.url)
        self.assertEqual(len(initial), len(expanded))

    def test_empty_feed_and_action_redirect_filters(self):
        self.assertContains(self.client.get(self.url), "No reports match this filter.")
        report = self.report()
        url = reverse("admin:reports_report_resolve", args=[report.pk])
        response = self.client.post(url + "?status=all&sort=most&page=2")
        self.assertEqual(response.url, self.url + "?status=all&sort=most&page=2")
        self.assertEqual(
            self.client.post(
                reverse("admin:reports_report_resolve", args=[999999])
            ).status_code,
            404,
        )
