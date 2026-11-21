"""Tests for a host removing a guest from their event (#162)."""

from datetime import timedelta

from django.contrib.auth import get_user_model
from django.core import mail
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from events.models import Event, Notification, Registration


class RemoveAttendeeTests(TestCase):
    """The host can take a registered guest off the list of an open event."""

    @classmethod
    def setUpTestData(cls):
        User = get_user_model()

        def user(name):
            return User.objects.create_user(
                username=name, email=f"{name}@example.com", password="pass12345"
            )

        cls.host = user("host")
        cls.guest = user("guest")
        cls.other_guest = user("other_guest")
        cls.stranger = user("stranger")

    def setUp(self):
        start = timezone.now() + timedelta(days=3)
        self.event = Event.objects.create(
            guid=Event.HOSTED_GUID_PREFIX + "picnic",
            host=self.host,
            title="Park Picnic",
            start_time=start,
            end_time=start + timedelta(hours=2),
            location="Great Lawn",
            content_hash="",
        )
        self.registration = Registration.objects.create(
            event=self.event, user=self.guest, party_size=3
        )
        self.other = Registration.objects.create(
            event=self.event, user=self.other_guest
        )
        self.manage_url = reverse("event_manage", args=[self.event.pk])

    def remove_url(self, registration=None):
        registration = registration or self.registration
        return reverse("event_remove_attendee", args=[self.event.pk, registration.pk])

    def remove(self, user=None, **kwargs):
        self.client.force_login(user or self.host)
        return self.client.post(self.remove_url(), **kwargs)

    def test_host_removes_guest(self):
        response = self.remove(follow=True)

        self.assertRedirects(response, self.manage_url + "?tab=attendees")
        self.assertFalse(Registration.objects.filter(pk=self.registration.pk).exists())
        self.assertTrue(Registration.objects.filter(pk=self.other.pk).exists())
        self.assertContains(
            response, "guest was removed from the guest list and has been notified."
        )
        self.assertEqual(list(response.context["attendees"]), [self.other])

    def test_guest_is_notified_in_app_and_by_email(self):
        self.remove()

        notification = Notification.objects.get(recipient=self.guest)
        self.assertEqual(notification.event, self.event)
        self.assertEqual(notification.subject, "Removed from Park Picnic")
        self.assertIn("host removed you from the guest list", notification.body)
        self.assertEqual(len(mail.outbox), 1)
        self.assertEqual(mail.outbox[0].to, ["guest@example.com"])
        self.assertIn("You are no longer registered", mail.outbox[0].body)
        self.assertIn(
            reverse("event_detail", args=[self.event.pk]), mail.outbox[0].body
        )

    def test_guest_without_email_still_gets_notification(self):
        get_user_model().objects.filter(pk=self.guest.pk).update(email="")

        self.remove()

        self.assertTrue(Notification.objects.filter(recipient=self.guest).exists())
        self.assertEqual(mail.outbox, [])

    def test_only_the_host_can_remove(self):
        for user in (self.stranger, self.other_guest, self.guest):
            with self.subTest(user=user.username):
                response = self.remove(user)

                self.assertEqual(response.status_code, 404)
        self.assertTrue(Registration.objects.filter(pk=self.registration.pk).exists())

    def test_ended_or_cancelled_events_cannot_be_changed(self):
        for status in (Event.Status.DONE, Event.Status.CANCELLED):
            with self.subTest(status=status):
                Event.objects.filter(pk=self.event.pk).update(status=status)

                self.assertEqual(self.remove().status_code, 404)
        self.assertTrue(Registration.objects.filter(pk=self.registration.pk).exists())

    def test_registration_from_another_event_is_not_removed(self):
        elsewhere = Event.objects.create(
            guid=Event.HOSTED_GUID_PREFIX + "other",
            host=self.stranger,
            title="Elsewhere",
            start_time=self.event.start_time,
            content_hash="",
        )
        theirs = Registration.objects.create(event=elsewhere, user=self.guest)
        self.client.force_login(self.host)

        response = self.client.post(self.remove_url(theirs), follow=True)

        self.assertContains(response, "That guest is no longer registered.")
        self.assertTrue(Registration.objects.filter(pk=theirs.pk).exists())
        self.assertFalse(Notification.objects.exists())

    def test_removing_twice_is_harmless(self):
        self.remove()
        response = self.remove(follow=True)

        self.assertContains(response, "That guest is no longer registered.")
        self.assertEqual(Notification.objects.filter(recipient=self.guest).count(), 1)

    def test_get_is_not_allowed(self):
        self.client.force_login(self.host)

        self.assertEqual(self.client.get(self.remove_url()).status_code, 405)

    def test_manage_page_shows_remove_only_for_open_events(self):
        self.client.force_login(self.host)

        response = self.client.get(self.manage_url)
        self.assertContains(response, self.remove_url())
        self.assertContains(response, "Remove guest")

        Event.objects.filter(pk=self.event.pk).update(status=Event.Status.DONE)
        response = self.client.get(self.manage_url)
        self.assertNotContains(response, self.remove_url())
