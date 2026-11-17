"""Tests for the toast shown after every action."""

from datetime import datetime
from zoneinfo import ZoneInfo

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.urls import reverse

from accounts.models import ContactPermission
from events.models import ContactRequest, Event, EventInterest, EventPost

User = get_user_model()
NYC = ZoneInfo("America/New_York")


class ToastTestCase(TestCase):
    """A member, a host with an open profile, and two events."""

    @classmethod
    def setUpTestData(cls):
        """Create the people and events the actions run against."""
        cls.member = User.objects.create_user(
            username="member",
            email="member@example.com",
            password="pass12345",
        )
        cls.peer = User.objects.create_user(
            username="peer",
            email="peer@example.com",
            password="pass12345",
            share_profile_with_attendees=True,
        )
        cls.event = Event.objects.create(
            guid="toast-open",
            title="Open walk",
            content_hash="t" * 64,
            start_time=datetime(2099, 5, 1, 9, 0, tzinfo=NYC),
        )
        cls.closed = Event.objects.create(
            guid="toast-closed",
            title="Old walk",
            content_hash="t" * 64,
            status=Event.Status.DONE,
        )

    def setUp(self):
        """Sign the member in."""
        self.client.force_login(self.member)

    def post(self, url, data=None):
        """POST, follow the redirect, and return the final response."""
        return self.client.post(url, data or {}, follow=True)

    def assertToast(self, response, text, level="success"):
        """Check the page shows one toast of this level with this text."""
        self.assertContains(response, f'class="toast toast-{level}"')
        self.assertContains(response, text)


class ToastMarkupTests(ToastTestCase):
    """Toasts render in one place on every page, styled by level."""

    def test_success_toast_hides_itself(self):
        """Success toasts are polite status messages that fade."""
        response = self.post(
            reverse("event_register", args=[self.event.pk]),
            {"party_size": 3},
        )
        self.assertToast(response, "Registered for a party of 3.")
        self.assertContains(
            response,
            'class="toast toast-success" role="status" data-toast-autohide',
        )
        self.assertContains(response, "events/toasts.js")
        self.assertContains(response, 'aria-label="Dismiss"')

    def test_errors_are_red_alerts_that_stay(self):
        """Errors use the alert role and stay until dismissed."""
        response = self.post(reverse("create_event_post", args=[self.event.pk]))
        self.assertContains(
            response,
            'class="toast toast-error" role="alert">',
        )
        self.assertContains(
            response,
            "Write a comment before posting.",
        )

    def test_toast_shows_once_then_clears(self):
        """A message shows on the next page only, not on later ones."""
        self.post(reverse("toggle_event_interest", args=[self.event.pk]))
        response = self.client.get(reverse("my_events"))
        self.assertNotContains(response, 'class="toast ')

    def test_signed_out_pages_show_toasts(self):
        """Pages for visitors render toasts too."""
        self.client.logout()
        response = self.client.post(
            reverse("login"),
            {"username": "member", "password": "wrong"},
        )
        self.assertContains(response, "data-toast-stack")


class ActionToastTests(ToastTestCase):
    """Each community action confirms success or explains a failure."""

    def test_interest_toggle(self):
        """Marking and unmarking interest both confirm."""
        url = reverse(
            "toggle_event_interest",
            args=[self.event.pk],
        )
        self.assertToast(
            self.post(url),
            "Marked as interested.",
        )
        self.assertToast(
            self.post(url),
            "Removed from your interested events.",
        )

    def test_interest_on_closed_event(self):
        """Closed events explain what to do instead."""
        url = reverse(
            "toggle_event_interest",
            args=[self.closed.pk],
        )
        self.assertToast(
            self.post(url),
            "Browse upcoming events instead.",
            "error",
        )

    def test_unknown_interest_choice(self):
        """An unknown choice asks the member to reload."""
        url = reverse(
            "toggle_event_interest",
            args=[self.event.pk],
        )
        response = self.post(url, {"status": "maybe"})
        self.assertToast(
            response,
            "Reload the page and try again.",
            "error",
        )

    def test_post_and_reply(self):
        """Comments and replies confirm when posted."""
        response = self.post(
            reverse("create_event_post", args=[self.event.pk]),
            {"body": "Hi"},
        )
        self.assertToast(response, "Comment posted.")

        post = EventPost.objects.get()
        url = reverse("create_event_reply", args=[post.pk])

        self.assertToast(
            self.post(url, {"body": "Hello"}),
            "Reply posted.",
        )
        self.assertToast(
            self.post(url),
            "Write a reply before posting.",
            "error",
        )

    def test_post_and_reply_on_closed_event(self):
        """Closed events refuse posts and replies with a next step."""
        response = self.post(reverse("create_event_post", args=[self.closed.pk]))
        self.assertToast(
            response,
            "no longer takes new activity",
            "error",
        )
        self.assertToast(
            response,
            "Browse upcoming events instead.",
            "error",
        )

        post = EventPost.objects.create(
            event=self.closed,
            author=self.member,
            body="Earlier",
        )
        response = self.post(reverse("create_event_reply", args=[post.pk]))
        self.assertToast(
            response,
            "no longer takes new activity",
            "error",
        )
        self.assertToast(
            response,
            "Browse upcoming events instead.",
            "error",
        )

    def test_contact_request_flow(self):
        """Sending, repeating and answering a contact request all confirm."""
        url = reverse(
            "send_contact_request",
            args=[self.event.pk, self.peer.pk],
        )
        EventInterest.objects.create(
            event=self.event,
            user=self.peer,
        )

        response = self.post(url)
        self.assertToast(
            response,
            "Mark yourself as interested",
            "error",
        )

        EventInterest.objects.create(
            event=self.event,
            user=self.member,
        )

        self.assertToast(
            self.post(url),
            "Contact request sent.",
        )
        self.assertToast(
            self.post(url),
            "already awaiting a response",
            level="info",
        )

        response = self.post(
            url,
            {"message": "x" * 5000},
        )
        self.assertToast(
            response,
            "Shorten it",
            "error",
        )

        contact = ContactRequest.objects.get()
        self.client.force_login(self.peer)

        respond = reverse(
            "respond_to_contact_request",
            args=[contact.pk, "accept"],
        )
        self.assertToast(
            self.post(respond),
            "Contact request accepted.",
        )

    def test_contact_request_declined_and_bad_action(self):
        """Declining confirms; an unknown action explains itself."""
        contact = ContactRequest.objects.create(
            event=self.event,
            sender=self.peer,
            recipient=self.member,
        )

        bad = reverse(
            "respond_to_contact_request",
            args=[contact.pk, "maybe"],
        )
        self.assertToast(
            self.post(bad),
            "Please try again.",
            "error",
        )

        decline = reverse(
            "respond_to_contact_request",
            args=[contact.pk, "decline"],
        )
        self.assertToast(
            self.post(decline),
            "Contact request declined.",
        )

    def test_contact_request_edge_cases(self):
        """Closed events, yourself, and open profiles each get a toast."""
        closed = reverse(
            "send_contact_request",
            args=[self.closed.pk, self.peer.pk],
        )
        self.assertToast(
            self.post(closed),
            "Browse upcoming events",
            "error",
        )

        EventInterest.objects.create(
            event=self.event,
            user=self.member,
        )
        own = reverse(
            "send_contact_request",
            args=[self.event.pk, self.member.pk],
        )
        self.member.share_profile_with_attendees = True
        self.member.save()

        self.assertToast(
            self.post(own),
            "to yourself",
            "error",
        )

    def test_open_profile_connects_straight_away(self):
        """Members who accept everyone are connected at once."""
        self.peer.contact_permission = ContactPermission.OPEN
        self.peer.save()

        EventInterest.objects.create(
            event=self.event,
            user=self.peer,
        )
        EventInterest.objects.create(
            event=self.event,
            user=self.member,
        )

        url = reverse(
            "send_contact_request",
            args=[self.event.pk, self.peer.pk],
        )
        self.assertToast(
            self.post(url),
            "connected. You can message them now.",
        )

    def test_registration_errors_say_what_to_do(self):
        """Registration errors explain the next step."""
        url = reverse(
            "event_register",
            args=[self.closed.pk],
        )
        self.assertToast(
            self.post(url),
            "Search for another upcoming event",
            "error",
        )

        self.event.host = self.member
        self.event.save()

        url = reverse(
            "event_register",
            args=[self.event.pk],
        )
        self.assertToast(
            self.post(url),
            "Use Manage event",
            "error",
        )

    def test_cancel_registration_and_report(self):
        """Cancelling and reporting show short confirmations."""
        self.post(
            reverse("event_register", args=[self.event.pk]),
            {"party_size": 1},
        )

        response = self.post(reverse("event_cancel_registration", args=[self.event.pk]))
        self.assertToast(
            response,
            "Registration cancelled.",
        )

        response = self.post(
            reverse("event_report", args=[self.event.pk]),
            {"reason": "spam_scam"},
        )
        self.assertToast(
            response,
            "Report sent.",
        )
