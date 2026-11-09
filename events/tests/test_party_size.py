"""Tests for registering with a party size and the host's headcount."""

from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from accounts.ban_services import apply_ban
from events.forms import PartySizeForm
from events.models import Event, Notification, Registration
from events.notifications import notify_registration_change

NYC = ZoneInfo("America/New_York")


class PartySizeTestCase(TestCase):
    """A host, two guests, and an upcoming hosted event."""

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
        cls.lucy = user("lucy", display_name="Lucy")
        cls.sam = user("sam")

    def setUp(self):
        self.event = self.hosted("yoga")
        self.register_url = reverse("event_register", args=[self.event.pk])
        self.cancel_url = reverse("event_cancel_registration", args=[self.event.pk])
        self.detail_url = reverse("event_detail", args=[self.event.pk])

    def hosted(self, guid, host=None, **fields):
        """Create an upcoming event posted by the host."""
        defaults = {
            "title": "Sunset Yoga",
            "start_time": datetime(2099, 1, 15, 18, 30, tzinfo=NYC),
            "end_time": datetime(2099, 1, 15, 19, 45, tzinfo=NYC),
            "location": "Pier 1",
        }
        defaults.update(fields)
        return Event.objects.create(
            guid=Event.HOSTED_GUID_PREFIX + guid,
            host=host or self.host,
            content_hash="",
            **defaults,
        )

    def register(self, party_size=None, user=None, url=None):
        self.client.force_login(user or self.lucy)
        data = {} if party_size is None else {"party_size": party_size}
        return self.client.post(url or self.register_url, data, follow=True)

    def messages_of(self, response):
        return [str(message) for message in response.context["messages"]]

    def host_notifications(self):
        return Notification.objects.filter(recipient=self.host).order_by("created_at")


class PartySizeFormTests(TestCase):
    def test_blank_means_just_the_user(self):
        form = PartySizeForm({})
        self.assertTrue(form.is_valid())
        self.assertEqual(form.cleaned_data["party_size"], 1)

    def test_accepts_one_to_ten(self):
        for size in (1, 5, 10):
            form = PartySizeForm({"party_size": str(size)})
            self.assertTrue(form.is_valid(), size)
            self.assertEqual(form.cleaned_data["party_size"], size)

    def test_rejects_out_of_range_and_non_numbers(self):
        for value in ("0", "-2", "11", "three", "2.5"):
            form = PartySizeForm({"party_size": value})
            self.assertFalse(form.is_valid(), value)
            self.assertEqual(
                form.errors["party_size"], ["Choose a party size from 1 to 10."]
            )


class RegisterWithPartySizeTests(PartySizeTestCase):
    def test_existing_registrations_default_to_one(self):
        registration = Registration.objects.create(event=self.event, user=self.sam)
        self.assertEqual(registration.party_size, 1)

    def test_registering_a_party_saves_the_size(self):
        response = self.register(3)

        registration = Registration.objects.get(event=self.event, user=self.lucy)
        self.assertEqual(registration.party_size, 3)
        self.assertIn("You're registered for a party of 3.", self.messages_of(response))

    def test_registering_alone_has_a_plain_message(self):
        response = self.register(1)

        self.assertEqual(
            Registration.objects.get(event=self.event, user=self.lucy).party_size, 1
        )
        self.assertIn("You're registered for this event.", self.messages_of(response))

    def test_registering_without_a_size_registers_one(self):
        self.register()
        self.assertEqual(
            Registration.objects.get(event=self.event, user=self.lucy).party_size, 1
        )

    def test_invalid_sizes_save_nothing(self):
        for value in ("0", "-1", "11", "lots"):
            response = self.register(value)
            self.assertIn(
                "Choose a party size from 1 to 10.", self.messages_of(response)
            )
        self.assertFalse(Registration.objects.exists())
        self.assertFalse(self.host_notifications().exists())

    def test_host_is_told_about_a_new_registration(self):
        self.register(3)

        notification = self.host_notifications().get()
        self.assertEqual(notification.event, self.event)
        self.assertEqual(notification.notification_type, Notification.Type.REGISTRATION)
        self.assertEqual(notification.subject, "New registration for Sunset Yoga")
        self.assertEqual(
            notification.body, "Lucy registered for Sunset Yoga with a party of 3."
        )

    def test_registering_alone_keeps_the_plain_host_message(self):
        self.register(1)

        self.assertEqual(
            self.host_notifications().get().body, "Lucy registered for Sunset Yoga."
        )

    def test_host_cannot_register_for_their_own_event(self):
        self.register(2, user=self.host)
        self.assertFalse(Registration.objects.exists())

    def test_cannot_register_for_ended_or_cancelled_events(self):
        ended = self.hosted(
            "ended",
            start_time=timezone.now() - timedelta(hours=3),
            end_time=timezone.now() - timedelta(hours=1),
        )
        cancelled = self.hosted("cancelled", status=Event.Status.CANCELLED)

        for event in (ended, cancelled):
            self.register(2, url=reverse("event_register", args=[event.pk]))

        self.assertFalse(Registration.objects.exists())
        self.assertFalse(self.host_notifications().exists())

    def test_feed_events_have_no_host_to_notify(self):
        feed_event = Event.objects.create(
            guid="parks-1",
            title="Bird Walk",
            start_time=datetime(2099, 2, 1, 9, tzinfo=NYC),
            content_hash="abc",
        )

        self.register(4, url=reverse("event_register", args=[feed_event.pk]))

        self.assertEqual(
            Registration.objects.get(event=feed_event, user=self.lucy).party_size, 4
        )
        self.assertFalse(Notification.objects.exists())


class ChangePartySizeTests(PartySizeTestCase):
    def setUp(self):
        super().setUp()
        self.registration = Registration.objects.create(
            event=self.event, user=self.lucy, party_size=3
        )

    def test_changing_the_size_updates_and_tells_the_host(self):
        response = self.register(4)

        self.registration.refresh_from_db()
        self.assertEqual(self.registration.party_size, 4)
        self.assertIn("Party size updated to 4.", self.messages_of(response))
        notification = self.host_notifications().get()
        self.assertEqual(notification.notification_type, Notification.Type.REGISTRATION)
        self.assertEqual(notification.subject, "Party size changed for Sunset Yoga")
        self.assertEqual(
            notification.body,
            "Lucy changed their party for Sunset Yoga from 3 to 4.",
        )

    def test_saving_the_same_size_changes_nothing(self):
        response = self.register(3)

        self.registration.refresh_from_db()
        self.assertEqual(self.registration.party_size, 3)
        self.assertIn("Your party size is already 3.", self.messages_of(response))
        self.assertFalse(self.host_notifications().exists())

    def test_one_click_register_does_not_reset_the_size(self):
        response = self.register()

        self.registration.refresh_from_db()
        self.assertEqual(self.registration.party_size, 3)
        self.assertIn(
            "You are already registered for this event.", self.messages_of(response)
        )

    def test_cannot_change_size_after_the_event_is_cancelled(self):
        Event.objects.filter(pk=self.event.pk).update(status=Event.Status.CANCELLED)

        self.register(5)

        self.registration.refresh_from_db()
        self.assertEqual(self.registration.party_size, 3)

    def test_cancelling_tells_the_host(self):
        self.client.force_login(self.lucy)
        response = self.client.post(self.cancel_url, follow=True)

        self.assertFalse(Registration.objects.exists())
        self.assertIn(
            "Your registration has been cancelled.", self.messages_of(response)
        )
        notification = self.host_notifications().get()
        self.assertEqual(notification.subject, "Registration cancelled for Sunset Yoga")
        self.assertEqual(
            notification.body,
            "Lucy unregistered from Sunset Yoga (a party of 3).",
        )

    def test_cancelling_an_ended_event_does_not_notify(self):
        Event.objects.filter(pk=self.event.pk).update(
            start_time=timezone.now() - timedelta(hours=3),
            end_time=timezone.now() - timedelta(hours=1),
        )
        self.client.force_login(self.lucy)
        self.client.post(self.cancel_url)

        self.assertFalse(Registration.objects.exists())
        self.assertFalse(self.host_notifications().exists())

    def test_cancelling_when_not_registered_does_not_notify(self):
        self.client.force_login(self.sam)
        self.client.post(self.cancel_url)

        self.assertTrue(Registration.objects.exists())
        self.assertFalse(self.host_notifications().exists())


class PartySizePagesTests(PartySizeTestCase):
    def test_detail_shows_a_party_size_picker(self):
        self.client.force_login(self.lucy)
        response = self.client.get(self.detail_url)

        # The picker stays tucked away until the user clicks Register.
        self.assertContains(response, '<details class="register-toggle" id="register">')
        self.assertContains(
            response, '<summary class="detail-button">Register for Event</summary>'
        )
        self.assertContains(response, "Confirm registration")
        self.assertContains(response, '<select id="party-size" name="party_size">')
        self.assertContains(response, '<option value="1" selected>1</option>')
        self.assertContains(response, '<option value="10">10</option>')
        self.assertNotContains(response, '<option value="11">')

    def test_registered_user_sees_their_size_and_can_change_it(self):
        Registration.objects.create(event=self.event, user=self.lucy, party_size=4)
        self.client.force_login(self.lucy)
        response = self.client.get(self.detail_url)

        self.assertContains(response, "with a party of 4")
        self.assertContains(response, '<option value="4" selected>4</option>')
        self.assertContains(response, "Update party size")
        self.assertContains(response, "Cancel Registration")

    def test_banned_user_cannot_change_their_party_size(self):
        registration = Registration.objects.create(
            event=self.event, user=self.lucy, party_size=4
        )
        admin = get_user_model().objects.create_superuser("admin", "a@example.com")
        apply_ban(admin, self.lucy)
        self.client.force_login(self.lucy)

        response = self.client.get(self.detail_url)
        self.assertContains(response, "with a party of 4")
        self.assertNotContains(response, "Update party size")

        response = self.client.post(self.register_url, {"party_size": 2})
        self.assertEqual(response.status_code, 403)
        registration.refresh_from_db()
        self.assertEqual(registration.party_size, 4)

    def test_host_sees_headcount_and_party_sizes(self):
        Registration.objects.create(event=self.event, user=self.lucy, party_size=4)
        Registration.objects.create(event=self.event, user=self.sam, party_size=1)
        self.client.force_login(self.host)
        response = self.client.get(self.detail_url)

        self.assertContains(response, "Your headcount")
        self.assertContains(response, "<strong>5</strong>")
        self.assertContains(response, "people coming in 2 registrations.")
        self.assertContains(response, reverse("event_manage", args=[self.event.pk]))

    def test_manage_page_lists_each_party_and_the_headcount(self):
        Registration.objects.create(event=self.event, user=self.lucy, party_size=4)
        Registration.objects.create(event=self.event, user=self.sam, party_size=1)
        self.client.force_login(self.host)
        response = self.client.get(reverse("event_manage", args=[self.event.pk]))

        self.assertEqual(response.context["headcount"], 5)
        self.assertContains(response, "<strong>5</strong>")
        self.assertContains(response, "people coming in 2 registrations.")
        self.assertContains(response, "Party of 4")
        self.assertContains(response, "Party of 1")

    def test_host_with_no_registrations_sees_zero(self):
        self.client.force_login(self.host)
        response = self.client.get(self.detail_url)

        self.assertEqual(response.context["headcount"], 0)
        self.assertContains(response, "<strong>0</strong>")
        response = self.client.get(reverse("event_manage", args=[self.event.pk]))
        self.assertContains(response, "<strong>0</strong>")
        self.assertContains(response, "people coming in 0 registrations.")

    def test_others_do_not_see_party_sizes(self):
        Registration.objects.create(event=self.event, user=self.lucy, party_size=4)
        for user in (self.sam, self.lucy):
            self.client.force_login(user)
            response = self.client.get(self.detail_url)
            self.assertNotContains(response, "Your headcount")
            self.assertNotIn("headcount", response.context)

        self.client.logout()
        response = self.client.get(self.detail_url)
        self.assertNotContains(response, "Your headcount")

    def test_public_count_is_registrations_not_people(self):
        Registration.objects.create(event=self.event, user=self.lucy, party_size=4)
        self.client.force_login(self.sam)
        response = self.client.get(self.detail_url)

        self.assertContains(response, "Registered Attendees (1)")

    def test_card_register_links_to_the_event_page(self):
        self.client.force_login(self.lucy)
        response = self.client.get(reverse("landing"))

        self.assertContains(
            response, f'<a class="register" href="{self.detail_url}#register">'
        )
        self.assertNotContains(
            response,
            f'action="{self.register_url}"',
        )

    def test_my_events_shows_party_size(self):
        Registration.objects.create(event=self.event, user=self.lucy, party_size=3)
        self.client.force_login(self.lucy)
        response = self.client.get(reverse("my_events"))

        self.assertContains(response, "Party of 3")


class HostNotificationHelperTests(PartySizeTestCase):
    def test_long_titles_are_truncated_in_the_subject(self):
        long_event = self.hosted("long", title="Y" * 250)

        notifications = [
            notify_registration_change(long_event, self.lucy, True, 2),
            notify_registration_change(long_event, self.lucy, True, 3, 2),
            notify_registration_change(long_event, self.lucy, False, 3),
        ]

        for notification in notifications:
            self.assertLessEqual(len(notification.subject), 200)

    def test_feed_events_without_host_return_none(self):
        feed_event = Event.objects.create(
            guid="parks-2", title="Bird Walk", content_hash="abc"
        )

        self.assertIsNone(notify_registration_change(feed_event, self.lucy, False, 3))
