"""Tests for hosts cancelling their events and the My events archive."""

from datetime import datetime, timedelta
from unittest.mock import patch
from zoneinfo import ZoneInfo

from django.contrib.auth import get_user_model
from django.core import mail
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from events.models import Event, EventInterest, Notification
from events.notifications import cancel_notice, notify_interested

NYC = ZoneInfo("America/New_York")


class CancelTestCase(TestCase):
    """A host, a fan of each interest kind, a bystander, and a hosted event."""

    @classmethod
    def setUpTestData(cls):
        User = get_user_model()

        def user(name, **fields):
            return User.objects.create_user(
                username=name,
                email=f"{name}@example.com",
                password="pass12345",
                **fields,
            )

        cls.host = user("host", display_name="Park Pals")
        cls.fan = user("fan")
        cls.goer = user("goer")
        cls.bystander = user("bystander")

    def setUp(self):
        self.event = self.hosted("yoga")
        EventInterest.objects.create(event=self.event, user=self.fan)
        EventInterest.objects.create(
            event=self.event,
            user=self.goer,
            status=EventInterest.Status.WANT_TO_ATTEND,
        )
        EventInterest.objects.create(event=self.event, user=self.host)
        self.cancel_url = reverse("event_cancel", args=[self.event.pk])
        self.detail_url = reverse("event_detail", args=[self.event.pk])

    def hosted(self, guid, host=None, **fields):
        """Create an upcoming event posted by the host."""
        defaults = {
            "title": "Sunset Yoga",
            "description": "Bring a mat.",
            "start_time": datetime(2099, 1, 15, 18, 30, tzinfo=NYC),
            "end_time": datetime(2099, 1, 15, 19, 45, tzinfo=NYC),
            "location": "Pier 1",
            "borough": Event.Borough.BROOKLYN,
            "categories": "Fitness",
            "registration_url": "https://example.org/tickets",
        }
        defaults.update(fields)
        return Event.objects.create(
            guid=Event.HOSTED_GUID_PREFIX + guid,
            host=host or self.host,
            content_hash="",
            **defaults,
        )

    def cancel(self, message="", user=None):
        self.client.force_login(user or self.host)
        return self.client.post(self.cancel_url, {"message": message}, follow=True)


class CancelEventTests(CancelTestCase):
    """The host confirms, the event is cancelled, and attendees are told."""

    def test_host_sees_cancel_button(self):
        self.client.force_login(self.host)
        self.assertContains(self.client.get(self.detail_url), self.cancel_url)

    def test_others_do_not_see_cancel_button(self):
        self.client.force_login(self.fan)
        self.assertNotContains(self.client.get(self.detail_url), self.cancel_url)

    def test_confirmation_page_counts_both_interest_kinds(self):
        self.client.force_login(self.host)
        response = self.client.get(self.cancel_url)
        self.assertContains(response, "Cancel this event?")
        self.assertContains(response, "This can't be undone.")
        self.assertEqual(response.context["recipient_count"], 2)
        self.event.refresh_from_db()
        self.assertEqual(self.event.status, Event.Status.UPCOMING)

    def test_cancel_without_message(self):
        response = self.cancel()

        self.assertRedirects(response, self.detail_url)
        self.assertContains(
            response, "Event cancelled. We let 2 interested people know."
        )
        self.event.refresh_from_db()
        self.assertEqual(self.event.status, Event.Status.CANCELLED)
        self.assertEqual(
            sorted(email.to[0] for email in mail.outbox),
            ["fan@example.com", "goer@example.com"],
        )
        email = mail.outbox[0]
        self.assertEqual(email.subject, "Cancelled: Sunset Yoga")
        self.assertIn("Park Pals cancelled Sunset Yoga.", email.body)
        self.assertIn(
            "It was planned for: Thursday, January 15, 2099 at 6:30 PM – 7:45 PM",
            email.body,
        )
        self.assertIn("Where: Pier 1", email.body)
        self.assertNotIn("Message from", email.body)
        self.assertIn("http://testserver" + self.detail_url, email.body)
        self.assertEqual(
            set(Notification.objects.values_list("recipient__username", flat=True)),
            {"fan", "goer"},
        )

    def test_cancel_with_message(self):
        self.cancel(message="Rain is forecast all day.")
        body = Notification.objects.first().body
        self.assertTrue(
            body.endswith("Message from Park Pals:\nRain is forecast all day.")
        )
        self.assertIn("Rain is forecast all day.", mail.outbox[0].body)

    def test_cancel_with_nobody_interested(self):
        EventInterest.objects.all().delete()
        response = self.cancel()
        self.assertEqual(
            [str(message) for message in response.context["messages"]],
            ["Event cancelled."],
        )
        self.assertEqual(mail.outbox, [])

    def test_message_too_long_is_rejected(self):
        self.client.force_login(self.host)
        response = self.client.post(self.cancel_url, {"message": "x" * 2001})
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.context["form"].errors)
        self.event.refresh_from_db()
        self.assertEqual(self.event.status, Event.Status.UPCOMING)
        self.assertEqual(mail.outbox, [])

    def test_only_host_can_cancel(self):
        self.client.force_login(self.bystander)
        self.assertEqual(self.client.get(self.cancel_url).status_code, 404)
        self.assertEqual(self.cancel(user=self.bystander).status_code, 404)
        self.event.refresh_from_db()
        self.assertEqual(self.event.status, Event.Status.UPCOMING)

    def test_feed_events_cannot_be_cancelled(self):
        feed = Event.objects.create(guid="feed-1", title="Parks Tour", content_hash="")
        self.client.force_login(self.host)
        response = self.client.get(reverse("event_cancel", args=[feed.pk]))
        self.assertEqual(response.status_code, 404)

    def test_ended_events_cannot_be_cancelled(self):
        past = timezone.now() - timedelta(days=2)
        self.event.start_time = past
        self.event.end_time = past + timedelta(hours=1)
        self.event.save()
        self.assertEqual(self.cancel().status_code, 404)

    def test_cancelling_twice_notifies_once(self):
        self.cancel()
        self.assertEqual(self.cancel().status_code, 404)
        self.assertEqual(len(mail.outbox), 2)

    def test_simultaneous_cancel_notifies_once(self):
        """A request that loaded the event before another cancelled it stops."""
        stale = Event.objects.get(pk=self.event.pk)
        Event.objects.filter(pk=self.event.pk).update(status=Event.Status.CANCELLED)
        with patch("events.views.get_hosted_upcoming_event", return_value=stale):
            self.assertEqual(self.cancel().status_code, 404)
        self.assertEqual(mail.outbox, [])
        self.assertFalse(Notification.objects.exists())

    def test_anonymous_redirected_to_login(self):
        response = self.client.post(self.cancel_url)
        self.assertEqual(response.status_code, 302)
        self.assertIn(reverse("login"), response["Location"])

    def test_other_methods_not_allowed(self):
        self.client.force_login(self.host)
        self.assertEqual(self.client.put(self.cancel_url).status_code, 405)


class CancelledEventTests(CancelTestCase):
    """A cancelled event is archived: hidden from listings and read-only."""

    def setUp(self):
        super().setUp()
        self.event.status = Event.Status.CANCELLED
        self.event.save()

    def test_hidden_from_home_search_and_filters(self):
        self.hosted("still-on", title="Sunrise Yoga")
        self.client.force_login(self.fan)
        search = reverse("search")
        for response in (
            self.client.get(reverse("landing")),
            self.client.get(search, {"q": "yoga"}),
            self.client.get(search, {"borough": "brooklyn"}),
        ):
            self.assertContains(response, "Sunrise Yoga")
            self.assertNotContains(response, "Sunset Yoga")

    def test_detail_page_shows_banner_and_hides_actions(self):
        self.client.force_login(self.host)
        response = self.client.get(self.detail_url)
        self.assertContains(response, "This event was cancelled")
        self.assertContains(response, "new comments are closed")
        for hidden in (
            reverse("event_edit", args=[self.event.pk]),
            self.cancel_url,
            reverse("event_notify", args=[self.event.pk]),
            reverse("toggle_event_interest", args=[self.event.pk]),
            reverse("create_event_post", args=[self.event.pk]),
            "https://example.org/tickets",
        ):
            self.assertNotContains(response, hidden)

    def test_host_cannot_edit_or_message(self):
        self.client.force_login(self.host)
        edit = self.client.get(reverse("event_edit", args=[self.event.pk]))
        self.assertEqual(edit.status_code, 404)
        notify = self.client.post(
            reverse("event_notify", args=[self.event.pk]),
            {"subject": "Hi", "message": "Hello"},
        )
        self.assertEqual(notify.status_code, 404)

    def test_interest_cannot_be_added(self):
        self.client.force_login(self.bystander)
        self.client.post(reverse("toggle_event_interest", args=[self.event.pk]))
        self.assertFalse(
            EventInterest.objects.filter(user=self.bystander, event=self.event).exists()
        )

    def test_model_flags(self):
        self.assertTrue(self.event.is_cancelled)
        self.assertFalse(self.event.is_open)
        self.assertFalse(self.event.has_ended)


class MyEventsTests(CancelTestCase):
    """Hosts see their upcoming events and an archive of the rest."""

    def test_requires_login(self):
        response = self.client.get(reverse("my_events"))
        self.assertEqual(response.status_code, 302)
        self.assertIn(reverse("login"), response["Location"])

    def test_splits_upcoming_and_archived(self):
        past = timezone.now() - timedelta(days=3)
        ended = self.hosted(
            "ended",
            title="Old Picnic",
            start_time=past,
            end_time=past + timedelta(hours=1),
        )
        cancelled = self.hosted(
            "cancelled",
            title="Rained Out",
            start_time=datetime(2099, 3, 1, 10, 0, tzinfo=NYC),
            end_time=None,
            status=Event.Status.CANCELLED,
        )
        self.hosted("other", host=self.fan, title="Someone Else's Event")
        self.client.force_login(self.host)
        response = self.client.get(reverse("my_events"))

        self.assertEqual(list(response.context["upcoming_events"]), [self.event])
        self.assertEqual(list(response.context["archived_events"]), [cancelled, ended])
        self.assertContains(response, reverse("event_edit", args=[self.event.pk]))
        self.assertContains(response, self.cancel_url)
        self.assertNotContains(response, reverse("event_edit", args=[ended.pk]))
        self.assertContains(response, "Cancelled")
        self.assertContains(response, "Ended")
        self.assertNotContains(response, "Someone Else")

    def test_empty_states(self):
        self.client.force_login(self.bystander)
        response = self.client.get(reverse("my_events"))
        self.assertContains(response, "You have no upcoming events.")
        self.assertContains(response, "Cancelled and past events will show up here.")

    def test_header_links_to_my_events(self):
        self.client.force_login(self.host)
        self.assertContains(self.client.get(reverse("landing")), reverse("my_events"))
        profile = reverse("public_profile", kwargs={"username": "host"})
        self.assertContains(self.client.get(profile), reverse("my_events"))


class CancelNoticeTests(CancelTestCase):
    """The wording of cancellation notices."""

    def test_without_date_or_location(self):
        event = self.hosted("tba", start_time=None, end_time=None, location="")
        subject, body = cancel_notice(event)
        self.assertEqual(subject, "Cancelled: Sunset Yoga")
        self.assertIn("It was planned for: To be announced", body)
        self.assertNotIn("Where:", body)

    def test_long_titles_fit_the_subject(self):
        notify_interested(self.event, "x" * 300, "Body", event_url="http://x")
        subject = Notification.objects.first().subject
        self.assertEqual(len(subject), 200)
        self.assertEqual(mail.outbox[0].subject, subject)
