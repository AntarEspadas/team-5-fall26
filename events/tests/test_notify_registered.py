"""Registered users hear about event changes too, not only Interested (#160)."""

from django.core import mail
from django.urls import reverse

from events.models import EventInterest, Notification, Registration
from events.notifications import interested_recipients
from events.tests.test_notifications import NotifyTestCase, as_input


class RegisteredUsersAreNotifiedTests(NotifyTestCase):
    """Someone who registered without selecting Interested is still told."""

    def setUp(self):
        super().setUp()
        # One fan is only Interested, one is both, and the bystander only
        # registered. The host registering for their own event is ignored.
        Registration.objects.create(event=self.event, user=self.second_fan)
        Registration.objects.create(event=self.event, user=self.bystander)
        Registration.objects.create(event=self.event, user=self.host)

    def notified(self):
        """Return the usernames that got an in-app notification."""
        return set(Notification.objects.values_list("recipient__username", flat=True))

    def test_recipients_include_registered_users_once(self):
        recipients = list(interested_recipients(self.event))

        self.assertCountEqual(recipients, [self.fan, self.second_fan, self.bystander])

    def test_registered_only_user_gets_change_notice(self):
        self.client.force_login(self.host)
        response = self.client.post(
            reverse("event_edit", args=[self.event.pk]),
            {
                "title": self.event.title,
                "description": self.event.description,
                "start_time": "2099-01-15T17:30",
                "end_time": as_input(self.event.end_time),
                "location": self.event.location,
                "borough": self.event.borough,
                "categories": ["fitness"],
                "image_url": "",
                "registration_url": "",
            },
            follow=True,
        )

        self.assertContains(
            response, "Event updated. We let 3 people know about the change."
        )
        self.assertEqual(self.notified(), {"fan", "second_fan", "bystander"})
        self.assertEqual(
            sorted(email.to[0] for email in mail.outbox),
            ["bystander@example.com", "fan@example.com", "second_fan@example.com"],
        )

    def test_registered_only_user_gets_host_message(self):
        self.client.force_login(self.host)

        response = self.send()

        self.assertContains(response, "Your message was sent to 3 people.")
        self.assertIn("bystander", self.notified())

    def test_registered_only_user_hears_about_cancellation(self):
        self.client.force_login(self.host)

        self.client.post(
            reverse("event_cancel", args=[self.event.pk]), {"message": "Rain."}
        )

        self.assertIn("bystander", self.notified())
        self.assertEqual(len(mail.outbox), 3)

    def test_counts_on_pages_include_registered_users(self):
        EventInterest.objects.filter(event=self.event).delete()
        self.client.force_login(self.host)

        detail = self.client.get(self.detail_url)
        cancel = self.client.get(reverse("event_cancel", args=[self.event.pk]))

        self.assertEqual(detail.context["recipient_count"], 2)
        self.assertContains(detail, "Message attendees")
        self.assertEqual(cancel.context["recipient_count"], 2)
        self.assertContains(cancel, "registered or signed up as Interested")
