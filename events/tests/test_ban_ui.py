"""Community pages keep readable content and only offer permitted submissions."""

from datetime import timedelta
from html.parser import HTMLParser

from django.test import TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from accounts.ban_responses import BAN_MESSAGE
from accounts.ban_services import apply_ban, lift_ban
from accounts.models import User, UserBan
from chatbot.models import Inquiry
from events.models import (
    ContactRequest,
    Conversation,
    DirectMessage,
    Event,
    EventInterest,
    EventPost,
    EventReply,
    Notification,
    Registration,
)


class Controls(HTMLParser):
    def __init__(self, response):
        super().__init__()
        self.buttons = []
        self.forms = []
        self.feed(response.content.decode())

    def handle_starttag(self, tag, attrs):
        if tag == "button":
            self.buttons.append(dict(attrs))
        if tag == "form":
            self.forms.append(dict(attrs))


@override_settings(CHATBOT_ENABLED=True)
class BanUITests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.admin = User.objects.create_superuser("admin", "admin@example.com")
        cls.user = User.objects.create_user(
            "member", "member@example.com", share_profile_with_attendees=True
        )
        cls.peer = User.objects.create_user(
            "peer", "peer@example.com", share_profile_with_attendees=True
        )
        cls.friend = User.objects.create_user(
            "friend", "friend@example.com", share_profile_with_attendees=True
        )
        cls.event = Event.objects.create(
            guid="ban-ui",
            host=cls.user,
            title="Community walk",
            start_time=timezone.now() + timedelta(days=30),
        )
        cls.ended = Event.objects.create(
            guid="ban-ui-ended",
            host=cls.user,
            title="Earlier walk",
            status=Event.Status.DONE,
        )
        cls.cancelled = Event.objects.create(
            guid="ban-ui-cancelled",
            host=cls.user,
            title="Cancelled walk",
            status=Event.Status.CANCELLED,
        )
        for member in (cls.user, cls.peer):
            EventInterest.objects.create(event=cls.event, user=member)
        cls.registration = Registration.objects.create(event=cls.event, user=cls.friend)
        cls.attending_event = Event.objects.create(
            guid="ban-ui-attending",
            host=cls.peer,
            title="Attending walk",
            start_time=timezone.now() + timedelta(days=30),
        )
        Registration.objects.create(event=cls.attending_event, user=cls.user)
        cls.post = EventPost.objects.create(
            event=cls.event, author=cls.peer, body="Existing discussion"
        )
        cls.reply = EventReply.objects.create(
            post=cls.post, author=cls.friend, body="Existing reply"
        )
        cls.pending = ContactRequest.objects.create(
            event=cls.event,
            sender=cls.peer,
            recipient=cls.user,
            message="Pending introduction",
        )
        cls.accepted = ContactRequest.objects.create(
            event=cls.event,
            sender=cls.friend,
            recipient=cls.user,
            status=ContactRequest.Status.ACCEPTED,
        )
        for contact in (cls.pending, cls.accepted):
            Notification.objects.create(
                recipient=cls.user,
                event=cls.event,
                contact_request=contact,
                subject="Contact request",
                body="An introduction",
            )
        cls.thread, _ = Conversation.get_or_create_for_users(cls.user, cls.friend)
        cls.message = DirectMessage.objects.create(
            conversation=cls.thread, sender=cls.friend, body="Existing private message"
        )
        cls.inquiry = Inquiry.objects.create(
            user=cls.peer, event=cls.event, question="Where should we meet?"
        )
        Inquiry.objects.create(
            user=cls.peer,
            event=cls.event,
            question="Is it accessible?",
            status=Inquiry.Status.ANSWERED,
            answer="Meet at the accessible entrance",
            answered_by=cls.user,
            answered_at=timezone.now(),
        )

    def setUp(self):
        self.client.force_login(self.user)
        self.ban, _ = apply_ban(self.admin, self.user)

    def test_event_separates_interest_from_other_interactions_for_both_bans(self):
        for permanent in (False, True):
            if permanent:
                lift_ban(self.admin, self.ban)
                self.ban, _ = apply_ban(self.admin, self.user)
            with self.subTest(permanent=permanent):
                response = self.client.get(
                    reverse("event_detail", args=[self.event.pk])
                )
                self.assertTrue(response.context["can_mark_interest"])
                self.assertFalse(response.context["can_interact"])
                self.assertFalse(response.context["can_manage"])
                self.assertFalse(response.context["can_register"])
                controls = Controls(response)
                self.assertTrue(
                    any(b.get("value") == "interested" for b in controls.buttons)
                )
                self.assertFalse(
                    any(b.get("value") == "want_to_attend" for b in controls.buttons)
                )
                self.assertContains(response, "Existing discussion")
                self.assertContains(response, "Existing reply")
                self.assertContains(response, "Pending introduction")
                self.assertContains(response, "peer's profile preview")
                self.assertContains(response, "friend's profile preview")
                self.assertContains(
                    response, reverse("public_profile", args=[self.peer.username])
                )
                for route, args in (
                    ("event_edit", [self.event.pk]),
                    ("event_cancel", [self.event.pk]),
                    ("event_notify", [self.event.pk]),
                    ("create_event_post", [self.event.pk]),
                    ("create_event_reply", [self.post.pk]),
                    ("send_contact_request", [self.event.pk, self.peer.pk]),
                    ("respond_to_contact_request", [self.pending.pk, "accept"]),
                    ("respond_to_contact_request", [self.pending.pk, "decline"]),
                    ("event_report", [self.event.pk]),
                    ("event_post_report", [self.post.pk]),
                    ("event_reply_report", [self.reply.pk]),
                    ("event_register", [self.event.pk]),
                    ("event_cancel_registration", [self.event.pk]),
                ):
                    url = reverse(route, args=args)
                    self.assertNotContains(response, f'action="{url}"')
                    self.assertNotContains(response, f'href="{url}"')
                self.assertNotContains(response, 'class="report-menu"')
                self.assertNotContains(response, 'id="parks-assistant"')
                self.assertContains(response, BAN_MESSAGE)
        self.assertEqual(
            EventInterest.objects.get(user=self.user).status,
            EventInterest.Status.INTERESTED,
        )
        self.assertTrue(Registration.objects.filter(pk=self.registration.pk).exists())

    def test_ended_and_cancelled_events_have_no_interest_controls(self):
        for event in (self.ended, self.cancelled):
            response = self.client.get(reverse("event_detail", args=[event.pk]))
            self.assertFalse(response.context["can_mark_interest"])
            self.assertFalse(Controls(response).forms)
            self.assertContains(response, event.title)

    def test_notifications_keep_content_and_existing_conversation_link(self):
        response = self.client.get(reverse("notifications"))
        self.assertContains(response, "An introduction")
        self.assertContains(response, "Status: Pending")
        self.assertContains(
            response, f'href="{reverse("message_user", args=[self.friend.pk])}"'
        )
        self.assertFalse(Controls(response).forms)
        self.pending.refresh_from_db()
        self.assertEqual(self.pending.status, ContactRequest.Status.PENDING)
        self.assertIsNone(
            Notification.objects.get(contact_request=self.pending).read_at
        )

    def test_hosted_event_list_keeps_events_and_removes_edit_controls(self):
        response = self.client.get(reverse("my_events"))
        for event in (self.event, self.ended, self.cancelled):
            self.assertContains(response, event.title)
            self.assertContains(
                response, f'href="{reverse("event_detail", args=[event.pk])}"'
            )
        self.assertNotContains(response, f'href="{reverse("event_create")}"')
        self.assertNotContains(
            response, f'href="{reverse("event_edit", args=[self.event.pk])}"'
        )
        self.assertNotContains(
            response, f'href="{reverse("event_cancel", args=[self.event.pk])}"'
        )
        self.assertContains(response, "View attendee questions")
        self.assertContains(response, 'id="hosted-tab"')
        self.assertContains(response, 'id="attended-tab"')
        self.assertContains(response, 'id="hosted-panel"')
        self.assertContains(response, 'id="attended-panel"')
        self.assertContains(response, self.attending_event.title)
        self.assertContains(
            response, reverse("event_detail", args=[self.attending_event.pk])
        )

    def test_inquiry_questions_and_answers_stay_readable_without_answer_form(self):
        response = self.client.get(reverse("chatbot:inquiries"))
        self.assertContains(response, self.inquiry.question)
        self.assertContains(response, "Meet at the accessible entrance")
        self.assertContains(response, "Answering is disabled")
        self.assertFalse(Controls(response).forms)

    def test_conversation_preserves_history_polling_and_read_tracking_without_composer(
        self,
    ):
        response = self.client.get(reverse("conversation", args=[self.thread.pk]))
        self.assertContains(response, self.message.body)
        self.assertContains(
            response, reverse("conversation_updates", args=[self.thread.pk])
        )
        self.assertContains(response, 'src="/static/events/conversation.js"')
        self.assertContains(response, f'data-current-user-id="{self.user.pk}"')
        self.assertFalse(Controls(response).forms)
        self.assertNotContains(response, 'class="report-menu"')
        self.assertContains(response, BAN_MESSAGE)
        self.message.refresh_from_db()
        self.assertIsNotNone(self.message.read_at)
        self.assertEqual(
            self.client.get(
                reverse("conversation_updates", args=[self.thread.pk])
            ).status_code,
            200,
        )

    def test_navigation_profiles_and_assistant_do_not_offer_blocked_actions(self):
        for url in (
            reverse("landing"),
            reverse("archived_events"),
            reverse("public_profile", args=[self.friend.username]),
            reverse("inbox"),
            reverse("account_settings"),
        ):
            with self.subTest(url=url):
                response = self.client.get(url)
                self.assertEqual(response.status_code, 200)
                self.assertNotContains(response, f'href="{reverse("event_create")}"')
                self.assertNotContains(response, 'id="parks-assistant"')
                self.assertNotContains(response, 'class="report-menu"')
        self.assertNotContains(
            self.client.get(reverse("public_profile", args=[self.friend.username])),
            "Send Message",
        )

    def test_direct_gets_to_submission_pages_explain_ban_without_forms(self):
        for route, args in (
            ("event_create", []),
            ("event_edit", [self.event.pk]),
            ("event_cancel", [self.event.pk]),
            ("event_report", [self.event.pk]),
            ("event_post_report", [self.post.pk]),
            ("event_reply_report", [self.reply.pk]),
            ("profile_report", [self.friend.username]),
            ("conversation_report", [self.thread.pk]),
            ("message_user", [self.peer.pk]),
        ):
            with self.subTest(route=route):
                response = self.client.get(reverse(route, args=args))
                self.assertEqual(response.status_code, 200)
                self.assertContains(response, BAN_MESSAGE)
                self.assertFalse(Controls(response).forms)

    def test_unbanned_viewers_can_interact_with_a_banned_hosts_event(self):
        self.client.force_login(self.peer)
        response = self.client.get(reverse("event_detail", args=[self.event.pk]))
        self.assertTrue(response.context["can_interact"])
        self.assertTrue(response.context["can_mark_interest"])
        self.assertTrue(response.context["can_register"])
        self.assertContains(
            response, f'action="{reverse("event_register", args=[self.event.pk])}"'
        )
        self.assertContains(response, "Post Comment")
        self.assertContains(response, 'id="parks-assistant"')

    def test_banned_registered_users_keep_status_without_cancellation_controls(self):
        url = reverse("event_detail", args=[self.attending_event.pk])
        response = self.client.get(url)
        self.assertTrue(response.context["user_is_registered"])
        self.assertContains(response, "You are registered for this event.")
        cancel_url = reverse(
            "event_cancel_registration", args=[self.attending_event.pk]
        )
        self.assertNotContains(response, f'action="{cancel_url}"')
        self.assertNotContains(
            response,
            f'action="{reverse("event_register", args=[self.attending_event.pk])}"',
        )

    def test_expired_and_feed_cancelled_events_cannot_offer_interest_or_registration(
        self,
    ):
        for fields in (
            {
                "start_time": timezone.now() - timedelta(days=2),
                "end_time": timezone.now() - timedelta(days=1),
            },
            {"title": "CANCELLED Community walk"},
        ):
            with self.subTest(fields=fields):
                Event.objects.filter(pk=self.event.pk).update(**fields)
                response = self.client.get(
                    reverse("event_detail", args=[self.event.pk])
                )
                self.assertFalse(response.context["can_mark_interest"])
                self.assertFalse(response.context["can_register"])

    def test_expiry_and_lifting_restore_controls_on_next_get(self):
        for restore in ("expire", "lift"):
            with self.subTest(restore=restore):
                if restore == "expire":
                    UserBan.objects.filter(pk=self.ban.pk).update(
                        expires_at=timezone.now()
                    )
                else:
                    self.ban, _ = apply_ban(self.admin, self.user)
                    lift_ban(self.admin, self.ban)
                response = self.client.get(
                    reverse("event_detail", args=[self.event.pk])
                )
                self.assertTrue(response.context["can_interact"])
                self.assertTrue(response.context["can_manage"])
                self.assertContains(response, "Post Comment")
                self.assertContains(response, 'id="parks-assistant"')
                response = self.client.get(
                    reverse("conversation", args=[self.thread.pk])
                )
                self.assertContains(response, 'class="chat-composer"')
                self.assertContains(response, 'class="report-menu"')

    def test_anonymous_visitors_keep_login_prompts_and_assistant(self):
        self.client.logout()
        response = self.client.get(reverse("event_detail", args=[self.event.pk]))
        self.assertFalse(response.context["viewer_banned"])
        self.assertContains(response, "Log in to Contact")
        self.assertContains(response, 'id="parks-assistant"')
