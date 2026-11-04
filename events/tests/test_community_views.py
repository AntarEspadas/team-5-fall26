"""Tests for event interests, discussions, and contact requests."""

from datetime import datetime
from zoneinfo import ZoneInfo

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.urls import reverse

from accounts.models import ContactPermission
from events.models import ContactRequest, Event, EventInterest, EventPost, EventReply

User = get_user_model()
NYC = ZoneInfo("America/New_York")


def make_event(guid="community-1", **kwargs):
    """Create an event with sensible defaults."""
    defaults = {
        "title": "Community Yoga",
        "content_hash": "v" * 64,
        "start_time": datetime(2099, 3, 15, 9, 0, tzinfo=NYC),
        "status": Event.Status.UPCOMING,
    }
    defaults.update(kwargs)
    return Event.objects.create(guid=guid, **defaults)


class CommunityViewTestBase(TestCase):
    """Shared fixtures for community view tests."""

    def setUp(self):
        self.event = make_event()
        self.alice = User.objects.create_user(
            username="alice",
            email="alice@example.com",
            password="pass12345",
        )
        self.bob = User.objects.create_user(
            username="bob",
            email="bob@example.com",
            password="pass12345",
            share_profile_with_attendees=True,
        )
        self.client.force_login(self.alice)

    def detail_url(self):
        return reverse("event_detail", args=[self.event.pk])


class EventInterestViewTests(CommunityViewTestBase):
    """Tests for toggling interest in an event."""

    def test_add_interest(self):
        response = self.client.post(
            reverse("toggle_event_interest", args=[self.event.pk])
        )

        self.assertRedirects(response, self.detail_url())
        self.assertTrue(
            EventInterest.objects.filter(event=self.event, user=self.alice).exists()
        )

    def test_remove_existing_interest(self):
        EventInterest.objects.create(event=self.event, user=self.alice)

        response = self.client.post(
            reverse("toggle_event_interest", args=[self.event.pk])
        )

        self.assertRedirects(response, self.detail_url())
        self.assertFalse(
            EventInterest.objects.filter(event=self.event, user=self.alice).exists()
        )

    def test_closed_event_does_not_change_interest(self):
        self.event.status = Event.Status.DONE
        self.event.save()
        EventInterest.objects.create(event=self.event, user=self.alice)

        response = self.client.post(
            reverse("toggle_event_interest", args=[self.event.pk])
        )

        self.assertRedirects(response, self.detail_url())
        self.assertTrue(
            EventInterest.objects.filter(event=self.event, user=self.alice).exists()
        )

    def test_get_is_not_allowed(self):
        response = self.client.get(
            reverse("toggle_event_interest", args=[self.event.pk])
        )

        self.assertEqual(response.status_code, 405)

    def test_anonymous_user_is_redirected_to_login(self):
        self.client.logout()

        response = self.client.post(
            reverse("toggle_event_interest", args=[self.event.pk])
        )

        self.assertEqual(response.status_code, 302)
        self.assertIn("/login", response.url)


class EventDiscussionViewTests(CommunityViewTestBase):
    """Tests for event posts and replies."""

    def test_create_post(self):
        response = self.client.post(
            reverse("create_event_post", args=[self.event.pk]),
            {"body": "  Looking forward to this event!  "},
        )

        self.assertRedirects(response, self.detail_url())
        post = EventPost.objects.get()
        self.assertEqual(post.body, "Looking forward to this event!")
        self.assertEqual(post.author, self.alice)
        self.assertEqual(post.event, self.event)

    def test_blank_post_is_not_saved(self):
        response = self.client.post(
            reverse("create_event_post", args=[self.event.pk]),
            {"body": "   "},
        )

        self.assertRedirects(response, self.detail_url())
        self.assertEqual(EventPost.objects.count(), 0)

    def test_post_on_closed_event_is_not_saved(self):
        self.event.status = Event.Status.DONE
        self.event.save()

        response = self.client.post(
            reverse("create_event_post", args=[self.event.pk]),
            {"body": "Hello"},
        )

        self.assertRedirects(response, self.detail_url())
        self.assertEqual(EventPost.objects.count(), 0)

    def test_create_reply(self):
        post = EventPost.objects.create(
            event=self.event,
            author=self.bob,
            body="What should I bring?",
        )

        response = self.client.post(
            reverse("create_event_reply", args=[post.pk]),
            {"body": "Bring a yoga mat."},
        )

        self.assertRedirects(response, self.detail_url())
        reply = EventReply.objects.get()
        self.assertEqual(reply.body, "Bring a yoga mat.")
        self.assertEqual(reply.author, self.alice)
        self.assertEqual(reply.post, post)

    def test_blank_reply_is_not_saved(self):
        post = EventPost.objects.create(
            event=self.event,
            author=self.bob,
            body="Question",
        )

        response = self.client.post(
            reverse("create_event_reply", args=[post.pk]),
            {"body": "   "},
        )

        self.assertRedirects(response, self.detail_url())
        self.assertEqual(EventReply.objects.count(), 0)

    def test_reply_to_closed_event_is_not_saved(self):
        post = EventPost.objects.create(
            event=self.event,
            author=self.bob,
            body="Question",
        )
        self.event.status = Event.Status.DONE
        self.event.save()

        response = self.client.post(
            reverse("create_event_reply", args=[post.pk]),
            {"body": "Reply"},
        )

        self.assertRedirects(response, self.detail_url())
        self.assertEqual(EventReply.objects.count(), 0)


class ContactRequestViewTests(CommunityViewTestBase):
    """Tests for sending and responding to contact requests."""

    def setUp(self):
        super().setUp()
        EventInterest.objects.create(event=self.event, user=self.alice)
        EventInterest.objects.create(event=self.event, user=self.bob)

    def send_url(self, recipient=None):
        recipient = recipient or self.bob
        return reverse(
            "send_contact_request",
            args=[self.event.pk, recipient.pk],
        )

    def test_send_request_requiring_approval(self):
        response = self.client.post(
            self.send_url(),
            {"message": "Hi, I would like to connect."},
        )

        self.assertRedirects(response, self.detail_url())
        contact_request = ContactRequest.objects.get()
        self.assertEqual(contact_request.sender, self.alice)
        self.assertEqual(contact_request.recipient, self.bob)
        self.assertEqual(contact_request.event, self.event)
        self.assertEqual(contact_request.message, "Hi, I would like to connect.")
        self.assertEqual(contact_request.status, ContactRequest.Status.PENDING)

    def test_open_contact_permission_accepts_request_automatically(self):
        self.bob.contact_permission = ContactPermission.OPEN
        self.bob.save()

        self.client.post(self.send_url(), {"message": "Hello"})

        contact_request = ContactRequest.objects.get()
        self.assertEqual(contact_request.status, ContactRequest.Status.ACCEPTED)

    def test_empty_optional_message_is_allowed(self):
        response = self.client.post(self.send_url(), {})

        self.assertRedirects(response, self.detail_url())
        contact_request = ContactRequest.objects.get()
        self.assertEqual(contact_request.message, "")

    def test_cannot_send_request_to_self(self):
        self.alice.share_profile_with_attendees = True
        self.alice.save()

        response = self.client.post(
            self.send_url(self.alice),
            {"message": "Hi"},
        )

        self.assertRedirects(response, self.detail_url())
        self.assertEqual(ContactRequest.objects.count(), 0)

    def test_sender_must_be_interested_in_event(self):
        EventInterest.objects.filter(event=self.event, user=self.alice).delete()

        response = self.client.post(self.send_url(), {"message": "Hello"})

        self.assertRedirects(response, self.detail_url())
        self.assertEqual(ContactRequest.objects.count(), 0)

    def test_recipient_must_share_profile(self):
        self.bob.share_profile_with_attendees = False
        self.bob.save()

        response = self.client.post(self.send_url(), {"message": "Hello"})

        self.assertEqual(response.status_code, 404)
        self.assertEqual(ContactRequest.objects.count(), 0)

    def test_recipient_must_be_interested_in_same_event(self):
        EventInterest.objects.filter(event=self.event, user=self.bob).delete()

        response = self.client.post(self.send_url(), {"message": "Hello"})

        self.assertEqual(response.status_code, 404)
        self.assertEqual(ContactRequest.objects.count(), 0)

    def test_duplicate_pending_request_is_not_created(self):
        ContactRequest.objects.create(
            event=self.event,
            sender=self.alice,
            recipient=self.bob,
            message="First request",
        )

        response = self.client.post(self.send_url(), {"message": "Second request"})

        self.assertRedirects(response, self.detail_url())
        self.assertEqual(ContactRequest.objects.count(), 1)

    def test_duplicate_accepted_request_is_not_created(self):
        ContactRequest.objects.create(
            event=self.event,
            sender=self.alice,
            recipient=self.bob,
            status=ContactRequest.Status.ACCEPTED,
        )

        response = self.client.post(self.send_url(), {"message": "Again"})

        self.assertRedirects(response, self.detail_url())
        self.assertEqual(ContactRequest.objects.count(), 1)

    def test_declined_request_can_be_sent_again(self):
        ContactRequest.objects.create(
            event=self.event,
            sender=self.alice,
            recipient=self.bob,
            status=ContactRequest.Status.DECLINED,
        )

        response = self.client.post(self.send_url(), {"message": "Retry"})

        self.assertRedirects(response, self.detail_url())
        self.assertEqual(ContactRequest.objects.count(), 2)
        self.assertTrue(
            ContactRequest.objects.filter(
                sender=self.alice,
                recipient=self.bob,
                status=ContactRequest.Status.PENDING,
            ).exists()
        )

    def test_closed_event_rejects_contact_request(self):
        self.event.status = Event.Status.DONE
        self.event.save()

        response = self.client.post(self.send_url(), {"message": "Hello"})

        self.assertRedirects(response, self.detail_url())
        self.assertEqual(ContactRequest.objects.count(), 0)

    def test_accept_pending_request(self):
        contact_request = ContactRequest.objects.create(
            event=self.event,
            sender=self.bob,
            recipient=self.alice,
        )

        response = self.client.post(
            reverse(
                "respond_to_contact_request",
                args=[contact_request.pk, "accept"],
            )
        )

        self.assertRedirects(response, self.detail_url())
        contact_request.refresh_from_db()
        self.assertEqual(contact_request.status, ContactRequest.Status.ACCEPTED)

    def test_decline_pending_request(self):
        contact_request = ContactRequest.objects.create(
            event=self.event,
            sender=self.bob,
            recipient=self.alice,
        )

        response = self.client.post(
            reverse(
                "respond_to_contact_request",
                args=[contact_request.pk, "decline"],
            )
        )

        self.assertRedirects(response, self.detail_url())
        contact_request.refresh_from_db()
        self.assertEqual(contact_request.status, ContactRequest.Status.DECLINED)

    def test_invalid_action_does_not_change_request(self):
        contact_request = ContactRequest.objects.create(
            event=self.event,
            sender=self.bob,
            recipient=self.alice,
        )

        response = self.client.post(
            reverse(
                "respond_to_contact_request",
                args=[contact_request.pk, "unknown"],
            )
        )

        self.assertRedirects(response, self.detail_url())
        contact_request.refresh_from_db()
        self.assertEqual(contact_request.status, ContactRequest.Status.PENDING)

    def test_another_user_cannot_respond_to_request(self):
        contact_request = ContactRequest.objects.create(
            event=self.event,
            sender=self.bob,
            recipient=self.alice,
        )
        self.client.force_login(self.bob)

        response = self.client.post(
            reverse(
                "respond_to_contact_request",
                args=[contact_request.pk, "accept"],
            )
        )

        self.assertEqual(response.status_code, 404)
        contact_request.refresh_from_db()
        self.assertEqual(contact_request.status, ContactRequest.Status.PENDING)

    def test_already_processed_request_cannot_be_processed_again(self):
        contact_request = ContactRequest.objects.create(
            event=self.event,
            sender=self.bob,
            recipient=self.alice,
            status=ContactRequest.Status.ACCEPTED,
        )

        response = self.client.post(
            reverse(
                "respond_to_contact_request",
                args=[contact_request.pk, "decline"],
            )
        )

        self.assertEqual(response.status_code, 404)
        contact_request.refresh_from_db()
        self.assertEqual(contact_request.status, ContactRequest.Status.ACCEPTED)
