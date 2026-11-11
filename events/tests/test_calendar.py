"""Tests for adding events to Google Calendar, Apple Calendar, and .ics apps."""

from datetime import datetime, timezone as dt_timezone
from urllib.parse import parse_qs, urlsplit
from zoneinfo import ZoneInfo

from django.test import TestCase
from django.urls import reverse
from django.utils.html import escape

from events.calendar_export import (
    GOOGLE_DETAILS_LIMIT,
    can_add_to_calendar,
    escape_text,
    fold_line,
    google_calendar_url,
    has_calendar_file,
    icalendar_text,
    ics_filename,
    webcal_url,
)
from events.models import Event

NYC = ZoneInfo("America/New_York")
EVENT_URL = "https://parks.example.org/events/7/"


def make_event(guid="cal-1", title="Sunset Yoga", **kwargs):
    """Create an upcoming event on a winter morning in New York."""
    kwargs.setdefault("content_hash", "c" * 64)
    kwargs.setdefault("start_time", datetime(2099, 1, 15, 9, 0, tzinfo=NYC))
    kwargs.setdefault("end_time", datetime(2099, 1, 15, 10, 30, tzinfo=NYC))
    return Event.objects.create(guid=guid, title=title, **kwargs)


def google_params(event):
    """Return the query of the event's Google Calendar link as a dict."""
    query = urlsplit(google_calendar_url(event, EVENT_URL)).query
    return {key: values[0] for key, values in parse_qs(query).items()}


def ics_lines(event, **kwargs):
    """Return the event's iCalendar content lines with folding undone."""
    return icalendar_text(event, EVENT_URL, **kwargs).replace("\r\n ", "").split("\r\n")


class CalendarAvailabilityTests(TestCase):
    """Only open events with a start time can be added to a calendar."""

    def test_upcoming_event_with_a_start_time_can_be_added(self):
        self.assertTrue(can_add_to_calendar(make_event()))

    def test_event_without_a_start_time_cannot_be_added(self):
        self.assertFalse(can_add_to_calendar(make_event(start_time=None)))

    def test_ended_and_done_events_cannot_be_added(self):
        ended = make_event(
            "ended",
            start_time=datetime(2000, 1, 1, 9, 0, tzinfo=NYC),
            end_time=datetime(2000, 1, 1, 10, 0, tzinfo=NYC),
        )
        done = make_event("done", status=Event.Status.DONE)

        self.assertFalse(can_add_to_calendar(ended))
        self.assertFalse(can_add_to_calendar(done))

    def test_cancelled_events_cannot_be_added(self):
        """Hosts cancel by status; the feed prefixes the title instead."""
        by_host = make_event("host", status=Event.Status.CANCELLED)
        by_feed = make_event("feed", title="CANCELED: Sunset Yoga")

        self.assertFalse(can_add_to_calendar(by_host))
        self.assertFalse(can_add_to_calendar(by_feed))

    def test_file_stays_available_for_subscribed_apps(self):
        """Ended and cancelled events keep their file; only undated ones don't."""
        self.assertTrue(has_calendar_file(make_event("done", status="done")))
        self.assertTrue(has_calendar_file(make_event("off", status="cancelled")))
        self.assertFalse(has_calendar_file(make_event("tba", start_time=None)))

    def test_webcal_links_hand_the_file_to_the_calendar_app(self):
        self.assertEqual(
            webcal_url("http://localhost:8000/events/7/calendar.ics"),
            "webcal://localhost:8000/events/7/calendar.ics",
        )
        self.assertEqual(
            webcal_url("https://parks.example.org/events/7/calendar.ics"),
            "webcal://parks.example.org/events/7/calendar.ics",
        )


class GoogleCalendarLinkTests(TestCase):
    """The Google Calendar link opens a prefilled event."""

    def test_link_prefills_title_times_details_and_location(self):
        event = make_event(
            description="Bring a mat.",
            location="Prospect Park Lakeside",
            borough=Event.Borough.BROOKLYN,
        )

        url = urlsplit(google_calendar_url(event, EVENT_URL))
        params = google_params(event)

        self.assertEqual(
            f"{url.scheme}://{url.netloc}{url.path}",
            "https://calendar.google.com/calendar/render",
        )
        self.assertEqual(params["action"], "TEMPLATE")
        self.assertEqual(params["text"], "Sunset Yoga")
        # 9:00-10:30 AM EST is 14:00-15:30 UTC.
        self.assertEqual(params["dates"], "20990115T140000Z/20990115T153000Z")
        self.assertEqual(params["details"], f"Bring a mat.\n\nEvent page: {EVENT_URL}")
        self.assertEqual(params["location"], "Prospect Park Lakeside, Brooklyn")

    def test_summer_times_use_daylight_saving_time(self):
        event = make_event(
            start_time=datetime(2099, 7, 4, 18, 0, tzinfo=NYC),
            end_time=datetime(2099, 7, 4, 21, 0, tzinfo=NYC),
        )

        self.assertEqual(
            google_params(event)["dates"], "20990704T220000Z/20990705T010000Z"
        )

    def test_missing_or_invalid_end_time_lasts_one_hour(self):
        no_end = make_event("no-end", end_time=None)
        end_before_start = make_event(
            "backwards", end_time=datetime(2099, 1, 15, 8, 0, tzinfo=NYC)
        )

        for event in (no_end, end_before_start):
            with self.subTest(event=event.guid):
                self.assertEqual(
                    google_params(event)["dates"],
                    "20990115T140000Z/20990115T150000Z",
                )

    def test_location_falls_back_to_park_names_then_borough(self):
        park = make_event("park", park_names="Central Park")
        borough_only = make_event("borough", borough=Event.Borough.BRONX)
        nowhere = make_event("nowhere")

        self.assertEqual(google_params(park)["location"], "Central Park")
        self.assertEqual(google_params(borough_only)["location"], "The Bronx")
        self.assertNotIn("location", google_params(nowhere))

    def test_event_without_description_links_to_its_page(self):
        self.assertEqual(
            google_params(make_event())["details"], f"Event page: {EVENT_URL}"
        )

    def test_long_descriptions_are_shortened(self):
        event = make_event(description="x" * (GOOGLE_DETAILS_LIMIT + 500))

        details = google_params(event)["details"]

        self.assertEqual(
            details,
            "x" * (GOOGLE_DETAILS_LIMIT - 1) + f"…\n\nEvent page: {EVENT_URL}",
        )


class ICalendarFileTests(TestCase):
    """The .ics file is a valid RFC 5545 calendar with one event."""

    def test_file_has_one_event_with_utc_times(self):
        event = make_event(
            description="Bring a mat.",
            location="Prospect Park Lakeside",
            borough=Event.Borough.BROOKLYN,
        )
        now = datetime(2099, 1, 1, 12, 0, tzinfo=dt_timezone.utc)

        self.assertEqual(
            ics_lines(event, now=now),
            [
                "BEGIN:VCALENDAR",
                "VERSION:2.0",
                "PRODID:-//Team 5//NYC Parks Events//EN",
                "CALSCALE:GREGORIAN",
                "METHOD:PUBLISH",
                "X-WR-CALNAME:Sunset Yoga",
                "REFRESH-INTERVAL;VALUE=DURATION:PT12H",
                "X-PUBLISHED-TTL:PT12H",
                "BEGIN:VEVENT",
                "UID:cal-1@nyc-parks-events",
                "DTSTAMP:20990101T120000Z",
                "DTSTART:20990115T140000Z",
                "DTEND:20990115T153000Z",
                "SUMMARY:Sunset Yoga",
                "STATUS:CONFIRMED",
                f"DESCRIPTION:Bring a mat.\\n\\nEvent page: {EVENT_URL}",
                "LOCATION:Prospect Park Lakeside\\, Brooklyn",
                f"URL:{EVENT_URL}",
                "END:VEVENT",
                "END:VCALENDAR",
                "",
            ],
        )

    def test_cancelled_events_are_marked_for_subscribed_apps(self):
        """Host cancellations get a title prefix; feed titles already say so."""
        by_host = ics_lines(make_event("host", status=Event.Status.CANCELLED))
        by_feed = ics_lines(make_event("feed", title="CANCELLED: Sunset Yoga"))

        self.assertIn("SUMMARY:Cancelled: Sunset Yoga", by_host)
        self.assertIn("X-WR-CALNAME:Cancelled: Sunset Yoga", by_host)
        self.assertIn("STATUS:CANCELLED", by_host)
        self.assertIn("SUMMARY:CANCELLED: Sunset Yoga", by_feed)
        self.assertIn("STATUS:CANCELLED", by_feed)

    def test_every_line_ends_with_crlf(self):
        text = icalendar_text(make_event(description="One\nTwo"), EVENT_URL)

        self.assertTrue(text.endswith("\r\n"))
        self.assertNotIn("\n", text.replace("\r\n", ""))

    def test_file_marks_the_spot_when_coordinates_are_known(self):
        event = make_event(latitude=40.6602, longitude=-73.969)

        self.assertIn("GEO:40.660200;-73.969000", ics_lines(event))
        self.assertFalse(
            any(line.startswith("GEO:") for line in ics_lines(make_event("x")))
        )

    def test_event_without_location_has_no_location_line(self):
        lines = ics_lines(make_event())

        self.assertFalse(any(line.startswith("LOCATION:") for line in lines))

    def test_text_values_are_escaped(self):
        event = make_event(
            title="Yoga, Tea; Talk \\ Q&A",
            description="Line one\r\nLine two\nLine three",
        )

        lines = ics_lines(event)

        self.assertIn("SUMMARY:Yoga\\, Tea\\; Talk \\\\ Q&A", lines)
        self.assertIn(
            "DESCRIPTION:Line one\\nLine two\\nLine three"
            f"\\n\\nEvent page: {EVENT_URL}",
            lines,
        )

    def test_escape_text_handles_breaks_and_control_characters(self):
        self.assertEqual(
            escape_text("a\\b;c,d\r\ne\rf\ng\x00h\x1fi\tj"),
            "a\\\\b\\;c\\,d\\ne\\nf\\nghi\tj",
        )

    def test_long_lines_are_folded_without_splitting_characters(self):
        line = "DESCRIPTION:" + "Café 🌳 picnic, " * 20

        folded = fold_line(line)
        pieces = folded.split("\r\n")

        self.assertGreater(len(pieces), 1)
        for piece in pieces:
            self.assertLessEqual(len(piece.encode("utf-8")), 75)
        for piece in pieces[1:]:
            self.assertTrue(piece.startswith(" "))
        self.assertEqual(folded.replace("\r\n ", ""), line)

    def test_lines_up_to_75_octets_are_not_folded(self):
        self.assertEqual(fold_line("x" * 75), "x" * 75)
        self.assertEqual(fold_line("x" * 76), "x" * 75 + "\r\n x")

    def test_filename_comes_from_the_title(self):
        self.assertEqual(
            ics_filename(make_event(title="Sunset Yoga by the Lake!")),
            "sunset-yoga-by-the-lake.ics",
        )
        self.assertEqual(ics_filename(make_event("cjk", title="瑜伽")), "event.ics")
        # Long titles are cut to 60 characters.
        self.assertEqual(
            ics_filename(make_event("long", title="Picnic " * 20)),
            "picnic-" * 8 + "picn.ics",
        )


class AddToCalendarPageTests(TestCase):
    """Event pages offer the calendar menu, and serve the .ics file."""

    def setUp(self):
        self.event = make_event(
            description="Bring a mat.",
            location="Prospect Park Lakeside",
            borough=Event.Borough.BROOKLYN,
        )
        self.detail_url = reverse("event_detail", args=[self.event.pk])
        self.calendar_url = reverse("event_calendar", args=[self.event.pk])

    def test_detail_page_offers_google_apple_and_ics_options(self):
        """Anyone viewing an upcoming event can add it to their calendar."""
        response = self.client.get(self.detail_url)

        google_url = google_calendar_url(
            self.event, f"http://testserver{self.detail_url}"
        )
        webcal = f"webcal://testserver{self.calendar_url}"
        self.assertTrue(response.context["calendar_available"])
        self.assertEqual(response.context["google_calendar_url"], google_url)
        self.assertEqual(response.context["webcal_url"], webcal)
        self.assertContains(response, "Add to calendar")
        self.assertContains(response, f'href="{escape(google_url)}"')
        self.assertContains(response, "Google Calendar")
        # Apple Calendar and the iCalendar option open the calendar app.
        self.assertContains(response, f'<a href="{webcal}">', count=2)
        self.assertContains(response, "Apple Calendar")
        self.assertContains(response, "iCalendar (Outlook and others)")
        # Browsers without a calendar app can still download the file.
        self.assertContains(response, f'<a href="{self.calendar_url}" download>')

    def test_detail_page_hides_the_menu_when_the_event_cannot_be_added(self):
        events = {
            "no start time": make_event("tba", start_time=None),
            "done": make_event("done", status=Event.Status.DONE),
            "cancelled": make_event("cancelled", status=Event.Status.CANCELLED),
        }

        for reason, event in events.items():
            with self.subTest(reason):
                response = self.client.get(reverse("event_detail", args=[event.pk]))

                self.assertFalse(response.context["calendar_available"])
                self.assertNotIn("google_calendar_url", response.context)
                self.assertNotIn("webcal_url", response.context)
                self.assertNotContains(response, "Add to calendar")
                self.assertNotContains(
                    response, reverse("event_calendar", args=[event.pk])
                )

    def test_ics_file_opens_in_calendar_apps(self):
        response = self.client.get(self.calendar_url)

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response["Content-Type"], "text/calendar; charset=utf-8")
        self.assertEqual(
            response["Content-Disposition"], 'inline; filename="sunset-yoga.ics"'
        )
        body = response.content.decode()
        self.assertIn("SUMMARY:Sunset Yoga\r\n", body)
        self.assertIn("DTSTART:20990115T140000Z\r\n", body)
        self.assertIn(f"URL:http://testserver{self.detail_url}\r\n", body)

    def test_ics_file_stays_available_after_the_event_ends_or_is_cancelled(self):
        """Subscribed calendar apps keep refreshing the file."""
        done = make_event("done", status=Event.Status.DONE)
        cancelled = make_event("cancelled", status=Event.Status.CANCELLED)

        done_response = self.client.get(reverse("event_calendar", args=[done.pk]))
        cancelled_response = self.client.get(
            reverse("event_calendar", args=[cancelled.pk])
        )

        self.assertEqual(done_response.status_code, 200)
        self.assertIn("STATUS:CONFIRMED\r\n", done_response.content.decode())
        self.assertEqual(cancelled_response.status_code, 200)
        self.assertIn("STATUS:CANCELLED\r\n", cancelled_response.content.decode())

    def test_ics_file_is_not_found_without_a_date_or_event(self):
        undated = make_event("tba", start_time=None)

        for pk in (undated.pk, 999999999):
            with self.subTest(pk=pk):
                response = self.client.get(reverse("event_calendar", args=[pk]))

                self.assertEqual(response.status_code, 404)

    def test_ics_file_only_answers_safe_requests(self):
        self.assertEqual(self.client.post(self.calendar_url).status_code, 405)
        self.assertEqual(self.client.head(self.calendar_url).status_code, 200)
