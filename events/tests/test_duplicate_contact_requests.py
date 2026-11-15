"""Regression tests: one contact invitation per pair, across all events."""

from django.test import TestCase
from django.urls import reverse

from accounts.models import ContactPermission, User
from events.contact_services import create_contact_request
from events.models import ContactRequest, Event, EventInterest, Notification


class DuplicateContactRequestTests(TestCase):
    """Prevent repeated invitations without blocking a later retry after decline."""

    @classmethod
    def setUpTestData(cls):
        cls.sender = User.objects.create_user(
            username="pair_sender",
            email="pair_sender@example.com",
            share_profile_with_attendees=True,
        )
        cls.recipient = User.objects.create_user(
            username="pair_recipient",
            email="pair_recipient@example.com",
            share_profile_with_attendees=True,
        )
        cls.first = Event.objects.create(
            guid="first-contact", title="First Event", content_hash="c" * 64
        )
        cls.second = Event.objects.create(
            guid="second-contact", title="Second Event", content_hash="d" * 64
        )
        for event in (cls.first, cls.second):
            for member in (cls.sender, cls.recipient):
                EventInterest.objects.create(event=event, user=member)

    def test_second_event_does_not_create_another_pending_request(self):
        """Switching the event cannot cause multiple pending requests."""
        first, created = create_contact_request(
            self.first, self.sender, self.recipient, "Hi"
        )
        self.assertTrue(created)
        second, created = create_contact_request(
            self.second, self.sender, self.recipient, "Hi again"
        )
        self.assertFalse(created)
        self.assertEqual(first.pk, second.pk)
        self.assertEqual(ContactRequest.objects.count(), 1)
        self.assertEqual(Notification.objects.count(), 1)

    def test_reverse_direction_reuses_the_pending_request(self):
        """The recipient cannot send a second invitation back."""
        first, created = create_contact_request(
            self.first, self.sender, self.recipient, "Hi"
        )
        self.assertTrue(created)
        reversed_request, created = create_contact_request(
            self.second, self.recipient, self.sender, "Hello"
        )
        self.assertFalse(created)
        self.assertEqual(first.pk, reversed_request.pk)
        self.assertEqual(ContactRequest.objects.count(), 1)

    def test_declined_request_allows_a_later_new_request(self):
        """Declining an invitation still allows someone to try again."""
        first, created = create_contact_request(
            self.first, self.sender, self.recipient, "Hi"
        )
        self.assertTrue(created)
        first.status = ContactRequest.Status.DECLINED
        first.save(update_fields=["status"])
        another, created = create_contact_request(
            self.second, self.sender, self.recipient, "Maybe later?"
        )
        self.assertTrue(created)
        self.assertNotEqual(another.pk, first.pk)

    def test_accepted_contact_is_not_duplicated(self):
        """Open-contact accounts already have the connection they need."""
        self.recipient.contact_permission = ContactPermission.OPEN
        self.recipient.save(update_fields=["contact_permission"])
        first, created = create_contact_request(
            self.first, self.sender, self.recipient, "Hi"
        )
        self.assertTrue(created)
        self.assertEqual(first.status, ContactRequest.Status.ACCEPTED)
        duplicate, created = create_contact_request(
            self.second, self.sender, self.recipient, "Hello again"
        )
        self.assertFalse(created)
        self.assertEqual(duplicate.pk, first.pk)

    def test_event_page_reuses_request_from_a_different_event(self):
        """Do not show a second request form after the first invitation."""
        self.client.force_login(self.sender)
        first_url = reverse(
            "send_contact_request", args=[self.first.pk, self.recipient.pk]
        )
        self.client.post(first_url, {"message": "Hi"})
        page = self.client.get(reverse("event_detail", args=[self.second.pk]))
        self.assertContains(page, "Your contact request about First Event")
        self.assertNotContains(
            page,
            reverse("send_contact_request", args=[self.second.pk, self.recipient.pk]),
        )
        self.assertEqual(ContactRequest.objects.count(), 1)

    def test_event_page_handles_reverse_pending_request(self):
        """The recipient sees their invitation and can respond to it."""
        create_contact_request(self.first, self.sender, self.recipient, "Hi")
        self.client.force_login(self.recipient)
        page = self.client.get(reverse("event_detail", args=[self.second.pk]))
        self.assertContains(page, "sent you a contact request")
        self.assertContains(page, reverse("notifications"))
        self.assertNotContains(
            page,
            reverse("send_contact_request", args=[self.second.pk, self.sender.pk]),
        )

    def test_event_page_shows_message_action_for_accepted_contact(self):
        """An existing accepted request offers messaging instead of a new form."""
        self.recipient.contact_permission = ContactPermission.OPEN
        self.recipient.save(update_fields=["contact_permission"])
        create_contact_request(self.first, self.sender, self.recipient, "Hi")
        self.client.force_login(self.sender)
        page = self.client.get(reverse("event_detail", args=[self.second.pk]))
        self.assertContains(page, "Contact request accepted.")
        self.assertContains(page, "Open messages")
        self.assertNotContains(
            page,
            reverse("send_contact_request", args=[self.second.pk, self.recipient.pk]),
        )

    def test_posting_again_on_second_event_explains_reused_request(self):
        """A stale event form should give feedback instead of failing silently."""
        create_contact_request(self.first, self.sender, self.recipient, "Hi")
        self.client.force_login(self.sender)
        response = self.client.post(
            reverse("send_contact_request", args=[self.second.pk, self.recipient.pk]),
            {"message": "Again"},
            follow=True,
        )
        self.assertContains(response, "already awaiting a response")
        self.assertEqual(ContactRequest.objects.count(), 1)
        self.assertEqual(Notification.objects.count(), 1)
