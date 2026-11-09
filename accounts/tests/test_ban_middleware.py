"""Bans restrict submissions without changing stored content or authentication."""

from datetime import timedelta
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from django.contrib.auth import SESSION_KEY
from django.contrib.auth.tokens import default_token_generator
from django.core import mail
from django.core.files.base import ContentFile
from django.http import JsonResponse
from django.test import Client, TestCase, override_settings
from django.urls import include, path, reverse
from django.utils.encoding import force_bytes
from django.utils.http import urlsafe_base64_encode
from django.utils import timezone

from accounts.ban_responses import BAN_MESSAGE
from accounts.ban_services import apply_ban, lift_ban
from accounts.models import ContactPermission, User, UserBan
from accounts.tests.test_google_sso import GOOGLE_TEST_PROVIDERS
from accounts.tests.test_settings import photo_upload
from chatbot.models import ChatLog, Inquiry
from config.urls import urlpatterns as site_urls
from events.models import (
    ContactRequest,
    Conversation,
    DirectMessage,
    Event,
    EventInterest,
    EventPost,
    EventReply,
    Notification,
)
from events.tests.test_hosting import listing_data
from reports.models import Report

PASSWORD = "Tr1cky-Passphrase!"


def future_submission(request):
    """Stand in for an endpoint added after bans were implemented."""
    EventPost.objects.create(
        event_id=request.POST["event"], author=request.user, body="New endpoint"
    )
    return JsonResponse({"saved": True})


urlpatterns = [
    *site_urls,
    path("future/submit/", future_submission, name="future_submit"),
    path("accounts/login/extra/", future_submission, name="login_extra"),
    path(
        "future/auth/",
        include(([path("login/", future_submission, name="login")], "future")),
    ),
]


class BanMiddlewareTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.admin = User.objects.create_superuser("admin", "admin@example.com")
        cls.user = User.objects.create_user(
            "member", "member@example.com", PASSWORD, share_profile_with_attendees=True
        )
        cls.peer = User.objects.create_user(
            "peer",
            "peer@example.com",
            share_profile_with_attendees=True,
            contact_permission=ContactPermission.OPEN,
        )
        cls.friend = User.objects.create_user("friend", "friend@example.com")
        cls.event = Event.objects.create(
            guid=Event.HOSTED_GUID_PREFIX + "ban-test",
            host=cls.user,
            title="Community walk",
            start_time=timezone.now() + timedelta(days=30),
            content_hash="",
        )
        EventInterest.objects.create(event=cls.event, user=cls.user)
        EventInterest.objects.create(event=cls.event, user=cls.peer)
        ContactRequest.objects.create(
            event=cls.event,
            sender=cls.user,
            recipient=cls.friend,
            status=ContactRequest.Status.ACCEPTED,
        )
        cls.pending = ContactRequest.objects.create(
            event=cls.event, sender=cls.peer, recipient=cls.user
        )
        cls.thread, _ = Conversation.get_or_create_for_users(cls.user, cls.friend)
        cls.message = DirectMessage.objects.create(
            conversation=cls.thread, sender=cls.friend, body="Existing message"
        )
        cls.post = EventPost.objects.create(
            event=cls.event, author=cls.peer, body="Existing comment"
        )
        cls.reply = EventReply.objects.create(
            post=cls.post, author=cls.peer, body="Existing reply"
        )
        cls.inquiry = Inquiry.objects.create(
            user=cls.peer, event=cls.event, question="Where do we meet?"
        )

    def setUp(self):
        self.client.force_login(self.user)
        self.ban, _ = apply_ban(self.admin, self.user)
        self.media_root = self.enterContext(TemporaryDirectory())
        self.enterContext(override_settings(MEDIA_ROOT=self.media_root))

    def snapshot(self):
        """Detect edits and deletions as well as newly created content."""
        models = (
            User,
            UserBan,
            Event,
            EventInterest,
            EventPost,
            EventReply,
            ContactRequest,
            Conversation,
            DirectMessage,
            Notification,
            Report,
            Inquiry,
            ChatLog,
        )
        return {model: list(model.objects.order_by("pk").values()) for model in models}

    def blocked_submissions(self):
        report = {"reason": Report.Reason.OTHER, "details": "New report"}
        return [
            ("event_create", [], listing_data()),
            ("event_edit", [self.event.pk], listing_data()),
            ("event_cancel", [self.event.pk], {"message": "Cancelled"}),
            (
                "event_notify",
                [self.event.pk],
                {"subject": "Update", "message": "Broadcast"},
            ),
            ("create_event_post", [self.event.pk], {"body": "Comment"}),
            ("create_event_reply", [self.post.pk], {"body": "Reply"}),
            (
                "send_contact_request",
                [self.event.pk, self.peer.pk],
                {"message": "Introduction"},
            ),
            (
                "message_user",
                [self.peer.pk],
                {"event": self.event.pk, "message": "Introduction"},
            ),
            ("respond_to_contact_request", [self.pending.pk, "accept"], {}),
            ("respond_to_contact_request", [self.pending.pk, "decline"], {}),
            ("conversation", [self.thread.pk], {"body": "Private message"}),
            ("account_settings", [], {"bio": "New bio", "display_name": "New name"}),
            ("event_report", [self.event.pk], report),
            ("event_post_report", [self.post.pk], report),
            ("event_reply_report", [self.reply.pk], report),
            ("profile_report", [self.peer.username], report),
            ("conversation_report", [self.thread.pk], report),
            ("chatbot:answer_inquiry", [self.inquiry.pk], {"answer": "At the gate"}),
            (
                "toggle_event_interest",
                [self.event.pk],
                {"status": "want_to_attend"},
            ),
            (
                "register",
                [],
                {
                    "username": "another",
                    "email": "another@example.com",
                    "password1": PASSWORD,
                    "password2": PASSWORD,
                },
            ),
        ]

    def test_all_member_submissions_are_blocked_without_side_effects(self):
        for permanent in (False, True):
            if permanent:
                lift_ban(self.admin, self.ban)
                self.ban, _ = apply_ban(self.admin, self.user)
                self.assertIsNone(self.ban.expires_at)
            before = self.snapshot()
            for route, args, data in self.blocked_submissions():
                with self.subTest(permanent=permanent, route=route, args=args):
                    response = self.client.post(reverse(route, args=args), data)
                    self.assertContains(response, BAN_MESSAGE, status_code=403)
                    self.assertTemplateUsed(response, "accounts/banned.html")
                    self.assertEqual(self.snapshot(), before)
                    self.assertEqual(mail.outbox, [])
                    self.assertEqual(list(Path(self.media_root).rglob("*")), [])

    def test_profile_upload_and_photo_removal_do_not_change_storage(self):
        self.user.profile_photo.save("existing.png", ContentFile(b"Original photo"))
        original_path = Path(self.user.profile_photo.path)
        original_name = self.user.profile_photo.name
        for data in (
            {"profile_photo": photo_upload(), "bio": "New bio"},
            {"profile_photo-clear": "on"},
        ):
            with self.subTest(fields=list(data)):
                response = self.client.post(reverse("account_settings"), data)
                self.assertEqual(response.status_code, 403)
                self.user.refresh_from_db()
                self.assertEqual(self.user.profile_photo.name, original_name)
                self.assertEqual(original_path.read_bytes(), b"Original photo")
                self.assertEqual(
                    [p for p in Path(self.media_root).rglob("*") if p.is_file()],
                    [original_path],
                )

    @override_settings(CHATBOT_ENABLED=True)
    def test_assistant_submissions_are_rejected_before_external_calls_or_logs(self):
        before = self.snapshot()
        session = self.client.session
        session["chatbot_history"] = [{"role": "user", "text": "Existing question"}]
        session.save()
        with patch("chatbot.views.gemini") as gemini, patch(
            "chatbot.views.reply"
        ) as reply:
            for route in ("message", "live", "live_tool", "live_turn", "reset"):
                with self.subTest(route=route):
                    response = self.client.post(
                        reverse(f"chatbot:{route}"),
                        {"message": "New question", "reply": "New answer"},
                        content_type="application/json",
                    )
                    self.assertEqual(response.status_code, 403)
                    self.assertEqual(
                        response.json(), {"error": BAN_MESSAGE, "code": "user_banned"}
                    )
            response = self.client.post(
                reverse("chatbot:voice"),
                HTTP_ACCEPT="application/json",
            )
            self.assertEqual(response.status_code, 403)
            self.assertEqual(response.json()["code"], "user_banned")
            self.assertEqual(gemini.mock_calls, [])
            reply.assert_not_called()
        self.assertEqual(self.snapshot(), before)
        self.assertEqual(
            self.client.session["chatbot_history"], session["chatbot_history"]
        )
        self.assertEqual(mail.outbox, [])

    def test_json_and_ajax_requests_receive_machine_readable_errors(self):
        for headers in (
            {"content_type": "application/json"},
            {"HTTP_X_REQUESTED_WITH": "XMLHttpRequest"},
            {"HTTP_ACCEPT": "application/json"},
            {"HTTP_ACCEPT": "text/html;q=0.5, application/json;q=0.9"},
        ):
            with self.subTest(headers=headers):
                response = self.client.post(
                    reverse("conversation", args=[self.thread.pk]),
                    {"body": "Private message"},
                    **headers,
                )
                self.assertEqual(response.status_code, 403)
                self.assertEqual(response["Content-Type"], "application/json")
                self.assertEqual(
                    response.json(), {"error": BAN_MESSAGE, "code": "user_banned"}
                )
        self.assertEqual(DirectMessage.objects.count(), 1)

    def test_browser_accept_headers_receive_html_with_safe_navigation(self):
        for accept in ("*/*", "text/html", "text/html,application/json;q=0.5"):
            with self.subTest(accept=accept):
                response = self.client.post(
                    reverse("account_settings"), HTTP_ACCEPT=accept
                )
                self.assertContains(response, BAN_MESSAGE, status_code=403)
                self.assertEqual(response["Content-Type"], "text/html; charset=utf-8")
                self.assertContains(
                    response, f'href="{reverse("landing")}"', status_code=403
                )
                self.assertContains(
                    response, f'action="{reverse("logout")}"', status_code=403
                )

    @override_settings(ROOT_URLCONF=__name__)
    def test_new_endpoints_and_auth_like_paths_are_restricted_by_default(self):
        before = self.snapshot()
        for route in ("future_submit", "login_extra", "future:login"):
            with self.subTest(route=route):
                response = self.client.post(reverse(route), {"event": self.event.pk})
                self.assertEqual(response.status_code, 403)
                self.assertEqual(self.snapshot(), before)

    @override_settings(ROOT_URLCONF=__name__)
    def test_unsafe_methods_cannot_bypass_the_default_restriction(self):
        before = self.snapshot()
        for method in ("POST", "PUT", "PATCH", "DELETE", "CUSTOM"):
            with self.subTest(method=method):
                response = self.client.generic(method, reverse("future_submit"))
                self.assertEqual(response.status_code, 403)
        self.assertEqual(self.snapshot(), before)

    def test_other_account_submissions_are_not_authentication_exceptions(self):
        for route in (
            "account_email",
            "account_change_password",
            "account_set_password",
            "account_signup",
            "socialaccount_connections",
        ):
            with self.subTest(route=route):
                response = self.client.post(reverse(route))
                self.assertContains(response, BAN_MESSAGE, status_code=403)

    def test_ban_created_after_login_applies_to_the_existing_session(self):
        self.assertEqual(int(self.client.session[SESSION_KEY]), self.user.pk)
        response = self.client.post(
            reverse("create_event_post", args=[self.event.pk]), {"body": "Blocked"}
        )
        self.assertEqual(response.status_code, 403)
        self.assertEqual(int(self.client.session[SESSION_KEY]), self.user.pk)

    def test_expiration_and_lifting_restore_submissions_without_relogin(self):
        url = reverse("create_event_post", args=[self.event.pk])
        # Advance the ban deadline without also expiring the 48-hour session.
        UserBan.objects.filter(pk=self.ban.pk).update(expires_at=timezone.now())
        response = self.client.post(url, {"body": "After expiration"})
        self.assertEqual(response.status_code, 302)
        self.assertTrue(EventPost.objects.filter(body="After expiration").exists())

        lift_ban(self.admin, self.ban)
        permanent, _ = apply_ban(self.admin, self.user)
        self.assertEqual(
            self.client.post(url, {"body": "Blocked again"}).status_code, 403
        )
        lift_ban(self.admin, permanent)
        response = self.client.post(url, {"body": "After lifting"})
        self.assertEqual(response.status_code, 302)
        self.assertTrue(EventPost.objects.filter(body="After lifting").exists())
        self.assertEqual(int(self.client.session[SESSION_KEY]), self.user.pk)

    def test_anonymous_requests_keep_existing_login_redirects(self):
        self.client.logout()
        response = self.client.post(
            reverse("create_event_post", args=[self.event.pk]), {"body": "Anonymous"}
        )
        self.assertEqual(response.status_code, 302)
        self.assertIn(reverse("login"), response.url)

    def test_unbanned_users_can_still_submit(self):
        self.client.force_login(self.peer)
        response = self.client.post(
            reverse("create_event_post", args=[self.event.pk]), {"body": "Allowed"}
        )
        self.assertEqual(response.status_code, 302)
        self.assertTrue(
            EventPost.objects.filter(author=self.peer, body="Allowed").exists()
        )

    def test_browsing_and_message_read_tracking_remain_available(self):
        for route, args in (
            ("landing", []),
            ("search", []),
            ("event_detail", [self.event.pk]),
            ("public_profile", [self.peer.username]),
            ("account_settings", []),
            ("my_events", []),
            ("inbox", []),
            ("notifications", []),
            ("conversation_updates", [self.thread.pk]),
            ("conversation", [self.thread.pk]),
            ("unread_message_count", []),
        ):
            with self.subTest(route=route):
                self.assertEqual(
                    self.client.get(reverse(route, args=args)).status_code, 200
                )
        self.message.refresh_from_db()
        self.assertIsNotNone(self.message.read_at)
        self.assertEqual(self.client.head(reverse("landing")).status_code, 200)
        self.assertEqual(
            self.client.options(reverse("account_settings")).status_code, 200
        )

    def test_interested_toggle_remains_available_but_registration_does_not(self):
        url = reverse("toggle_event_interest", args=[self.event.pk])
        for data in ({}, {"status": EventInterest.Status.INTERESTED}):
            with self.subTest(data=data):
                response = self.client.post(url, data)
                self.assertEqual(response.status_code, 302)
        self.assertEqual(
            EventInterest.objects.get(user=self.user, event=self.event).status,
            EventInterest.Status.INTERESTED,
        )
        for data in (
            {"status": "want_to_attend"},
            {"status": "invalid"},
            {"status": ["interested", "want_to_attend"]},
        ):
            with self.subTest(data=data):
                response = self.client.post(url, data)
                self.assertEqual(response.status_code, 403)
        self.assertEqual(
            EventInterest.objects.get(user=self.user, event=self.event).status,
            EventInterest.Status.INTERESTED,
        )
        self.assertEqual(self.client.put(url).status_code, 403)

    def test_csrf_remains_required_for_interest_and_logout_exceptions(self):
        client = Client(enforce_csrf_checks=True)
        client.force_login(self.user)
        url = reverse("toggle_event_interest", args=[self.event.pk])
        self.assertEqual(client.post(url).status_code, 403)
        self.assertEqual(client.post(reverse("logout")).status_code, 403)
        client.get(reverse("event_detail", args=[self.event.pk]))
        token = client.cookies["csrftoken"].value
        response = client.post(url, HTTP_X_CSRFTOKEN=token)
        self.assertEqual(response.status_code, 302)
        response = client.post(reverse("logout"), HTTP_X_CSRFTOKEN=token)
        self.assertEqual(response.status_code, 302)
        self.assertNotIn(SESSION_KEY, client.session)

    def test_banned_users_can_log_out_and_log_in_again(self):
        self.assertEqual(self.client.post(reverse("logout")).status_code, 302)
        self.assertNotIn(SESSION_KEY, self.client.session)
        response = self.client.post(
            reverse("login"), {"username": self.user.username, "password": PASSWORD}
        )
        self.assertEqual(response.status_code, 302)
        self.assertEqual(int(self.client.session[SESSION_KEY]), self.user.pk)
        response = self.client.post(reverse("login"))
        self.assertEqual(response.status_code, 302)

    def test_password_recovery_remains_available_while_authenticated_and_banned(self):
        response = self.client.post(
            reverse("password_reset"), {"email": self.user.email}
        )
        self.assertEqual(response.status_code, 302)
        self.assertEqual(len(mail.outbox), 1)
        response = self.client.post(
            reverse("password_reset_confirm", args=["invalid", "invalid"])
        )
        self.assertEqual(response.status_code, 200)
        self.assertTemplateUsed(response, "accounts/password_reset_confirm.html")

    def test_banned_user_can_complete_password_recovery(self):
        uid = urlsafe_base64_encode(force_bytes(self.user.pk))
        token = default_token_generator.make_token(self.user)
        response = self.client.get(reverse("password_reset_confirm", args=[uid, token]))
        self.assertEqual(response.status_code, 302)

        new_password = "Fresh-Passphrase-42"
        response = self.client.post(
            response.url,
            {"new_password1": new_password, "new_password2": new_password},
        )

        self.assertRedirects(response, reverse("password_reset_complete"))
        self.user.refresh_from_db()
        self.assertTrue(self.user.check_password(new_password))
        self.assertIsNone(self.ban.lifted_at)

    @override_settings(
        GOOGLE_OAUTH_ENABLED=True, SOCIALACCOUNT_PROVIDERS=GOOGLE_TEST_PROVIDERS
    )
    def test_google_authentication_is_allowed_but_unrelated_account_posts_are_not(self):
        response = self.client.post(reverse("google_login"))
        self.assertEqual(response.status_code, 302)
        self.assertIn("accounts.google.com", response.url)
        response = self.client.post(reverse("google_callback"))
        self.assertNotEqual(response.status_code, 403)
        response = self.client.post(reverse("account_email"))
        self.assertContains(response, BAN_MESSAGE, status_code=403)
