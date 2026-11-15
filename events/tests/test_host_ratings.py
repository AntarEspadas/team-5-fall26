"""Tests for rating an event's host once the event is over (#128)."""

from datetime import datetime, timedelta
from unittest import mock

from django.contrib.admin.sites import site
from django.contrib.auth import get_user_model
from django.db import IntegrityError, transaction
from django.test import RequestFactory, TestCase
from django.urls import reverse
from django.utils import timezone

from accounts.models import UserBan
from events.filters import NYC_TIMEZONE
from events.forms import HostRatingForm
from events.models import Event, HostRating, Registration
from events.ratings import (
    MIN_RATINGS_FOR_AVERAGE,
    RATING_WINDOW,
    host_rating_summary,
    latest_host_comments,
    rating_block_reason,
    rating_closes_at,
    rating_window_open,
)
from reports.admin import target_summary
from reports.models import Report

User = get_user_model()


def make_event(host, guid, *, days_ago=1, **fields):
    """Create a hosted event that ended ``days_ago`` days ago.

    A negative ``days_ago`` makes an event that hasn't started yet.
    """
    end = timezone.now() - timedelta(days=days_ago)
    defaults = {
        "title": f"Event {guid}",
        "content_hash": "r" * 64,
        "host": host,
        "start_time": end - timedelta(hours=2),
        "end_time": end,
    }
    defaults.update(fields)
    return Event.objects.create(guid=f"host-{guid}", **defaults)


class RatingTestCase(TestCase):
    """Shared users and a finished event that ``maya`` registered for."""

    @classmethod
    def setUpTestData(cls):
        """Create a host, an attendee, a stranger, and a finished event."""
        cls.host = User.objects.create_user(
            username="alex",
            email="alex@example.com",
            password="pass12345",
            display_name="Alex Rivera",
            share_profile_with_attendees=True,
        )
        cls.attendee = User.objects.create_user(
            username="maya", email="maya@example.com", password="pass12345"
        )
        cls.stranger = User.objects.create_user(
            username="sam", email="sam@example.com", password="pass12345"
        )
        cls.event = make_event(cls.host, "yoga", title="Sunrise Yoga")
        Registration.objects.create(event=cls.event, user=cls.attendee)

    def rate(self, rater, event, stars, comment=""):
        """Store a rating directly, as if ``rater`` had submitted it."""
        Registration.objects.get_or_create(event=event, user=rater)
        return HostRating.objects.create(
            event=event, host=event.host, rater=rater, stars=stars, comment=comment
        )

    def ban(self, user):
        """Give ``user`` an active ban."""
        UserBan.objects.create(user=user, offense_number=1)


class RatingRuleTests(RatingTestCase):
    """Unit tests for who may rate and when."""

    def test_window_starts_at_end_time(self):
        """Ratings close 14 days after the event's end time."""
        self.assertEqual(
            rating_closes_at(self.event), self.event.end_time + RATING_WINDOW
        )

    def test_window_runs_from_effective_end_without_end_time(self):
        """Without an end time, the window runs from midnight after the start day.

        That's when ``has_ended`` first counts the event as over, so a
        morning event isn't short-changed by most of a day.
        """
        start = datetime(2026, 5, 3, 9, 0, tzinfo=NYC_TIMEZONE)
        event = make_event(self.host, "no-end", start_time=start, end_time=None)
        midnight_after = datetime(2026, 5, 4, 0, 0, tzinfo=NYC_TIMEZONE)
        self.assertEqual(event.ended_at, midnight_after)
        self.assertEqual(rating_closes_at(event), midnight_after + RATING_WINDOW)

    def test_morning_event_without_end_time_open_on_day_fourteen(self):
        """Ratings stay open late on day 14 for an event with only a start time."""
        start = datetime(2026, 5, 3, 9, 0, tzinfo=NYC_TIMEZONE)
        event = make_event(self.host, "morning", start_time=start, end_time=None)
        Registration.objects.create(event=event, user=self.attendee)
        late_day_14 = datetime(2026, 5, 17, 20, 0, tzinfo=NYC_TIMEZONE)
        with mock.patch("django.utils.timezone.now", return_value=late_day_14):
            self.assertTrue(rating_window_open(event))
            self.assertIsNone(rating_block_reason(event, self.attendee))
        day_after = datetime(2026, 5, 18, 0, 1, tzinfo=NYC_TIMEZONE)
        with mock.patch("django.utils.timezone.now", return_value=day_after):
            self.assertFalse(rating_window_open(event))

    def test_window_never_closes_without_any_time(self):
        """Events with no times at all have no closing date."""
        event = make_event(self.host, "no-time", start_time=None, end_time=None)
        self.assertIsNone(rating_closes_at(event))

    def test_window_open_only_between_end_and_close(self):
        """Upcoming, cancelled and long-past events can't be rated."""
        self.assertTrue(rating_window_open(self.event))
        upcoming = make_event(self.host, "soon", days_ago=-2)
        self.assertFalse(rating_window_open(upcoming))
        cancelled = make_event(self.host, "off", status=Event.Status.CANCELLED)
        self.assertFalse(rating_window_open(cancelled))
        old = make_event(self.host, "old", days_ago=15)
        self.assertFalse(rating_window_open(old))

    def test_window_open_when_event_ended_without_times(self):
        """A done event with no times stays open for rating."""
        event = make_event(
            self.host,
            "done",
            start_time=None,
            end_time=None,
            status=Event.Status.DONE,
        )
        self.assertTrue(rating_window_open(event))

    def test_registered_attendee_can_rate(self):
        """An attendee of a finished event gets no block reason."""
        self.assertIsNone(rating_block_reason(self.event, self.attendee))

    def test_block_reasons(self):
        """Each rule gives a message that says what to do next."""
        feed_event = make_event(None, "feed")
        cancelled = make_event(self.host, "cancel", status=Event.Status.CANCELLED)
        upcoming = make_event(self.host, "later", days_ago=-1)
        old = make_event(self.host, "past", days_ago=20)
        for event in (feed_event, cancelled, upcoming, old):
            Registration.objects.create(event=event, user=self.attendee)
        cases = [
            (feed_event, self.attendee, "Only events posted on this site"),
            (self.event, self.host, "You can't rate your own event."),
            (cancelled, self.attendee, "This event was cancelled"),
            (upcoming, self.attendee, "once the event has ended"),
            (self.event, self.stranger, "Only people who registered"),
            (old, self.attendee, "Ratings close 14 days after"),
        ]
        for event, user, expected in cases:
            with self.subTest(expected=expected):
                self.assertIn(expected, rating_block_reason(event, user))

    def test_banned_attendee_is_blocked(self):
        """Members with an active ban can't rate."""
        self.ban(self.attendee)
        self.assertIn("suspended", rating_block_reason(self.event, self.attendee))


class RatingSummaryTests(RatingTestCase):
    """Unit tests for the average shown on profiles and event pages."""

    def test_no_ratings(self):
        """A host with no ratings is a new host with no average."""
        summary = host_rating_summary(self.host)
        self.assertEqual(summary["count"], 0)
        self.assertIsNone(summary["average"])
        self.assertFalse(summary["show_average"])
        self.assertEqual(summary["stars_filled"], 0)

    def test_average_hidden_below_minimum(self):
        """Fewer than three ratings keep the average hidden."""
        self.rate(self.attendee, self.event, 1)
        summary = host_rating_summary(self.host)
        self.assertEqual(summary["count"], 1)
        self.assertFalse(summary["show_average"])

    def test_average_rounded_once_minimum_reached(self):
        """Three ratings show the average to one decimal place."""
        self.rate(self.attendee, self.event, 5)
        self.rate(self.stranger, self.event, 4)
        third = User.objects.create_user("lee", "lee@example.com")
        self.rate(third, self.event, 4)
        summary = host_rating_summary(self.host)
        self.assertEqual(MIN_RATINGS_FOR_AVERAGE, 3)
        self.assertTrue(summary["show_average"])
        self.assertEqual(summary["average"], 4.3)
        self.assertEqual(summary["stars_filled"], 4)

    def test_latest_comments_skip_blank_and_limit(self):
        """Only ratings with comments are listed, newest first, up to a limit."""
        self.rate(self.attendee, self.event, 5, "Lovely morning")
        self.rate(self.stranger, self.event, 3)
        comments = list(latest_host_comments(self.host))
        self.assertEqual([r.comment for r in comments], ["Lovely morning"])
        self.assertEqual(list(latest_host_comments(self.host, limit=0)), [])

    def test_str_and_one_rating_per_event(self):
        """Ratings describe themselves and are unique per rater and event."""
        rating = self.rate(self.attendee, self.event, 4)
        self.assertEqual(str(rating), "maya rated alex 4/5 for Sunrise Yoga")
        with self.assertRaises(IntegrityError), transaction.atomic():
            HostRating.objects.create(
                event=self.event, host=self.host, rater=self.attendee, stars=2
            )

    def test_database_rejects_out_of_range_stars(self):
        """The database itself only accepts 1 to 5 stars."""
        for stars in (0, 6):
            with self.subTest(stars=stars):
                with self.assertRaises(IntegrityError), transaction.atomic():
                    HostRating.objects.create(
                        event=self.event,
                        host=self.host,
                        rater=self.stranger,
                        stars=stars,
                    )


class HostRatingFormTests(TestCase):
    """Unit tests for the rating form."""

    def test_valid_rating_trims_comment(self):
        """Stars are stored as numbers and comments lose stray whitespace."""
        form = HostRatingForm({"stars": "4", "comment": "  Nice host  "})
        self.assertTrue(form.is_valid())
        self.assertEqual(form.cleaned_data["stars"], 4)
        self.assertEqual(form.cleaned_data["comment"], "Nice host")

    def test_missing_or_bad_stars(self):
        """Missing or out-of-range stars explain how to fix it."""
        for data in ({}, {"stars": "9"}):
            with self.subTest(data=data):
                form = HostRatingForm(data)
                self.assertFalse(form.is_valid())
                self.assertEqual(
                    form.errors["stars"],
                    ["Pick from 1 to 5 stars before saving your rating."],
                )

    def test_long_comment(self):
        """Comments over the limit ask the user to shorten them."""
        form = HostRatingForm({"stars": "3", "comment": "x" * 301})
        self.assertFalse(form.is_valid())
        self.assertIn("Shorten your comment", form.errors["comment"][0])


class RateHostViewTests(RatingTestCase):
    """The rate_host endpoint saves ratings and reports problems as toasts."""

    def setUp(self):
        """Sign the attendee in."""
        self.client.force_login(self.attendee)
        self.url = reverse("rate_host", args=[self.event.pk])

    def toasts(self, response):
        """Return the toast texts on a followed response."""
        return [str(m) for m in response.context["messages"]]

    def test_saves_new_rating(self):
        """A first rating is stored with the event's host."""
        response = self.client.post(
            self.url, {"stars": "5", "comment": "Great"}, follow=True
        )
        self.assertRedirects(
            response, reverse("event_detail", args=[self.event.pk]) + "#rate-host"
        )
        rating = HostRating.objects.get(event=self.event, rater=self.attendee)
        self.assertEqual(
            (rating.host, rating.stars, rating.comment), (self.host, 5, "Great")
        )
        self.assertEqual(self.toasts(response), ["Thanks, your rating was saved."])
        self.assertContains(response, "toast-success")

    def test_updates_existing_rating(self):
        """Submitting again changes the same rating."""
        self.rate(self.attendee, self.event, 2, "Meh")
        response = self.client.post(
            self.url, {"stars": "4", "comment": ""}, follow=True
        )
        rating = HostRating.objects.get(event=self.event, rater=self.attendee)
        self.assertEqual((rating.stars, rating.comment), (4, ""))
        self.assertEqual(HostRating.objects.count(), 1)
        self.assertEqual(self.toasts(response), ["Your rating was updated."])

    def test_returns_to_safe_next_url(self):
        """A local ``next`` URL is honoured; an outside one is ignored."""
        my_events = reverse("my_events")
        response = self.client.post(self.url, {"stars": "3", "next": my_events})
        self.assertRedirects(response, my_events)
        response = self.client.post(
            self.url, {"stars": "3", "next": "https://evil.example/"}
        )
        self.assertRedirects(
            response, reverse("event_detail", args=[self.event.pk]) + "#rate-host"
        )

    def test_invalid_form_shows_error_toast(self):
        """Missing stars show a red toast and save nothing."""
        response = self.client.post(self.url, {"comment": "hi"}, follow=True)
        self.assertFalse(HostRating.objects.exists())
        self.assertEqual(
            self.toasts(response),
            ["Pick from 1 to 5 stars before saving your rating."],
        )
        self.assertContains(response, "toast-error")

    def test_blocked_rating_shows_reason(self):
        """Visitors who can't rate get the reason as an error toast."""
        self.client.force_login(self.stranger)
        response = self.client.post(self.url, {"stars": "1"}, follow=True)
        self.assertFalse(HostRating.objects.exists())
        self.assertIn("Only people who registered", self.toasts(response)[0])

    def test_host_cannot_rate_self(self):
        """Hosts are told they can't rate their own event."""
        self.client.force_login(self.host)
        response = self.client.post(self.url, {"stars": "5"}, follow=True)
        self.assertEqual(self.toasts(response), ["You can't rate your own event."])

    def test_requires_post_and_login(self):
        """GET isn't allowed and visitors are sent to log in."""
        self.assertEqual(self.client.get(self.url).status_code, 405)
        self.client.logout()
        response = self.client.post(self.url, {"stars": "5"})
        self.assertEqual(response.status_code, 302)
        self.assertIn(reverse("login"), response["Location"])

    def test_banned_member_is_stopped(self):
        """The ban middleware blocks rating submissions."""
        self.ban(self.attendee)
        response = self.client.post(self.url, {"stars": "5"})
        self.assertEqual(response.status_code, 403)
        self.assertFalse(HostRating.objects.exists())

    def test_unknown_event(self):
        """Rating an event that doesn't exist is a 404."""
        response = self.client.post(reverse("rate_host", args=[999999]), {"stars": 3})
        self.assertEqual(response.status_code, 404)


class EventPageRatingTests(RatingTestCase):
    """The event page shows the host's rating and the attendee's rating card."""

    def detail(self, event=None):
        """Fetch the detail page for ``event`` (default: the finished event)."""
        event = event or self.event
        return self.client.get(reverse("event_detail", args=[event.pk]))

    def test_attendee_sees_rating_card_in_panel(self):
        """After the event, attendees get the card inside the action panel."""
        self.client.force_login(self.attendee)
        response = self.detail()
        html = response.content.decode()
        panel = html[html.index('<aside class="detail-panel"') :]
        self.assertIn('id="rate-host"', panel)
        self.assertIn("How was it?", panel)
        self.assertIn(reverse("rate_host", args=[self.event.pk]), panel)
        self.assertIn("Save rating", panel)
        self.assertIn("You can change it until", panel)

    def test_existing_rating_is_prefilled(self):
        """A saved rating is checked and its comment filled in."""
        self.rate(self.attendee, self.event, 3, "Fine")
        self.client.force_login(self.attendee)
        response = self.detail()
        self.assertContains(response, 'value="3" required checked')
        self.assertContains(response, ">Fine</textarea>")
        self.assertContains(response, "Update rating")

    def test_closed_window_shows_read_only_rating(self):
        """After 14 days, attendees see their rating but can't change it."""
        old = make_event(self.host, "old-card", days_ago=20)
        self.rate(self.attendee, old, 4, "Was good")
        self.client.force_login(self.attendee)
        response = self.detail(old)
        self.assertContains(response, 'aria-label="4 out of 5 stars"')
        self.assertContains(response, "Was good")
        self.assertContains(response, "Ratings close 14 days after")
        self.assertNotContains(response, "Save rating")

    def test_no_card_for_others(self):
        """Hosts, strangers, visitors and upcoming events get no card."""
        upcoming = make_event(self.host, "upcoming-card", days_ago=-3)
        Registration.objects.create(event=upcoming, user=self.attendee)
        cases = [
            (self.host, self.event),
            (self.stranger, self.event),
            (None, self.event),
            (self.attendee, upcoming),
        ]
        for user, event in cases:
            with self.subTest(user=user, event=event.title):
                self.client.logout()
                if user:
                    self.client.force_login(user)
                self.assertNotContains(self.detail(event), 'id="rate-host"')

    def test_host_line_shows_new_host_then_average(self):
        """The Hosted by line shows "New host" until three ratings exist."""
        self.client.force_login(self.stranger)
        self.assertContains(self.detail(), "New host")
        for name in ("a", "b", "c"):
            rater = User.objects.create_user(name, f"{name}@example.com")
            self.rate(rater, self.event, 5)
        response = self.detail()
        self.assertContains(response, "Rated 5.0 out of 5 from 3 ratings")
        self.assertContains(
            response,
            f'href="{reverse("public_profile", args=["alex"])}#host-ratings"',
        )

    def test_feed_events_have_no_host_rating(self):
        """Events from the NYC Parks feed show no rating badge."""
        feed = make_event(None, "feed-page")
        self.client.force_login(self.attendee)
        response = self.detail(feed)
        self.assertNotContains(response, "host-rating-badge")


class MyEventsRatingTests(RatingTestCase):
    """My events offers a Rate host link on past events."""

    def setUp(self):
        """Sign the attendee in."""
        self.client.force_login(self.attendee)

    def test_rate_host_link_for_unrated_event(self):
        """Finished, unrated events link to the rating card."""
        response = self.client.get(reverse("my_events"))
        self.assertContains(
            response,
            f'href="{reverse("event_detail", args=[self.event.pk])}#rate-host"',
        )
        self.assertContains(response, "Rate host")

    def test_rated_event_shows_stars_and_change_link(self):
        """Rated events show the stars given and offer to change them."""
        self.rate(self.attendee, self.event, 4)
        response = self.client.get(reverse("my_events"))
        self.assertContains(response, "You rated ★ 4")
        self.assertContains(response, "Change rating")

    def test_no_link_when_rating_not_possible(self):
        """Upcoming, old, feed and banned cases don't offer rating."""
        Registration.objects.filter(user=self.attendee).delete()
        for guid, days, host in (("up", -2, self.host), ("old", 30, self.host)):
            event = make_event(host, f"mine-{guid}", days_ago=days)
            Registration.objects.create(event=event, user=self.attendee)
        feed = make_event(None, "mine-feed")
        Registration.objects.create(event=feed, user=self.attendee)
        self.assertNotContains(self.client.get(reverse("my_events")), "Rate host")
        Registration.objects.create(event=self.event, user=self.attendee)
        self.ban(self.attendee)
        self.assertNotContains(self.client.get(reverse("my_events")), "Rate host")


class EventCardRatingTests(RatingTestCase):
    """Ended event cards say Rate, not Ended, to attendees who can rate."""

    def setUp(self):
        """Archive the event so it shows on the archived events page."""
        Event.objects.filter(pk=self.event.pk).update(status=Event.Status.DONE)
        self.rate_link = (
            f'href="{reverse("event_detail", args=[self.event.pk])}#rate-host"'
        )

    def card_page(self, user):
        """Return the archived events page as ``user`` sees it."""
        self.client.force_login(user)
        return self.client.get(reverse("archived_events"))

    def test_attendee_sees_rate_button(self):
        """An attendee who hasn't rated gets a Rate button to the event page."""
        response = self.card_page(self.attendee)
        self.assertContains(response, self.rate_link)
        self.assertNotContains(response, "Ended")

    def test_others_still_see_ended(self):
        """Rated, non-attendee, host, and closed-window cards say Ended."""
        for user in (self.stranger, self.host):
            with self.subTest(user=user.username):
                response = self.card_page(user)
                self.assertNotContains(response, self.rate_link)
                self.assertContains(response, "Ended")
        self.rate(self.attendee, self.event, 5)
        self.assertNotContains(self.card_page(self.attendee), self.rate_link)
        HostRating.objects.all().delete()
        Event.objects.filter(pk=self.event.pk).update(
            end_time=timezone.now() - RATING_WINDOW - timedelta(days=1)
        )
        self.assertNotContains(self.card_page(self.attendee), self.rate_link)

    def test_feed_events_have_no_rate_button(self):
        """Events without a host on this site can't be rated."""
        self.assertFalse(make_event(None, "card-feed").rating_window_open)
        self.assertTrue(self.event.rating_window_open)


class ProfileRatingTests(RatingTestCase):
    """Public profiles show a host's rating and the latest comments."""

    def setUp(self):
        """Sign the stranger in to view the host's profile."""
        self.client.force_login(self.stranger)
        self.url = reverse("public_profile", args=["alex"])

    def test_new_host(self):
        """Below three ratings the average says "New host", but comments show.

        Only the average waits for three raters; the comment is shown
        without its stars so it can't stand in for the hidden average.
        """
        self.rate(self.attendee, self.event, 1, "Too early to tell")
        response = self.client.get(self.url)
        self.assertContains(response, 'id="host-ratings"')
        self.assertContains(response, "New host.")
        self.assertContains(response, "Too early to tell")
        html = response.content.decode()
        section = html[html.index('id="host-ratings"') :]
        self.assertNotIn("out of 5 stars", section)

    def test_party_registration_counts_as_one_rating(self):
        """A registration for a group still gives the host one rating and comment."""
        Registration.objects.filter(event=self.event, user=self.attendee).update(
            party_size=4
        )
        self.rate(self.attendee, self.event, 5, "Great for the whole family")
        response = self.client.get(self.url)
        self.assertContains(response, "1 rating<")
        self.assertContains(response, "Great for the whole family")

    def test_average_and_comments_without_names(self):
        """Three ratings show the average and comments, but not who wrote them."""
        self.rate(self.attendee, self.event, 5, "Lovely morning")
        self.rate(self.stranger, self.event, 4, "Well run")
        third = User.objects.create_user("lee", "lee@example.com")
        self.rate(third, self.event, 4)
        response = self.client.get(self.url)
        self.assertContains(response, "Rated 4.3 out of 5 from 3 ratings")
        self.assertContains(response, "Lovely morning")
        self.assertContains(response, "Sunrise Yoga")
        html = response.content.decode()
        section = html[html.index('id="host-ratings"') :]
        self.assertNotIn("maya", section)
        maya_rating = HostRating.objects.get(rater=self.attendee)
        own_rating = HostRating.objects.get(rater=self.stranger)
        self.assertIn(reverse("host_rating_report", args=[maya_rating.pk]), section)
        self.assertNotIn(reverse("host_rating_report", args=[own_rating.pk]), section)

    def test_non_hosts_have_no_rating_section(self):
        """Members who never hosted don't get a rating section."""
        response = self.client.get(reverse("public_profile", args=["sam"]))
        self.assertNotContains(response, 'id="host-ratings"')


class RatingReportTests(RatingTestCase):
    """Rating comments can be reported to the moderation queue."""

    @classmethod
    def setUpTestData(cls):
        """Add a rating with a comment to report."""
        super().setUpTestData()
        cls.rating = HostRating.objects.create(
            event=cls.event,
            host=cls.host,
            rater=cls.attendee,
            stars=1,
            comment="Rude comment",
        )

    def test_report_is_recorded_against_the_rater(self):
        """Reporting stores the rating, event and author, then thanks the user."""
        self.client.force_login(self.stranger)
        url = reverse("host_rating_report", args=[self.rating.pk])
        self.assertContains(self.client.get(url), "Report rating")
        response = self.client.post(
            url, {"reason": Report.Reason.HARASSMENT_HATE, "details": ""}, follow=True
        )
        self.assertRedirects(response, reverse("public_profile", args=["alex"]))
        report = Report.objects.get()
        self.assertEqual(report.kind, Report.Kind.RATING)
        self.assertEqual(report.reported_rating, self.rating)
        self.assertEqual(report.event, self.event)
        self.assertEqual(report.reported_user, self.attendee)
        self.assertEqual(report.event_title, "Sunrise Yoga")
        self.assertContains(response, "Report sent.")
        again = self.client.get(url, follow=True)
        self.assertContains(again, "You have already reported this rating.")

    def test_cannot_report_own_or_blank_rating(self):
        """Raters can't report themselves; comment-free ratings can't be reported."""
        self.client.force_login(self.attendee)
        url = reverse("host_rating_report", args=[self.rating.pk])
        self.assertEqual(self.client.get(url).status_code, 404)
        blank = HostRating.objects.create(
            event=self.event, host=self.host, rater=self.stranger, stars=3
        )
        url = reverse("host_rating_report", args=[blank.pk])
        self.assertEqual(self.client.get(url).status_code, 404)

    def test_moderation_queue_describes_rating(self):
        """The queue summarises the rating and links to it on the profile."""
        admin = User.objects.create_superuser("mod", "mod@example.com", "pass")
        report = Report.objects.create(
            kind=Report.Kind.RATING,
            reporter=self.stranger,
            reported_rating=self.rating,
            event=self.event,
            reported_user=self.attendee,
            reason=Report.Reason.OTHER,
        )
        request = RequestFactory().get("/")
        request.user = admin
        summary = target_summary(report, request, site)
        self.assertEqual(
            summary["summary"], "1/5 for Alex Rivera on Sunrise Yoga: Rude comment"
        )
        self.assertEqual(
            summary["public_url"],
            reverse("public_profile", args=["alex"]) + f"#rating-{self.rating.pk}",
        )
        self.assertIn("hostrating", summary["admin_url"])
        self.client.force_login(admin)
        response = self.client.get(reverse("admin:reports_report_changelist"))
        self.assertContains(response, "Rude comment")


class RatingIntegrationTests(TestCase):
    """End to end: register, wait for the event to end, rate, see the average."""

    def test_full_rating_flow(self):
        """Three attendees rate a host and the average appears everywhere."""
        host = User.objects.create_user(
            "host", "host@example.com", share_profile_with_attendees=True
        )
        event = make_event(host, "flow", days_ago=-1, title="Kite Day")
        attendees = [
            User.objects.create_user(f"guest{n}", f"guest{n}@example.com")
            for n in range(3)
        ]
        for guest in attendees:
            self.client.force_login(guest)
            self.client.post(reverse("event_register", args=[event.pk]))
        self.assertEqual(Registration.objects.filter(event=event).count(), 3)

        # Before the event ends, rating is refused.
        response = self.client.post(
            reverse("rate_host", args=[event.pk]), {"stars": "5"}, follow=True
        )
        self.assertContains(response, "once the event has ended")

        # A day after it ends, everyone rates.
        later = event.end_time + timedelta(days=1)
        with mock.patch("django.utils.timezone.now", return_value=later):
            for guest, stars in zip(attendees, (5, 4, 3)):
                self.client.force_login(guest)
                response = self.client.post(
                    reverse("rate_host", args=[event.pk]),
                    {"stars": str(stars), "comment": f"From {guest.username}"},
                    follow=True,
                )
                self.assertContains(response, "Thanks, your rating was saved.")
            profile = self.client.get(reverse("public_profile", args=["host"]))
            detail = self.client.get(reverse("event_detail", args=[event.pk]))
        self.assertContains(profile, "Rated 4.0 out of 5 from 3 ratings")
        self.assertContains(profile, "From guest0")
        self.assertContains(detail, "Rated 4.0 out of 5 from 3 ratings")
