"""Contact request delivery, resolution, ordering, and transaction tests."""

from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from threading import Barrier
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.core import mail
from django.db import close_old_connections, connections
from django.http import Http404
from django.test import Client, RequestFactory, TransactionTestCase
from django.urls import reverse
from django.utils import timezone

from accounts.models import ContactPermission
from events.community_views import respond_to_contact_request, send_contact_request
from events.models import ContactRequest, Event, EventInterest, Notification
from events.notifications import notify_contact_request
from events.tests.test_notifications import NotifyTestCase


class ContactNotificationTests(NotifyTestCase):
    """Incoming requests remain new until the recipient resolves them."""

    def setUp(self):
        super().setUp()
        self.fan.share_profile_with_attendees = True
        self.fan.save()
        self.client.force_login(self.host)
        self.send_url = reverse(
            "send_contact_request", args=[self.event.pk, self.fan.pk]
        )

    def incoming(self, status=ContactRequest.Status.PENDING, **kwargs):
        contact_request = ContactRequest.objects.create(
            event=self.event,
            sender=self.host,
            recipient=self.fan,
            status=status,
            **kwargs,
        )
        return contact_request, notify_contact_request(contact_request)

    def response_url(self, contact_request, action="accept"):
        return reverse("respond_to_contact_request", args=[contact_request.pk, action])

    def test_send_creates_recipient_only_notification_without_email(self):
        self.host.display_name = "Park Pals"
        self.host.save()
        response = self.client.post(self.send_url, {"message": "Hello!"})
        self.assertRedirects(response, self.detail_url)
        notification = Notification.objects.get()
        self.assertEqual(notification.recipient, self.fan)
        self.assertEqual(notification.event, self.event)
        self.assertEqual(notification.contact_request, ContactRequest.objects.get())
        self.assertEqual(notification.subject, "Contact request from Park Pals")
        self.assertEqual(notification.body, "Hello!")
        self.assertIsNone(notification.read_at)
        self.assertFalse(self.host.notifications.exists())
        self.assertEqual(mail.outbox, [])

    def test_optional_message_and_idempotent_helper(self):
        self.client.post(self.send_url)
        contact_request = ContactRequest.objects.get()
        first = Notification.objects.get()
        again = notify_contact_request(contact_request)
        self.assertEqual(first.pk, again.pk)
        self.assertEqual(first.body, "")
        self.assertEqual(Notification.objects.count(), 1)

    def test_open_contact_is_read_introduction(self):
        self.fan.contact_permission = ContactPermission.OPEN
        self.fan.save()
        self.client.post(self.send_url, {"message": "Hello!"})
        notification = Notification.objects.get()
        self.assertEqual(notification.subject, "Introduction from host")
        self.assertIsNotNone(notification.read_at)
        self.assertEqual(notification.contact_request.status, "accepted")
        self.client.force_login(self.fan)
        response = self.client.get(reverse("notifications"))
        self.assertContains(response, "Status: Accepted")
        self.assertContains(response, f'href="/messages/{self.host.pk}/"')
        self.assertNotContains(response, "New</span>")
        self.assertNotContains(response, ">Accept</button>")
        self.assertNotContains(response, ">Decline</button>")
        self.assertFalse(self.host.notifications.exists())
        self.assertEqual(mail.outbox, [])

    def test_duplicate_submissions_and_retry_after_decline(self):
        self.client.post(self.send_url)
        self.client.post(self.send_url)
        self.assertEqual(ContactRequest.objects.count(), 1)
        self.assertEqual(Notification.objects.count(), 1)
        contact_request = ContactRequest.objects.get()
        self.client.force_login(self.fan)
        self.client.post(self.response_url(contact_request, "decline"))
        self.client.force_login(self.host)
        self.client.post(self.send_url)
        self.assertEqual(ContactRequest.objects.count(), 2)
        self.assertEqual(Notification.objects.count(), 2)
        self.assertEqual(Notification.objects.filter(read_at__isnull=True).count(), 1)
        latest = ContactRequest.objects.get(status="pending")
        self.client.force_login(self.fan)
        self.client.post(self.response_url(latest))
        self.client.force_login(self.host)
        self.client.post(self.send_url)
        self.assertEqual(Notification.objects.count(), 2)

    def test_profile_link_for_each_state_and_message_only_when_accepted(self):
        self.host.display_name = "Park Pals"
        self.host.save()
        self.client.force_login(self.fan)
        profile_url = reverse("public_profile", args=[self.host.username])
        message_url = f"/messages/{self.host.pk}/"
        for status in ContactRequest.Status.values:
            with self.subTest(status=status):
                contact_request, notification = self.incoming(status=status)
                response = self.client.get(reverse("notifications"))
                self.assertContains(
                    response, f'<a href="{profile_url}">Park Pals</a>', html=True
                )
                if status == ContactRequest.Status.ACCEPTED:
                    self.assertContains(response, f'href="{message_url}"')
                    self.assertContains(response, "Message</a>")
                    self.assertNotContains(response, "<button")
                else:
                    self.assertNotContains(response, f'href="{message_url}"')
                if status == ContactRequest.Status.PENDING:
                    notification.refresh_from_db()
                    self.assertIsNone(notification.read_at)
                    self.assertContains(response, "New</span>")
                contact_request.delete()

    def test_profile_link_preserves_existing_visibility_rules(self):
        self.incoming()
        self.client.force_login(self.fan)
        profile_url = reverse("public_profile", args=[self.host.username])
        self.assertContains(
            self.client.get(reverse("notifications")), f'href="{profile_url}"'
        )
        response = self.client.get(profile_url)
        self.assertTemplateUsed(response, "accounts/profile_unavailable.html")
        self.host.share_profile_with_attendees = True
        self.host.save()
        response = self.client.get(profile_url)
        self.assertTemplateUsed(response, "accounts/public_profile.html")
        self.assertEqual(response.context["profile_user"], self.host)

    def test_message_link_appears_after_acceptance_from_either_page(self):
        self.client.force_login(self.fan)
        message_url = f"/messages/{self.host.pk}/"
        for return_to in ("notifications", ""):
            with self.subTest(return_to=return_to):
                contact_request, notification = self.incoming()
                self.assertNotContains(
                    self.client.get(reverse("notifications")), f'href="{message_url}"'
                )
                response = self.client.post(
                    self.response_url(contact_request), {"return_to": return_to}
                )
                self.assertRedirects(
                    response,
                    reverse("notifications") if return_to else self.detail_url,
                )
                self.assertContains(
                    self.client.get(reverse("notifications")), f'href="{message_url}"'
                )
                notification.refresh_from_db()
                self.assertIsNotNone(notification.read_at)
                contact_request.delete()

    def test_event_notification_does_not_show_contact_actions(self):
        Notification.objects.create(
            recipient=self.fan, event=self.event, subject="Event update", body="Details"
        )
        self.client.force_login(self.fan)
        response = self.client.get(reverse("notifications"))
        self.assertNotContains(response, 'class="notification-message"')
        self.assertNotContains(
            response, reverse("public_profile", args=[self.host.username])
        )

    def test_invalid_requests_do_not_notify(self):
        self.client.post(self.send_url, {"message": "x" * 501})
        self.client.post(
            reverse("send_contact_request", args=[self.event.pk, self.host.pk])
        )
        EventInterest.objects.filter(event=self.event, user=self.host).delete()
        self.client.post(self.send_url)
        self.assertFalse(ContactRequest.objects.exists())
        self.assertFalse(Notification.objects.exists())

    def test_nonsharing_recipient_and_closed_event_do_not_notify(self):
        self.fan.share_profile_with_attendees = False
        self.fan.save()
        self.assertEqual(self.client.post(self.send_url).status_code, 404)
        self.fan.share_profile_with_attendees = True
        self.fan.save()
        self.event.status = Event.Status.DONE
        self.event.save()
        self.client.post(self.send_url)
        self.assertFalse(Notification.objects.exists())

    def test_notification_failure_rolls_back_new_request(self):
        with patch(
            "events.community_views.notify_contact_request",
            side_effect=RuntimeError("notification failure"),
        ):
            with self.assertRaisesMessage(RuntimeError, "notification failure"):
                self.client.post(self.send_url)
        self.assertFalse(ContactRequest.objects.exists())
        self.assertFalse(Notification.objects.exists())

    def test_pending_stays_new_across_visits_and_in_badge(self):
        contact_request, notification = self.incoming(
            message="<script>alert(1)</script>"
        )
        event_notification = Notification.objects.create(
            recipient=self.fan, event=self.event, subject="Event update", body="Details"
        )
        self.client.force_login(self.fan)
        for _ in range(2):
            response = self.client.get(reverse("notifications"))
            self.assertContains(response, "Status: Pending")
            self.assertContains(response, self.response_url(contact_request))
            self.assertContains(response, self.response_url(contact_request, "decline"))
            self.assertContains(response, 'name="return_to" value="notifications"')
            self.assertContains(response, "&lt;script&gt;alert(1)&lt;/script&gt;")
        notification.refresh_from_db()
        event_notification.refresh_from_db()
        self.assertIsNone(notification.read_at)
        self.assertIsNotNone(event_notification.read_at)
        self.assertContains(response, "New</span>", count=1)
        self.assertContains(self.client.get(reverse("landing")), "1 new")
        contact_request.refresh_from_db()
        self.assertEqual(contact_request.status, "pending")

    def test_responses_from_both_pages_resolve_and_mark_read(self):
        for action in ("accept", "decline"):
            for return_to in ("notifications", "", "https://example.com"):
                with self.subTest(action=action, return_to=return_to):
                    contact_request, notification = self.incoming()
                    self.client.force_login(self.fan)
                    response = self.client.post(
                        self.response_url(contact_request, action),
                        {"return_to": return_to},
                    )
                    expected = (
                        reverse("notifications")
                        if return_to == "notifications"
                        else self.detail_url
                    )
                    self.assertRedirects(response, expected)
                    contact_request.refresh_from_db()
                    notification.refresh_from_db()
                    self.assertEqual(
                        contact_request.status,
                        "accepted" if action == "accept" else "declined",
                    )
                    self.assertIsNotNone(notification.read_at)
        page = self.client.get(reverse("notifications"))
        self.assertContains(page, "Status: Accepted")
        self.assertContains(page, "Status: Declined")
        self.assertNotContains(page, "New</span>")
        self.assertNotContains(page, "<button")
        self.assertFalse(self.host.notifications.exists())
        self.assertNotContains(self.client.get(reverse("landing")), "1 new")
        self.assertEqual(mail.outbox, [])

    def test_response_permissions_methods_csrf_and_replay(self):
        contact_request, notification = self.incoming()
        url = self.response_url(contact_request)
        self.assertEqual(self.client.post(url).status_code, 404)
        self.client.force_login(self.fan)
        self.assertEqual(self.client.get(url).status_code, 405)
        csrf_client = Client(enforce_csrf_checks=True)
        csrf_client.force_login(self.fan)
        self.assertEqual(csrf_client.post(url).status_code, 403)
        response = self.client.post(
            self.response_url(contact_request, "unknown"),
            {"return_to": "notifications"},
        )
        self.assertRedirects(response, reverse("notifications"))
        contact_request.refresh_from_db()
        notification.refresh_from_db()
        self.assertEqual(contact_request.status, "pending")
        self.assertIsNone(notification.read_at)
        self.client.post(url)
        notification.refresh_from_db()
        resolved_at = notification.read_at
        self.assertEqual(self.client.post(url).status_code, 404)
        notification.refresh_from_db()
        self.assertEqual(notification.read_at, resolved_at)
        self.client.logout()
        self.assertEqual(self.client.post(url).status_code, 302)

    def test_resolution_failure_rolls_back_status_and_read_state(self):
        contact_request, notification = self.incoming()
        self.client.force_login(self.fan)
        with patch(
            "django.db.models.query.QuerySet.update",
            side_effect=RuntimeError("read state failure"),
        ):
            with self.assertRaisesMessage(RuntimeError, "read state failure"):
                self.client.post(self.response_url(contact_request))
        contact_request.refresh_from_db()
        notification.refresh_from_db()
        self.assertEqual(contact_request.status, "pending")
        self.assertIsNone(notification.read_at)

    def test_recipient_isolation(self):
        self.incoming(message="Private introduction")
        self.client.force_login(self.bystander)
        page = self.client.get(reverse("notifications"))
        self.assertNotContains(page, "Private introduction")
        self.assertContains(page, "No notifications yet.")

    def test_requests_first_with_stable_ties_across_pages(self):
        timestamp = timezone.now() - timedelta(days=2)
        request_notifications = [
            self.incoming(status="pending" if i % 2 else "accepted")[1]
            for i in range(21)
        ]
        Notification.objects.filter(
            pk__in=[n.pk for n in request_notifications]
        ).update(created_at=timestamp)
        event_notifications = [
            Notification.objects.create(
                recipient=self.fan, event=self.event, subject=f"Update {i}", body=""
            )
            for i in range(2)
        ]
        Notification.objects.filter(pk__in=[n.pk for n in event_notifications]).update(
            created_at=timezone.now()
        )
        # A newer request sorts ahead of equal-time requests in its own group.
        newest = request_notifications[0]
        Notification.objects.filter(pk=newest.pk).update(
            created_at=timestamp + timedelta(hours=1)
        )
        expected = (
            [newest.pk]
            + sorted([n.pk for n in request_notifications[1:]], reverse=True)
            + sorted([n.pk for n in event_notifications], reverse=True)
        )
        self.client.force_login(self.fan)
        pages = [
            self.client.get(reverse("notifications")),
            self.client.get(reverse("notifications"), {"page": 2}),
        ]
        actual = [n.pk for page in pages for n in page.context["notifications"]]
        self.assertEqual(actual, expected)
        self.assertEqual(len(pages[0].context["notifications"]), 20)
        self.assertEqual(len(pages[1].context["notifications"]), 3)

    def test_undisplayed_event_updates_stay_unread(self):
        for _ in range(20):
            self.incoming()
        event_notification = Notification.objects.create(
            recipient=self.fan, event=self.event, subject="Event update", body=""
        )
        self.client.force_login(self.fan)
        self.client.get(reverse("notifications"))
        event_notification.refresh_from_db()
        self.assertIsNone(event_notification.read_at)
        self.assertEqual(
            self.fan.notifications.filter(read_at__isnull=True).count(), 21
        )
        self.client.get(reverse("notifications"), {"page": 2})
        event_notification.refresh_from_db()
        self.assertIsNotNone(event_notification.read_at)
        self.assertEqual(
            self.fan.notifications.filter(read_at__isnull=True).count(), 20
        )

    def test_deleting_request_removes_only_its_notification(self):
        contact_request, notification = self.incoming()
        event_notification = Notification.objects.create(
            recipient=self.fan, event=self.event, subject="Event update", body=""
        )
        contact_request.delete()
        self.assertFalse(Notification.objects.filter(pk=notification.pk).exists())
        self.assertTrue(Notification.objects.filter(pk=event_notification.pk).exists())


class ConcurrentContactNotificationTests(TransactionTestCase):
    """Use independent database connections to exercise actual row locks."""

    def setUp(self):
        user_model = get_user_model()
        self.sender = user_model.objects.create_user(
            username="sender", email="sender@example.com"
        )
        self.recipient = user_model.objects.create_user(
            username="recipient",
            email="recipient@example.com",
            share_profile_with_attendees=True,
        )
        self.event = Event.objects.create(
            guid="concurrent", title="Concurrent requests", content_hash="v" * 64
        )
        for user in (self.sender, self.recipient):
            EventInterest.objects.create(event=self.event, user=user)

    def run_concurrently(self, view, submissions):
        barrier = Barrier(2)

        def submit(user_id, args):
            close_old_connections()
            try:
                request = RequestFactory().post("/")
                request.user = get_user_model().objects.get(pk=user_id)
                barrier.wait(timeout=10)
                try:
                    return view(request, *args).status_code
                except Http404:
                    return 404
            finally:
                connections["default"].close()

        with ThreadPoolExecutor(max_workers=2) as executor:
            futures = [
                executor.submit(submit, user_id, args) for user_id, args in submissions
            ]
            return [future.result(timeout=30) for future in futures]

    def test_simultaneous_sends_and_responses(self):
        results = self.run_concurrently(
            send_contact_request,
            [(self.sender.pk, (self.event.pk, self.recipient.pk))] * 2,
        )
        self.assertEqual(results, [302, 302])
        self.assertEqual(ContactRequest.objects.count(), 1)
        self.assertEqual(Notification.objects.count(), 1)
        contact_request = ContactRequest.objects.get()
        results = self.run_concurrently(
            respond_to_contact_request,
            [
                (self.recipient.pk, (contact_request.pk, "accept")),
                (self.recipient.pk, (contact_request.pk, "decline")),
            ],
        )
        self.assertEqual(sorted(results), [302, 404])
        contact_request.refresh_from_db()
        self.assertIn(contact_request.status, ("accepted", "declined"))
        self.assertIsNotNone(Notification.objects.get().read_at)

    def test_reciprocal_requests_do_not_deadlock(self):
        self.sender.share_profile_with_attendees = True
        self.sender.save()
        delivery_barrier = Barrier(2)

        def deliver(contact_request):
            notification = notify_contact_request(contact_request)
            # Both sender locks remain held until the transactions commit.
            delivery_barrier.wait(timeout=10)
            return notification

        with patch(
            "events.community_views.notify_contact_request", side_effect=deliver
        ):
            results = self.run_concurrently(
                send_contact_request,
                [
                    (self.sender.pk, (self.event.pk, self.recipient.pk)),
                    (self.recipient.pk, (self.event.pk, self.sender.pk)),
                ],
            )
        self.assertEqual(results, [302, 302])
        self.assertEqual(ContactRequest.objects.count(), 2)
        self.assertEqual(Notification.objects.filter(read_at__isnull=True).count(), 2)
