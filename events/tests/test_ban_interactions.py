"""Ban restrictions preserve interests and browsing without creating content."""

from datetime import timedelta
from unittest.mock import Mock, patch

from django.core import mail
from django.core.exceptions import PermissionDenied
from django.test import RequestFactory, TestCase
from django.urls import reverse
from django.utils import timezone

from accounts.ban_responses import BAN_MESSAGE
from accounts.ban_services import apply_ban, lift_ban
from accounts.models import ContactPermission, User, UserBan
from chatbot.inquiries import answer_inquiry
from chatbot.models import Inquiry
from events.community_views import toggle_event_interest
from events.contact_services import create_contact_request
from events.models import (
    ContactRequest,
    Conversation,
    DirectMessage,
    Event,
    EventInterest,
    Notification,
    Registration,
)


class BanInteractionTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.admin = User.objects.create_superuser(
            username="admin", email="admin@example.com"
        )
        cls.user = User.objects.create_user(
            username="member",
            email="member@example.com",
            share_profile_with_attendees=True,
        )
        cls.peer = User.objects.create_user(
            username="peer",
            email="peer@example.com",
            share_profile_with_attendees=True,
        )
        cls.event = Event.objects.create(
            guid="ban-interactions",
            title="Community walk",
            host=cls.user,
            start_time=timezone.now() + timedelta(days=30),
            content_hash="",
        )
        EventInterest.objects.create(event=cls.event, user=cls.peer)
        cls.registration_event = Event.objects.create(
            guid="ban-registration",
            host=cls.peer,
            title="Registration walk",
            start_time=timezone.now() + timedelta(days=30),
        )
        cls.inquiry = Inquiry.objects.create(
            user=cls.peer,
            event=cls.event,
            question="Where do we meet?",
            language="es",
        )

    def setUp(self):
        self.ban, _ = apply_ban(self.admin, self.user)
        self.client.force_login(self.user)

    def make_indefinite(self):
        lift_ban(self.admin, self.ban)
        self.ban, _ = apply_ban(self.admin, self.user)
        self.assertIsNone(self.ban.expires_at)

    def set_interest(self, status):
        EventInterest.objects.filter(event=self.event, user=self.user).delete()
        if status is not None:
            EventInterest.objects.create(
                event=self.event, user=self.user, status=status
            )

    def accept_contact(self):
        return ContactRequest.objects.create(
            event=self.event,
            sender=self.peer,
            recipient=self.user,
            status=ContactRequest.Status.ACCEPTED,
        )

    def test_interested_transitions_with_default_and_explicit_status(self):
        url = reverse("toggle_event_interest", args=[self.event.pk])
        for permanent in (False, True):
            if permanent:
                self.make_indefinite()
            for initial in (None, *EventInterest.Status.values):
                for data in ({}, {"status": EventInterest.Status.INTERESTED}):
                    with self.subTest(permanent=permanent, initial=initial, data=data):
                        self.set_interest(initial)
                        response = self.client.post(url, data)
                        self.assertRedirects(
                            response, reverse("event_detail", args=[self.event.pk])
                        )
                        selection = EventInterest.objects.filter(
                            event=self.event, user=self.user
                        ).first()
                        if initial == EventInterest.Status.INTERESTED:
                            self.assertIsNone(selection)
                        else:
                            self.assertEqual(
                                selection.status, EventInterest.Status.INTERESTED
                            )
            self.assertEqual(ContactRequest.objects.count(), 0)
            self.assertEqual(Notification.objects.count(), 0)
            self.assertEqual(mail.outbox, [])

    def test_registration_cannot_be_added_or_removed_during_either_ban(self):
        url = reverse("event_register", args=[self.registration_event.pk])
        cancel_url = reverse(
            "event_cancel_registration", args=[self.registration_event.pk]
        )
        for permanent in (False, True):
            if permanent:
                self.make_indefinite()
            for initial in (None, EventInterest.Status.INTERESTED):
                with self.subTest(permanent=permanent, initial=initial):
                    self.set_interest(initial)
                    before = list(EventInterest.objects.order_by("pk").values())
                    Registration.objects.filter(
                        event=self.registration_event, user=self.user
                    ).delete()
                    response = self.client.post(
                        url,
                        HTTP_X_REQUESTED_WITH="XMLHttpRequest",
                    )
                    self.assertEqual(response.status_code, 403)
                    self.assertEqual(
                        response.json(), {"error": BAN_MESSAGE, "code": "user_banned"}
                    )
                    self.assertEqual(
                        list(EventInterest.objects.order_by("pk").values()), before
                    )
                    self.assertFalse(
                        Registration.objects.filter(
                            event=self.registration_event, user=self.user
                        ).exists()
                    )
                    registration = Registration.objects.create(
                        event=self.registration_event, user=self.user
                    )
                    response = self.client.post(
                        cancel_url, HTTP_X_REQUESTED_WITH="XMLHttpRequest"
                    )
                    self.assertEqual(response.status_code, 403)
                    self.assertEqual(
                        response.json(), {"error": BAN_MESSAGE, "code": "user_banned"}
                    )
                    self.assertTrue(
                        Registration.objects.filter(pk=registration.pk).exists()
                    )

    def test_interest_handler_enforces_ban_without_middleware(self):
        for status in ("want_to_attend", "invalid", ""):
            with self.subTest(status=status):
                request = RequestFactory().post("/", {"status": status})
                request.user = self.user
                response = toggle_event_interest(request, self.event.pk)
                self.assertEqual(response.status_code, 403)
                self.assertFalse(EventInterest.objects.filter(user=self.user).exists())

    def test_closed_events_still_reject_interest_changes(self):
        for status in (Event.Status.DONE, Event.Status.CANCELLED):
            with self.subTest(status=status):
                Event.objects.filter(pk=self.event.pk).update(status=status)
                response = self.client.post(
                    reverse("toggle_event_interest", args=[self.event.pk])
                )
                self.assertEqual(response.status_code, 302)
                self.assertFalse(EventInterest.objects.filter(user=self.user).exists())

    def test_expiration_and_lifting_restore_attendance(self):
        url = reverse("event_register", args=[self.registration_event.pk])
        cancel_url = reverse(
            "event_cancel_registration", args=[self.registration_event.pk]
        )
        for restore in ("expire", "lift"):
            with self.subTest(restore=restore):
                if restore == "expire":
                    UserBan.objects.filter(pk=self.ban.pk).update(
                        expires_at=timezone.now()
                    )
                else:
                    self.ban, _ = apply_ban(self.admin, self.user)
                    lift_ban(self.admin, self.ban)
                response = self.client.post(url)
                self.assertEqual(response.status_code, 302)
                self.assertTrue(
                    Registration.objects.filter(
                        event=self.registration_event, user=self.user
                    ).exists()
                )
                self.assertEqual(self.client.post(cancel_url).status_code, 302)
                self.assertFalse(
                    Registration.objects.filter(
                        event=self.registration_event, user=self.user
                    ).exists()
                )

    def test_message_link_opens_existing_thread_for_either_participant(self):
        self.accept_contact()
        thread, _ = Conversation.get_or_create_for_users(self.user, self.peer)
        EventInterest.objects.all().delete()
        User.objects.filter(pk__in=[self.user.pk, self.peer.pk]).update(
            share_profile_with_attendees=False
        )
        for visitor, recipient in ((self.user, self.peer), (self.peer, self.user)):
            apply_ban(self.admin, visitor)
            self.client.force_login(visitor)
            with self.subTest(visitor=visitor.pk):
                response = self.client.get(reverse("message_user", args=[recipient.pk]))
                self.assertRedirects(
                    response, reverse("conversation", args=[thread.pk])
                )
                self.assertEqual(Conversation.objects.count(), 1)
                self.assertEqual(DirectMessage.objects.count(), 0)

    def test_accepted_contact_without_thread_renders_read_only_even_if_hidden(self):
        self.accept_contact()
        User.objects.filter(pk=self.peer.pk).update(share_profile_with_attendees=False)
        url = reverse("message_user", args=[self.peer.pk])
        for permanent in (False, True):
            if permanent:
                self.make_indefinite()
            for method in (self.client.get, self.client.head):
                with self.subTest(permanent=permanent, method=method.__name__):
                    response = method(url)
                    self.assertEqual(response.status_code, 200)
                    self.assertTemplateUsed(response, "events/message_user.html")
                    self.assertFalse(Conversation.objects.exists())
            response = self.client.get(url)
            self.assertContains(response, BAN_MESSAGE)
            self.assertContains(response, reverse("inbox"))
            self.assertNotContains(response, 'class="message-form"')
            self.assertNotContains(response, "Send Contact Request")

    def test_message_start_is_read_only_and_preserves_profile_privacy(self):
        self.set_interest(EventInterest.Status.INTERESTED)
        url = reverse("message_user", args=[self.peer.pk])
        response = self.client.get(url)
        self.assertContains(response, BAN_MESSAGE)
        self.assertNotContains(response, 'class="message-form"')
        self.assertFalse(ContactRequest.objects.exists())
        self.assertFalse(Conversation.objects.exists())
        # A thread alone does not bypass accepted-contact or profile checks.
        Conversation.get_or_create_for_users(self.user, self.peer)
        User.objects.filter(pk=self.peer.pk).update(share_profile_with_attendees=False)
        self.assertEqual(self.client.get(url).status_code, 404)
        self.assertEqual(
            self.client.get(reverse("message_user", args=[self.user.pk])).status_code,
            404,
        )

    def test_unbanned_message_link_resumes_creating_accepted_thread(self):
        self.accept_contact()
        lift_ban(self.admin, self.ban)
        response = self.client.get(reverse("message_user", args=[self.peer.pk]))
        thread = Conversation.objects.get()
        self.assertRedirects(response, reverse("conversation", args=[thread.pk]))

    def test_both_contact_entry_points_reject_bans_without_side_effects(self):
        self.set_interest(EventInterest.Status.INTERESTED)
        urls = (
            reverse("send_contact_request", args=[self.event.pk, self.peer.pk]),
            reverse("message_user", args=[self.peer.pk]),
        )
        for permanent in (False, True):
            if permanent:
                self.make_indefinite()
            for url in urls:
                with self.subTest(permanent=permanent, url=url):
                    response = self.client.post(
                        url, {"event": self.event.pk, "message": "Hello"}
                    )
                    self.assertContains(response, BAN_MESSAGE, status_code=403)
                    self.assertFalse(ContactRequest.objects.exists())
                    self.assertFalse(Conversation.objects.exists())
                    self.assertFalse(Notification.objects.exists())
                    self.assertEqual(mail.outbox, [])

    def test_contact_service_rejects_banned_sender_before_notifications(self):
        self.set_interest(EventInterest.Status.INTERESTED)
        self.peer.contact_permission = ContactPermission.OPEN
        self.peer.save(update_fields=["contact_permission"])
        deliver = Mock()
        for permanent in (False, True):
            if permanent:
                self.make_indefinite()
            with self.subTest(permanent=permanent):
                with self.assertRaisesMessage(PermissionDenied, BAN_MESSAGE):
                    create_contact_request(
                        self.event,
                        self.user,
                        self.peer,
                        "Hello",
                        notification_sender=deliver,
                    )
                deliver.assert_not_called()
                self.assertFalse(ContactRequest.objects.exists())
                self.assertFalse(Conversation.objects.exists())
                self.assertFalse(Notification.objects.exists())

    def test_inquiry_service_rejects_banned_host_before_translation_or_delivery(self):
        for permanent in (False, True):
            if permanent:
                self.make_indefinite()
            with self.subTest(permanent=permanent), patch(
                "chatbot.inquiries.gemini.translate"
            ) as translate:
                with self.assertRaisesMessage(PermissionDenied, BAN_MESSAGE):
                    answer_inquiry(self.inquiry, "At the gate", self.user)
                translate.assert_not_called()
                self.inquiry.refresh_from_db()
                self.assertEqual(self.inquiry.status, Inquiry.Status.OPEN)
                self.assertEqual(self.inquiry.answer, "")
                self.assertIsNone(self.inquiry.answered_at)
                self.assertIsNone(self.inquiry.answered_by)
                self.assertFalse(Notification.objects.exists())
                self.assertEqual(mail.outbox, [])

    def test_services_resume_after_expiration_and_lifting(self):
        self.set_interest(EventInterest.Status.INTERESTED)
        for restore in ("expire", "lift"):
            with self.subTest(restore=restore):
                if restore == "expire":
                    UserBan.objects.filter(pk=self.ban.pk).update(
                        expires_at=timezone.now()
                    )
                else:
                    self.ban, _ = apply_ban(self.admin, self.user)
                    lift_ban(self.admin, self.ban)
                contact, created = create_contact_request(
                    self.event, self.user, self.peer, "Hello"
                )
                self.assertTrue(created)
                contact.delete()
                inquiry = Inquiry.objects.create(
                    user=self.peer, event=self.event, question="Where?"
                )
                self.assertTrue(answer_inquiry(inquiry, "At the gate", self.user))

    def test_banned_recipient_cannot_receive_new_contact_requests(self):
        """A banned user should not receive new unsolicited requests."""
        self.set_interest(EventInterest.Status.INTERESTED)
        contact, created = create_contact_request(
            self.event, self.peer, self.user, "Hello"
        )
        self.assertFalse(created)
        self.assertIsNone(contact)
        self.assertFalse(
            Notification.objects.filter(contact_request__isnull=False).exists()
        )

    def test_browsing_and_polling_keep_automatic_read_tracking(self):
        self.accept_contact()
        thread, _ = Conversation.get_or_create_for_users(self.user, self.peer)
        incoming = DirectMessage.objects.create(
            conversation=thread, sender=self.peer, body="Hello"
        )
        outgoing = DirectMessage.objects.create(
            conversation=thread, sender=self.user, body="Earlier reply"
        )
        pending = ContactRequest.objects.create(
            event=self.event, sender=self.peer, recipient=self.user
        )
        pending_notice = Notification.objects.create(
            recipient=self.user,
            event=self.event,
            subject="Contact request",
            body="Hello",
            contact_request=pending,
        )
        update_notice = Notification.objects.create(
            recipient=self.user,
            event=self.event,
            subject="Event update",
            body="New venue",
        )
        for permanent in (False, True):
            if permanent:
                self.make_indefinite()
            DirectMessage.objects.filter(pk=incoming.pk).update(read_at=None)
            Notification.objects.filter(pk=update_notice.pk).update(read_at=None)
            for route, args in (
                ("inbox", []),
                ("notifications", []),
                ("event_detail", [self.event.pk]),
            ):
                with self.subTest(permanent=permanent, route=route):
                    self.assertEqual(
                        self.client.get(reverse(route, args=args)).status_code, 200
                    )
            incoming.refresh_from_db()
            self.assertIsNone(incoming.read_at)
            response = self.client.get(
                reverse("conversation_updates", args=[thread.pk]), {"after": 0}
            )
            self.assertEqual(response.status_code, 200)
            self.assertEqual(response["Cache-Control"], "no-store")
            self.assertEqual(
                [message["id"] for message in response.json()["messages"]],
                [incoming.pk, outgoing.pk],
            )
            incoming.refresh_from_db()
            outgoing.refresh_from_db()
            self.assertIsNotNone(incoming.read_at)
            self.assertIsNone(outgoing.read_at)
            self.assertEqual(
                self.client.get(reverse("unread_message_count")).json()["count"], 0
            )
            DirectMessage.objects.filter(pk=incoming.pk).update(read_at=None)
            self.assertEqual(
                self.client.get(reverse("conversation", args=[thread.pk])).status_code,
                200,
            )
            incoming.refresh_from_db()
            self.assertIsNotNone(incoming.read_at)
            update_notice.refresh_from_db()
            pending_notice.refresh_from_db()
            pending.refresh_from_db()
            self.assertIsNotNone(update_notice.read_at)
            self.assertIsNone(pending_notice.read_at)
            self.assertEqual(pending.status, ContactRequest.Status.PENDING)
            self.assertEqual(Conversation.objects.count(), 1)
            self.assertEqual(DirectMessage.objects.count(), 2)
            self.assertEqual(mail.outbox, [])
