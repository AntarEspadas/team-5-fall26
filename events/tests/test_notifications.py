"""Tests for hosts notifying interested attendees and the notifications page."""

from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from django.contrib.auth import get_user_model
from django.core import mail
from django.test import RequestFactory, TestCase
from django.urls import reverse
from django.utils import timezone

from events.context_processors import notifications as notifications_context
from events.models import Event, EventInterest, Notification
from events.notifications import change_notice, describe_when

NYC = ZoneInfo("America/New_York")


def as_input(moment):
    """Format a datetime the way the form's calendar input submits it."""
    return timezone.localtime(moment, NYC).strftime("%Y-%m-%dT%H:%M")


class NotifyTestCase(TestCase):
    """A host, two interested fans, a bystander, and one hosted event."""

    @classmethod
    def setUpTestData(cls):
        User = get_user_model()

        def user(name):
            return User.objects.create_user(
                username=name, email=f"{name}@example.com", password="pass12345"
            )

        cls.host = user("host")
        cls.fan = user("fan")
        cls.second_fan = user("second_fan")
        cls.bystander = user("bystander")

    def setUp(self):
        self.event = self.hosted("yoga")
        for person in (self.fan, self.second_fan, self.host):
            EventInterest.objects.create(event=self.event, user=person)
        self.notify_url = reverse("event_notify", args=[self.event.pk])
        self.detail_url = reverse("event_detail", args=[self.event.pk])

    def hosted(self, guid, **fields):
        """Create an upcoming event posted by the host."""
        defaults = {
            "title": "Sunset Yoga",
            "description": "Bring a mat.",
            "start_time": datetime(2099, 1, 15, 18, 30, tzinfo=NYC),
            "end_time": datetime(2099, 1, 15, 19, 45, tzinfo=NYC),
            "location": "Pier 1",
            "borough": Event.Borough.BROOKLYN,
            "categories": "Fitness",
        }
        defaults.update(fields)
        return Event.objects.create(
            guid=Event.HOSTED_GUID_PREFIX + guid,
            host=self.host,
            content_hash="",
            **defaults,
        )

    def send(self, subject="Bring water", message="It will be hot."):
        return self.client.post(
            self.notify_url, {"subject": subject, "message": message}, follow=True
        )


class HostMessageTests(NotifyTestCase):
    """The host writes a message to everyone marked Interested."""

    def test_host_sees_form_and_recipient_count(self):
        self.client.force_login(self.host)
        response = self.client.get(self.detail_url)
        self.assertContains(response, "Message interested attendees")
        self.assertEqual(response.context["recipient_count"], 2)
        self.assertContains(response, self.notify_url)

    def test_others_do_not_see_form(self):
        self.client.force_login(self.fan)
        response = self.client.get(self.detail_url)
        self.assertNotContains(response, "Message interested attendees")
        self.assertNotIn("recipient_count", response.context)

    def test_message_reaches_interested_users_except_host(self):
        self.client.force_login(self.host)
        response = self.send()

        self.assertRedirects(response, self.detail_url)
        self.assertContains(response, "Your message was sent to 2 interested people.")
        self.assertEqual(
            sorted(email.to[0] for email in mail.outbox),
            ["fan@example.com", "second_fan@example.com"],
        )
        email = mail.outbox[0]
        self.assertEqual(email.subject, "Bring water")
        self.assertIn("It will be hot.", email.body)
        self.assertIn("http://testserver" + self.detail_url, email.body)
        self.assertEqual(
            set(Notification.objects.values_list("recipient__username", flat=True)),
            {"fan", "second_fan"},
        )

    def test_singular_count(self):
        EventInterest.objects.filter(user=self.second_fan).delete()
        self.client.force_login(self.host)
        self.assertContains(
            self.send(), "Your message was sent to 1 interested person."
        )

    def test_nobody_interested(self):
        EventInterest.objects.all().delete()
        self.client.force_login(self.host)
        response = self.send()
        self.assertContains(response, "Nobody is interested in this event yet.")
        self.assertEqual(mail.outbox, [])
        self.assertFalse(Notification.objects.exists())

    def test_blank_message_is_rejected(self):
        self.client.force_login(self.host)
        response = self.send(subject="", message="  ")
        self.assertContains(response, "Write a subject and a message before sending.")
        self.assertEqual(mail.outbox, [])

    def test_only_host_can_send(self):
        self.client.force_login(self.bystander)
        self.assertEqual(self.send().status_code, 404)
        self.assertEqual(mail.outbox, [])

    def test_cannot_send_after_event_ended(self):
        past = timezone.now() - timedelta(days=2)
        self.event.start_time = past
        self.event.end_time = past + timedelta(hours=1)
        self.event.save()
        self.client.force_login(self.host)
        self.assertEqual(self.send().status_code, 404)
        response = self.client.get(self.detail_url)
        self.assertNotContains(response, "Message interested attendees")

    def test_anonymous_redirected_to_login(self):
        response = self.client.post(self.notify_url, {"subject": "a", "message": "b"})
        self.assertEqual(response.status_code, 302)
        self.assertIn(reverse("login"), response["Location"])

    def test_get_not_allowed(self):
        self.client.force_login(self.host)
        self.assertEqual(self.client.get(self.notify_url).status_code, 405)


class ChangeNoticeTests(NotifyTestCase):
    """Editing the date, time, or place tells interested users automatically."""

    def edit(self, **overrides):
        data = {
            "title": self.event.title,
            "description": self.event.description,
            "start_time": as_input(self.event.start_time),
            "end_time": as_input(self.event.end_time),
            "location": self.event.location,
            "borough": self.event.borough,
            "categories": ["fitness"],
            "image_url": "",
            "registration_url": "",
        }
        data.update(overrides)
        self.client.force_login(self.host)
        return self.client.post(
            reverse("event_edit", args=[self.event.pk]), data, follow=True
        )

    def test_time_change_notifies(self):
        response = self.edit(start_time="2099-01-15T17:30")

        self.assertContains(
            response, "Event updated. We let 2 interested people know about the change."
        )
        self.assertEqual(len(mail.outbox), 2)
        email = mail.outbox[0]
        self.assertEqual(email.subject, "Update: Sunset Yoga has changed")
        self.assertIn(
            "When: Thursday, January 15, 2099 at 5:30 PM – 7:45 PM "
            "(was Thursday, January 15, 2099 at 6:30 PM – 7:45 PM)",
            email.body,
        )
        self.assertEqual(Notification.objects.count(), 2)

    def test_location_change_notifies(self):
        self.edit(location="Pier 6")
        self.assertIn("Where: Pier 6 (was Pier 1)", mail.outbox[0].body)

    def test_other_changes_do_not_notify(self):
        response = self.edit(title="Sunrise Yoga")
        self.assertContains(response, "Event updated.")
        self.assertNotContains(response, "know about the change")
        self.assertEqual(mail.outbox, [])
        self.assertFalse(Notification.objects.exists())

    def test_change_with_nobody_interested(self):
        EventInterest.objects.all().delete()
        response = self.edit(location="Pier 6")
        self.assertNotContains(response, "know about the change")
        self.assertEqual(mail.outbox, [])


class NotificationPageTests(NotifyTestCase):
    """Signed-in users read their notifications, newest first."""

    def notify(self, subject, minutes_ago=0):
        notification = Notification.objects.create(
            recipient=self.fan, event=self.event, subject=subject, body="Details"
        )
        Notification.objects.filter(pk=notification.pk).update(
            created_at=timezone.now() - timedelta(minutes=minutes_ago)
        )
        return notification

    def test_lists_newest_first_and_links_to_event(self):
        self.notify("Older", minutes_ago=30)
        self.notify("Newer")
        self.client.force_login(self.fan)
        response = self.client.get(reverse("notifications"))

        subjects = [n.subject for n in response.context["notifications"]]
        self.assertEqual(subjects, ["Newer", "Older"])
        self.assertContains(response, self.detail_url)
        self.assertContains(response, "New</span>", count=2)

    def test_opening_marks_read(self):
        notification = self.notify("Hello")
        self.client.force_login(self.fan)
        self.client.get(reverse("notifications"))
        notification.refresh_from_db()
        self.assertIsNotNone(notification.read_at)

        response = self.client.get(reverse("notifications"))
        self.assertNotContains(response, "New</span>")

    def test_only_own_notifications(self):
        self.notify("For the fan")
        self.client.force_login(self.bystander)
        response = self.client.get(reverse("notifications"))
        self.assertNotContains(response, "For the fan")
        self.assertContains(response, "No notifications yet.")

    def test_requires_login(self):
        response = self.client.get(reverse("notifications"))
        self.assertEqual(response.status_code, 302)
        self.assertIn(reverse("login"), response["Location"])

    def test_home_shows_unread_count(self):
        self.notify("One")
        self.notify("Two")
        self.client.force_login(self.fan)
        response = self.client.get(reverse("landing"))
        self.assertContains(response, reverse("notifications"))
        self.assertContains(response, "2 new")

        self.client.get(reverse("notifications"))
        response = self.client.get(reverse("landing"))
        self.assertNotContains(response, "2 new")

    def test_context_processor_skips_anonymous_users(self):
        request = RequestFactory().get("/")
        request.user = type("Anon", (), {"is_authenticated": False})()
        self.assertEqual(notifications_context(request), {})


class MessageWordingTests(NotifyTestCase):
    """The text used in automatic change notices."""

    def test_describe_when(self):
        start = datetime(2099, 1, 15, 18, 30, tzinfo=NYC)
        self.assertEqual(describe_when(None, None), "To be announced")
        self.assertEqual(
            describe_when(start, None), "Thursday, January 15, 2099 at 6:30 PM"
        )

    def test_notification_str(self):
        notification = Notification(
            recipient=self.fan, event=self.event, subject="Hi", body=""
        )
        self.assertEqual(str(notification), "Hi → fan")

    def test_no_notice_without_time_or_place_change(self):
        self.assertIsNone(change_notice(self.event, {}, ["title", "description"]))

    def test_notice_names_host(self):
        self.host.display_name = "Park Pals"
        self.host.save()
        previous = {"location": "Pier 1"}
        self.event.location = "Pier 6"
        subject, body = change_notice(self.event, previous, ["location"])
        self.assertTrue(body.startswith("Park Pals changed the details of Sunset Yoga"))
