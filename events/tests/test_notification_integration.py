"""Keep upstream attendance and discussion features working with request alerts."""

from itertools import product
from unittest.mock import patch

from django.core import mail
from django.urls import reverse

from events.models import ContactRequest, Event, EventInterest, EventPost, EventReply
from events.models import Notification
from events.notifications import interested_recipients, notify_contact_request
from events.tests.test_notifications import NotifyTestCase


class NotificationIntegrationTests(NotifyTestCase):
    """Exercise the combined feature through its public views."""

    def setUp(self):
        super().setUp()
        self.client.force_login(self.host)
        self.fan.share_profile_with_attendees = True
        self.fan.save()
        self.contact_url = reverse(
            "send_contact_request", args=[self.event.pk, self.fan.pk]
        )
        self.post = EventPost.objects.create(
            event=self.event, author=self.fan, body="What should I bring?"
        )
        self.reply_url = reverse("create_event_reply", args=[self.post.pk])

    def test_reply_alert_becomes_read_while_request_stays_new(self):
        contact_request = ContactRequest.objects.create(
            event=self.event, sender=self.host, recipient=self.fan
        )
        request_alert = notify_contact_request(contact_request)
        self.assertRedirects(
            self.client.post(self.reply_url, {"body": "Bring water."}), self.detail_url
        )
        reply_alert = Notification.objects.exclude(pk=request_alert.pk).get()
        self.assertEqual(reply_alert.recipient, self.fan)
        self.assertEqual(reply_alert.event, self.event)
        self.assertIsNone(reply_alert.contact_request_id)
        self.assertIn("New reply to your comment", reply_alert.subject)
        self.assertIn("host replied", reply_alert.body)
        self.client.force_login(self.fan)
        response = self.client.get(reverse("notifications"))
        self.assertEqual(
            [n.pk for n in response.context["notifications"]],
            [request_alert.pk, reply_alert.pk],
        )
        request_alert.refresh_from_db()
        reply_alert.refresh_from_db()
        self.assertIsNone(request_alert.read_at)
        self.assertIsNotNone(reply_alert.read_at)
        self.assertContains(self.client.get(reverse("landing")), "1 new")
        self.assertEqual(mail.outbox, [])

    def test_self_invalid_and_closed_event_replies_do_not_notify(self):
        self.client.force_login(self.fan)
        self.client.post(self.reply_url, {"body": "My own follow-up"})
        self.client.post(self.reply_url, {"body": "   "})
        self.event.status = Event.Status.DONE
        self.event.save()
        self.client.force_login(self.host)
        self.client.post(self.reply_url, {"body": "Too late"})
        self.assertEqual(EventReply.objects.count(), 1)
        self.assertFalse(Notification.objects.exists())

    def test_reply_notification_failure_rolls_back_reply(self):
        with patch(
            "events.community_views.Notification.objects.create",
            side_effect=RuntimeError("reply alert failure"),
        ):
            with self.assertRaisesMessage(RuntimeError, "reply alert failure"):
                self.client.post(self.reply_url, {"body": "Bring water."})
        self.assertFalse(EventReply.objects.exists())
        self.assertFalse(Notification.objects.exists())

    def test_reply_for_long_event_title_fits_notification_subject(self):
        self.event.title = "A" * 300
        self.event.save()
        self.assertRedirects(
            self.client.post(self.reply_url, {"body": "Bring water."}), self.detail_url
        )
        alert = Notification.objects.get()
        self.assertEqual(len(alert.subject), 200)
        self.assertIn(self.event.title, alert.body)
        self.assertEqual(EventReply.objects.count(), 1)

    def test_both_intentions_support_requests_and_resolved_card_status(self):
        choices = EventInterest.Status.values
        for sender_status, recipient_status in product(choices, repeat=2):
            with self.subTest(sender=sender_status, recipient=recipient_status):
                ContactRequest.objects.all().delete()
                EventInterest.objects.filter(user=self.host).update(
                    status=sender_status
                )
                EventInterest.objects.filter(user=self.fan).update(
                    status=recipient_status
                )
                self.client.force_login(self.host)
                page = self.client.get(self.detail_url)
                self.assertContains(
                    page, reverse("public_profile", args=[self.fan.username])
                )
                group = (
                    "interested_users"
                    if recipient_status == EventInterest.Status.INTERESTED
                    else "want_to_attend_users"
                )
                self.assertEqual(page.context[group][0]["user"], self.fan)
                self.assertRedirects(
                    self.client.post(self.contact_url, {"message": "Hello"}),
                    self.detail_url,
                )
                self.assertContains(
                    self.client.get(self.detail_url), "awaiting response."
                )
                contact_request = ContactRequest.objects.get()
                alert = Notification.objects.get()
                self.assertIsNone(alert.read_at)
                self.client.force_login(self.fan)
                self.assertRedirects(
                    self.client.post(
                        reverse(
                            "respond_to_contact_request",
                            args=[contact_request.pk, "accept"],
                        ),
                        {"return_to": "notifications"},
                    ),
                    reverse("notifications"),
                )
                alert.refresh_from_db()
                self.assertIsNotNone(alert.read_at)
                self.client.force_login(self.host)
                page = self.client.get(self.detail_url)
                self.assertContains(page, "Contact request accepted.")
                self.assertNotContains(page, f'action="{self.contact_url}"')

    def test_want_to_attend_users_receive_host_announcements(self):
        EventInterest.objects.filter(user=self.fan).update(
            status=EventInterest.Status.WANT_TO_ATTEND
        )
        self.assertEqual(
            set(interested_recipients(self.event)), {self.fan, self.second_fan}
        )
        self.send()
        self.assertEqual(
            set(Notification.objects.values_list("recipient_id", flat=True)),
            {self.fan.pk, self.second_fan.pk},
        )
        self.assertEqual(len(mail.outbox), 2)

    def test_intention_switch_and_removal_do_not_resolve_request(self):
        self.client.post(self.contact_url)
        alert = Notification.objects.get()
        self.client.force_login(self.fan)
        url = reverse("toggle_event_interest", args=[self.event.pk])
        self.client.post(url, {"status": EventInterest.Status.WANT_TO_ATTEND})
        self.assertEqual(
            EventInterest.objects.get(user=self.fan).status,
            EventInterest.Status.WANT_TO_ATTEND,
        )
        self.client.post(url, {"status": EventInterest.Status.INTERESTED})
        self.assertEqual(
            EventInterest.objects.get(user=self.fan).status,
            EventInterest.Status.INTERESTED,
        )
        self.client.post(url, {"status": EventInterest.Status.INTERESTED})
        self.assertFalse(EventInterest.objects.filter(user=self.fan).exists())
        alert.refresh_from_db()
        self.assertIsNone(alert.read_at)
        self.assertEqual(alert.contact_request.status, ContactRequest.Status.PENDING)

    def test_invalid_intention_is_ignored(self):
        self.client.post(
            reverse("toggle_event_interest", args=[self.event.pk]),
            {"status": "invalid"},
        )
        self.assertEqual(
            EventInterest.objects.get(user=self.host).status,
            EventInterest.Status.INTERESTED,
        )

    def test_cards_hide_request_form_for_nonparticipants_and_closed_events(self):
        self.client.force_login(self.bystander)
        response = self.client.get(self.detail_url)
        self.assertNotContains(response, f'action="{self.contact_url}"')
        self.assertContains(response, "Select Interested or Want to Attend")
        self.client.force_login(self.host)
        self.host.share_profile_with_attendees = True
        self.host.save()
        self.assertContains(self.client.get(self.detail_url), "This is your profile.")
        self.event.status = Event.Status.DONE
        self.event.save()
        response = self.client.get(self.detail_url)
        self.assertNotContains(response, f'action="{self.contact_url}"')
        self.assertContains(response, "New contact requests are closed")

    def test_declined_card_allows_new_request_and_anonymous_card_requires_login(self):
        self.client.post(self.contact_url)
        contact_request = ContactRequest.objects.get()
        self.client.force_login(self.fan)
        self.client.post(
            reverse("respond_to_contact_request", args=[contact_request.pk, "decline"])
        )
        self.client.force_login(self.host)
        self.assertContains(
            self.client.get(self.detail_url), f'action="{self.contact_url}"'
        )
        self.client.logout()
        response = self.client.get(self.detail_url)
        self.assertNotContains(response, f'action="{self.contact_url}"')
        self.assertContains(response, "Log in to Contact")
