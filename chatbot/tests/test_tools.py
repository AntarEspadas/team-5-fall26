"""Tests for the database tools the assistant calls."""

from django.contrib.auth.models import AnonymousUser
from django.core import mail
from django.test import TestCase, override_settings

from chatbot.models import Inquiry
from chatbot.tools import Toolbox, borough_label, event_status
from chatbot.tests.helpers import make_event, make_user
from events.models import (
    ContactRequest,
    Conversation,
    DirectMessage,
    Event,
    EventInterest,
    Notification,
)


class SearchEventsToolTests(TestCase):
    """Search reuses the site's full-text search and filters."""

    @classmethod
    def setUpTestData(cls):
        cls.yoga = make_event(
            "y1",
            "Sunrise Yoga",
            categories="Yoga & Pilates Classes",
            park_names="Prospect Park",
            park_ids="B073",
        )
        cls.concert = make_event(
            "c1", "Jazz Concert", days=5, categories="Concerts", borough="manhattan"
        )
        make_event("d1", "Old Yoga", status=Event.Status.DONE)
        cls.toolbox = Toolbox(AnonymousUser())

    def titles(self, result):
        return [event["title"] for event in result["events"]]

    def test_keyword_search(self):
        result = self.toolbox.call("search_events", {"query": "yoga"})
        self.assertEqual(self.titles(result), ["Sunrise Yoga"])
        self.assertEqual(result["events"][0]["page"], f"/events/{self.yoga.pk}/")
        self.assertEqual(result["events"][0]["borough"], "Brooklyn")

    def test_falls_back_to_any_word(self):
        result = self.toolbox.search_events(query="yoga skydiving")
        self.assertEqual(self.titles(result), ["Sunrise Yoga"])

    def test_filters_without_a_query_list_soonest_first(self):
        result = self.toolbox.search_events(boroughs=["manhattan", "atlantis"])
        self.assertEqual(self.titles(result), ["Jazz Concert"])
        result = self.toolbox.search_events(categories=["yoga"], boroughs="brooklyn")
        self.assertEqual(self.titles(result), ["Sunrise Yoga"])

    def test_dates_and_limits(self):
        result = self.toolbox.search_events(date_from="2000-01-01", limit=1)
        self.assertEqual(self.titles(result), ["Sunrise Yoga"])
        result = self.toolbox.search_events(date_from="not a date", limit="many")
        self.assertEqual(result["count"], 2)
        far = self.toolbox.search_events(date_from="2999-01-01", date_to="2999-01-02")
        self.assertEqual(far["count"], 0)


class EventDetailsToolTests(TestCase):
    """Details include public facts and counts, never attendee identities."""

    @classmethod
    def setUpTestData(cls):
        cls.host = make_user("host", display_name="Hana")
        cls.fan = make_user("fan")
        cls.event = make_event(
            "host-1",
            "Picnic",
            host=cls.host,
            borough="queens",
            description="Bring snacks.",
            registration_url="https://example.org/register",
        )
        EventInterest.objects.create(event=cls.event, user=cls.fan)
        EventInterest.objects.create(
            event=cls.event,
            user=make_user("other"),
            status=EventInterest.Status.WANT_TO_ATTEND,
        )

    def test_anonymous_details(self):
        details = Toolbox(AnonymousUser()).get_event_details(self.event.pk)
        self.assertEqual(details["host"], "Hana")
        self.assertEqual(details["source"], "posted by a user")
        self.assertEqual(details["interested_count"], 1)
        self.assertEqual(details["want_to_attend_count"], 1)
        self.assertEqual(details["registration_link"], "https://example.org/register")
        self.assertNotIn("my_attendance", details)
        self.assertNotIn("fan", str(details))

    def test_signed_in_details_include_own_attendance(self):
        details = Toolbox(self.fan).get_event_details(str(self.event.pk))
        self.assertEqual(details["my_attendance"], "Interested")
        self.assertFalse(details["i_am_the_host"])
        details = Toolbox(self.host).get_event_details(self.event.pk)
        self.assertEqual(details["my_attendance"], "none")
        self.assertTrue(details["i_am_the_host"])

    def test_unknown_events(self):
        toolbox = Toolbox(AnonymousUser())
        self.assertIn("error", toolbox.get_event_details(999999))
        self.assertIn("error", toolbox.get_event_details("abc"))
        self.assertIn("error", toolbox.get_event_details())


class MyActivityToolTests(TestCase):
    """Visitors only ever see their own activity."""

    def test_anonymous_visitors_are_asked_to_sign_in(self):
        result = Toolbox(AnonymousUser()).get_my_activity()
        self.assertEqual(result["sign_in_page"], "/accounts/login/")

    def test_signed_in_summary(self):
        user = make_user("me", display_name="Me")
        friend = make_user("friend")
        planned = make_event("p1", "Planned Walk")
        ended = make_event("p2", "Ended Walk", days=-3)
        for event in (planned, ended):
            EventInterest.objects.create(event=event, user=user)
        make_event("h1", "My Hosted Hike", host=user)
        make_event("h2", "My Cancelled Hike", host=user, status="cancelled")
        Notification.objects.create(
            recipient=user, event=planned, subject="S", body="B"
        )
        ContactRequest.objects.create(event=planned, sender=friend, recipient=user)
        conversation, _ = Conversation.get_or_create_for_users(user, friend)
        DirectMessage.objects.create(
            conversation=conversation, sender=friend, body="Hi"
        )
        DirectMessage.objects.create(conversation=conversation, sender=user, body="Yo")

        result = Toolbox(user).call("get_my_activity", None)

        self.assertEqual(result["name"], "Me")
        self.assertEqual(
            [e["title"] for e in result["planned_events"]], ["Planned Walk"]
        )
        self.assertEqual(result["planned_events"][0]["attendance"], "Interested")
        self.assertEqual(
            [e["title"] for e in result["hosted_upcoming_events"]], ["My Hosted Hike"]
        )
        self.assertEqual(result["hosted_events_total"], 2)
        self.assertEqual(result["unread_notifications"], 1)
        self.assertEqual(result["unread_messages"], 1)
        self.assertEqual(result["pending_contact_requests"], 1)
        self.assertEqual(result["settings"]["contact_permission"], "Require approval")


@override_settings(MANAGERS=[("Staff", "staff@example.com")])
class EscalateToolTests(TestCase):
    """Unanswerable questions are saved and their manager is told."""

    def test_question_about_a_hosted_event_notifies_its_host(self):
        host = make_user("host")
        asker = make_user("asker")
        event = make_event("host-2", "Kite Day", host=host)
        toolbox = Toolbox(asker)

        result = toolbox.call(
            "escalate_to_event_manager",
            {
                "question": "¿Hay estacionamiento?",
                "question_english": "Is there parking?",
                "language": "Spanish",
                "event_id": event.pk,
            },
        )

        self.assertEqual(result["status"], "forwarded")
        self.assertIn("notifications", result["visitor_will_be_told"])
        inquiry = Inquiry.objects.get()
        self.assertEqual((inquiry.user, inquiry.event), (asker, event))
        self.assertEqual(toolbox.inquiry_ids, [inquiry.pk])
        notification = Notification.objects.get(recipient=host)
        self.assertIn("Is there parking?", notification.body)
        self.assertIn("¿Hay estacionamiento?", notification.body)
        self.assertEqual(
            sorted(m.to[0] for m in mail.outbox),
            ["host@example.com", "staff@example.com"],
        )
        self.assertEqual(
            toolbox.escalate_to_event_manager(question="Again?"),
            {"status": "already_forwarded"},
        )

    def test_anonymous_question_without_an_event_goes_to_staff(self):
        toolbox = Toolbox(AnonymousUser())
        result = toolbox.escalate_to_event_manager(
            question="Are dogs allowed?", event_id="nonsense"
        )
        self.assertEqual(result["visitor_will_be_told"], "in this chat window")
        inquiry = Inquiry.objects.get()
        self.assertIsNone(inquiry.user)
        self.assertIsNone(inquiry.event)
        self.assertFalse(Notification.objects.exists())
        self.assertEqual([m.to for m in mail.outbox], [["staff@example.com"]])

    def test_signed_in_question_without_an_event(self):
        result = Toolbox(make_user("u")).escalate_to_event_manager(question="Hours?")
        self.assertEqual(
            result["visitor_will_be_told"], "in this chat window, by email"
        )

    def test_hosts_asking_about_their_own_event_are_not_notified(self):
        host = make_user("host")
        event = make_event("host-3", "Own Event", host=host)
        Toolbox(host).escalate_to_event_manager(question="Q?", event_id=event.pk)
        self.assertFalse(Notification.objects.exists())

    def test_empty_questions_are_rejected(self):
        self.assertIn("error", Toolbox(AnonymousUser()).escalate_to_event_manager())
        self.assertFalse(Inquiry.objects.exists())


class ToolDispatchTests(TestCase):
    """Bad tool calls from the model come back as errors, not exceptions."""

    def test_unknown_tool_and_bad_arguments(self):
        toolbox = Toolbox(AnonymousUser())
        self.assertIn("error", toolbox.call("drop_tables", {}))
        self.assertIn("error", toolbox.call("get_my_activity", {"x": 1}))

    def test_status_and_borough_helpers(self):
        event = Event(title="CANCELED: Swim", park_ids="")
        self.assertEqual(event_status(event), "cancelled")
        self.assertEqual(borough_label(event), "")
        event = Event(title="Swim", status=Event.Status.DONE, park_ids="x123")
        self.assertEqual(event_status(event), "ended")
        self.assertEqual(borough_label(event), "The Bronx")
