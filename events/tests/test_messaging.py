"""Tests for pair-level attendee messaging."""

from datetime import datetime
from unittest.mock import patch
from zoneinfo import ZoneInfo

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.urls import reverse

from accounts.models import ContactPermission
from events.contact_services import create_contact_request
from events.forms import DirectMessageForm, EventPostForm, EventReplyForm
from events.models import (
    ContactRequest,
    Conversation,
    DirectMessage,
    Event,
    EventInterest,
    Notification,
)

User = get_user_model()
NYC = ZoneInfo("America/New_York")


def make_event(**kwargs):
    defaults = {
        "guid": "messages-event",
        "title": "Community Walk",
        "content_hash": "m" * 64,
        "start_time": datetime(2099, 3, 15, 9, 0, tzinfo=NYC),
        "status": Event.Status.UPCOMING,
    }
    defaults.update(kwargs)
    return Event.objects.create(**defaults)


class MessagingViewTests(TestCase):
    def setUp(self):
        self.sender = User.objects.create_user(
            username="sender",
            email="sender@example.com",
            password="Secure-Passphrase-23!",
            share_profile_with_attendees=True,
        )
        self.recipient = User.objects.create_user(
            username="recipient",
            email="recipient@example.com",
            password="Secure-Passphrase-23!",
            share_profile_with_attendees=True,
        )
        self.outsider = User.objects.create_user(
            username="outsider",
            email="outsider@example.com",
            password="Secure-Passphrase-23!",
        )
        self.event = make_event()
        EventInterest.objects.create(event=self.event, user=self.sender)
        EventInterest.objects.create(event=self.event, user=self.recipient)
        self.client.force_login(self.sender)

    def accept_contact(self):
        return ContactRequest.objects.create(
            event=self.event,
            sender=self.sender,
            recipient=self.recipient,
            status=ContactRequest.Status.ACCEPTED,
        )

    def test_profile_offers_send_message_for_shared_attendee(self):
        response = self.client.get(
            reverse("public_profile", args=[self.recipient.username])
        )

        self.assertContains(response, "Send Message")
        self.assertContains(
            response,
            reverse("message_user", args=[self.recipient.pk]),
        )

    def test_profile_self_and_missing_profile_are_handled(self):
        own_profile = self.client.get(
            reverse("public_profile", args=[self.sender.username])
        )
        missing_profile = self.client.get(
            reverse("public_profile", args=["no-such-user"])
        )

        self.assertFalse(own_profile.context["can_message"])
        self.assertEqual(missing_profile.status_code, 404)

    def test_cannot_start_conversation_with_self(self):
        response = self.client.get(reverse("message_user", args=[self.sender.pk]))

        self.assertEqual(response.status_code, 404)

    def test_contact_request_requires_an_eligible_recipient(self):
        contact_request, created = create_contact_request(
            self.event,
            self.sender,
            self.outsider,
            "Hello",
        )

        self.assertIsNone(contact_request)
        self.assertFalse(created)

    def test_hidden_profile_requires_accepted_contact_to_open_messages(self):
        self.recipient.share_profile_with_attendees = False
        self.recipient.save(update_fields=["share_profile_with_attendees"])

        response = self.client.get(reverse("message_user", args=[self.recipient.pk]))

        self.assertEqual(response.status_code, 404)
        self.accept_contact()
        response = self.client.get(reverse("message_user", args=[self.recipient.pk]))
        self.assertEqual(response.status_code, 302)
        self.assertEqual(Conversation.objects.count(), 1)

    def test_start_flow_sends_contact_request_instead_of_message(self):
        response = self.client.get(reverse("message_user", args=[self.recipient.pk]))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Send Contact Request")
        self.assertContains(response, "Community Walk")

        response = self.client.post(
            reverse("message_user", args=[self.recipient.pk]),
            {"event": self.event.pk, "message": "Would you like to go together?"},
        )

        self.assertRedirects(
            response,
            reverse("message_user", args=[self.recipient.pk]),
        )
        contact_request = ContactRequest.objects.get()
        self.assertEqual(contact_request.status, ContactRequest.Status.PENDING)
        self.assertEqual(contact_request.sender, self.sender)
        self.assertEqual(contact_request.message, "Would you like to go together?")
        self.assertFalse(Conversation.objects.exists())
        notification = Notification.objects.get(contact_request=contact_request)
        self.assertEqual(notification.recipient, self.recipient)
        self.assertEqual(notification.event, self.event)

    def test_ineligible_contact_request_reports_that_shared_event_is_required(self):
        with patch(
            "events.messaging_views.create_contact_request",
            return_value=(None, False),
        ):
            response = self.client.post(
                reverse("message_user", args=[self.recipient.pk]),
                {"event": self.event.pk, "message": "Hello"},
                follow=True,
            )

        self.assertContains(
            response,
            "Choose an upcoming event that you both are interested in.",
        )

    def test_duplicate_pending_contact_request_is_not_created_again(self):
        existing = ContactRequest.objects.create(
            event=self.event,
            sender=self.sender,
            recipient=self.recipient,
        )

        response = self.client.post(
            reverse("message_user", args=[self.recipient.pk]),
            {"event": self.event.pk, "message": "A duplicate"},
            follow=True,
        )

        self.assertContains(response, "already awaiting a response")
        self.assertEqual(ContactRequest.objects.count(), 1)
        self.assertEqual(existing.status, ContactRequest.Status.PENDING)

    def test_open_contact_permission_starts_an_accepted_conversation(self):
        self.recipient.contact_permission = ContactPermission.OPEN
        self.recipient.save(update_fields=["contact_permission"])

        response = self.client.post(
            reverse("message_user", args=[self.recipient.pk]),
            {"event": self.event.pk, "message": "Hello!"},
        )

        conversation = Conversation.objects.get()
        self.assertRedirects(
            response,
            reverse("conversation", args=[conversation.pk]),
        )
        self.assertEqual(
            ContactRequest.objects.get().status,
            ContactRequest.Status.ACCEPTED,
        )

    def test_accepting_request_creates_pair_thread_and_unlocks_messaging(self):
        contact_request = ContactRequest.objects.create(
            event=self.event,
            sender=self.recipient,
            recipient=self.sender,
        )

        response = self.client.post(
            reverse(
                "respond_to_contact_request",
                args=[contact_request.pk, "accept"],
            )
        )

        self.assertEqual(response.status_code, 302)
        thread = Conversation.objects.get()
        self.assertEqual(thread.other_participant(self.sender), self.recipient)
        self.assertRedirects(
            self.client.get(reverse("message_user", args=[self.recipient.pk])),
            reverse("conversation", args=[thread.pk]),
        )

    def test_reply_creates_message_without_duplicate_notification(self):
        self.accept_contact()
        thread, _ = Conversation.get_or_create_for_users(self.sender, self.recipient)

        response = self.client.post(
            reverse("conversation", args=[thread.pk]),
            {"body": "See you there!"},
        )

        self.assertRedirects(response, reverse("conversation", args=[thread.pk]))
        message = DirectMessage.objects.get()
        self.assertEqual(message.sender, self.sender)
        self.assertIsNone(message.event)
        self.assertEqual(message.body, "See you there!")
        self.assertFalse(Notification.objects.exists())

    def test_messages_are_not_listed_as_notifications_and_mark_read_in_chat(self):
        self.accept_contact()
        thread, _ = Conversation.get_or_create_for_users(self.sender, self.recipient)
        message = DirectMessage.objects.create(
            conversation=thread,
            sender=self.sender,
            event=self.event,
            body="Meet by the main entrance.",
        )
        self.client.force_login(self.recipient)

        response = self.client.get(reverse("notifications"))

        self.assertNotContains(response, "Meet by the main entrance.")
        message.refresh_from_db()
        self.assertIsNone(message.read_at)

        response = self.client.get(reverse("conversation", args=[thread.pk]))

        self.assertNotContains(response, "Community Walk")
        self.assertContains(response, "Meet by the main entrance.")
        message.refresh_from_db()
        self.assertIsNotNone(message.read_at)

    def test_chat_composer_has_no_redundant_event_selector(self):
        self.accept_contact()
        thread, _ = Conversation.get_or_create_for_users(self.sender, self.recipient)

        response = self.client.get(reverse("conversation", args=[thread.pk]))

        self.assertContains(response, 'aria-label="Write a message"')
        self.assertContains(response, "chat-composer")
        self.assertNotContains(response, "Related event")
        self.assertNotContains(response, "Community Walk")

    def test_unaccepted_contacts_cannot_open_or_post_to_conversation(self):
        contact_request = ContactRequest.objects.create(
            event=self.event,
            sender=self.sender,
            recipient=self.recipient,
        )
        thread, _ = Conversation.get_or_create_for_users(self.sender, self.recipient)

        response = self.client.get(reverse("conversation", args=[thread.pk]))

        self.assertEqual(response.status_code, 404)
        response = self.client.post(
            reverse("conversation", args=[thread.pk]),
            {"body": "Not yet"},
        )
        self.assertEqual(response.status_code, 404)
        response = self.client.get(reverse("conversation_updates", args=[thread.pk]))
        self.assertEqual(response.status_code, 404)
        self.assertEqual(contact_request.status, ContactRequest.Status.PENDING)
        self.assertFalse(DirectMessage.objects.exists())

    def test_nonparticipant_cannot_view_conversation(self):
        self.accept_contact()
        thread, _ = Conversation.get_or_create_for_users(self.sender, self.recipient)
        self.client.force_login(self.outsider)

        response = self.client.get(reverse("conversation", args=[thread.pk]))

        self.assertEqual(response.status_code, 404)

    def test_message_keeps_event_association_out_of_chat_display(self):
        self.accept_contact()
        thread, _ = Conversation.get_or_create_for_users(self.sender, self.recipient)
        DirectMessage.objects.create(
            conversation=thread,
            sender=self.sender,
            event=self.event,
            body="This message is still event-related.",
        )

        response = self.client.get(reverse("conversation", args=[thread.pk]))

        self.assertContains(response, "This message is still event-related.")
        self.assertNotContains(response, "Community Walk")
        self.assertNotContains(response, "Related event")

    def test_polling_returns_only_new_messages_and_marks_them_read(self):
        self.accept_contact()
        thread, _ = Conversation.get_or_create_for_users(self.sender, self.recipient)
        first = DirectMessage.objects.create(
            conversation=thread,
            sender=self.sender,
            body="Earlier message",
        )
        second = DirectMessage.objects.create(
            conversation=thread,
            sender=self.recipient,
            body="New reply",
        )
        self.client.force_login(self.sender)

        response = self.client.get(
            reverse("conversation_updates", args=[thread.pk]),
            {"after": first.pk},
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            [item["id"] for item in response.json()["messages"]],
            [second.pk],
        )
        self.assertEqual(response.json()["messages"][0]["body"], "New reply")
        second.refresh_from_db()
        self.assertIsNotNone(second.read_at)

    def test_polling_rejects_invalid_cursor_and_nonparticipants(self):
        self.accept_contact()
        thread, _ = Conversation.get_or_create_for_users(self.sender, self.recipient)

        response = self.client.get(
            reverse("conversation_updates", args=[thread.pk]),
            {"after": "not-an-id"},
        )

        self.assertEqual(response.status_code, 400)
        negative_cursor = self.client.get(
            reverse("conversation_updates", args=[thread.pk]),
            {"after": "-1"},
        )
        self.assertEqual(negative_cursor.status_code, 400)
        self.client.force_login(self.outsider)
        response = self.client.get(
            reverse("conversation_updates", args=[thread.pk]),
        )
        self.assertEqual(response.status_code, 404)

    def test_ajax_send_returns_created_message_without_page_reload(self):
        self.accept_contact()
        thread, _ = Conversation.get_or_create_for_users(self.sender, self.recipient)

        response = self.client.post(
            reverse("conversation", args=[thread.pk]),
            {"body": "Sent live"},
            HTTP_X_REQUESTED_WITH="XMLHttpRequest",
            HTTP_ACCEPT="application/json",
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["message"]["body"], "Sent live")
        self.assertEqual(DirectMessage.objects.get().sender, self.sender)

    def test_invalid_ajax_send_returns_form_errors(self):
        self.accept_contact()
        thread, _ = Conversation.get_or_create_for_users(self.sender, self.recipient)

        response = self.client.post(
            reverse("conversation", args=[thread.pk]),
            {"body": "   "},
            HTTP_X_REQUESTED_WITH="XMLHttpRequest",
        )

        self.assertEqual(response.status_code, 400)
        self.assertIn("errors", response.json())
        self.assertEqual(DirectMessage.objects.count(), 0)

    def test_whitespace_only_community_and_message_forms_are_invalid(self):
        cases = (
            (DirectMessageForm, "Your message cannot be empty."),
            (EventPostForm, "Your post cannot be empty."),
            (EventReplyForm, "Your reply cannot be empty."),
        )
        for form_class, expected_error in cases:
            with self.subTest(form=form_class.__name__):
                form = form_class(data={"body": "   "})
                self.assertFalse(form.is_valid())
                self.assertEqual(
                    form.errors.as_data()["body"][0].message,
                    expected_error,
                )

    def test_messages_navigation_shows_unread_count_not_notification_count(self):
        self.accept_contact()
        thread, _ = Conversation.get_or_create_for_users(self.sender, self.recipient)
        DirectMessage.objects.create(
            conversation=thread,
            sender=self.recipient,
            body="Unread for sender",
        )

        response = self.client.get(reverse("landing"))

        self.assertContains(response, "Messages")
        self.assertContains(response, "1 new")
        self.assertFalse(Notification.objects.exists())

    def test_unread_message_count_endpoint_reports_current_count(self):
        thread, _ = Conversation.get_or_create_for_users(self.sender, self.recipient)
        url = reverse("unread_message_count")

        self.assertEqual(self.client.get(url).json()["count"], 0)
        DirectMessage.objects.create(
            conversation=thread,
            sender=self.recipient,
            body="Arrived after landing loaded.",
        )

        response = self.client.get(url)

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response["Cache-Control"], "no-store")
        self.assertEqual(response.json(), {"count": 1})

    def test_unread_message_count_endpoint_requires_login(self):
        self.client.logout()

        response = self.client.get(reverse("unread_message_count"))

        self.assertEqual(response.status_code, 302)
        self.assertIn("/login", response.url)

    def test_landing_page_has_live_message_count_badge_hook(self):
        response = self.client.get(reverse("landing"))

        self.assertContains(
            response,
            f'data-message-count-url="{reverse("unread_message_count")}"',
        )
        self.assertContains(response, "events/message-count.js")
        self.assertNotContains(response, "message-count-badge")
        self.assertNotContains(response, '<span class="count-pill"></span>', html=True)

    def test_landing_page_renders_badge_only_when_unread_messages_exist(self):
        thread, _ = Conversation.get_or_create_for_users(self.sender, self.recipient)
        DirectMessage.objects.create(
            conversation=thread,
            sender=self.recipient,
            body="Unread message",
        )

        response = self.client.get(reverse("landing"))

        self.assertContains(response, "Messages")
        self.assertContains(response, "message-count-badge")
        self.assertContains(response, "1 new")

    def test_messages_page_is_private_and_shows_last_sender(self):
        self.accept_contact()
        thread, _ = Conversation.get_or_create_for_users(self.sender, self.recipient)
        DirectMessage.objects.create(
            conversation=thread,
            sender=self.recipient,
            body="A private hello.",
        )

        response = self.client.get(reverse("inbox"))

        self.assertContains(response, "<h1>Messages</h1>", html=True)
        self.assertContains(response, "recipient")
        self.assertContains(response, "A private hello.")
        self.client.force_login(self.outsider)
        response = self.client.get(reverse("inbox"))
        self.assertFalse(response.context["conversations"])
        self.assertContains(response, "No messages yet")
