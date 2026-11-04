"""Tests for fetching and syncing the NYC Parks events feed."""

import json
from datetime import datetime, timedelta
from io import BytesIO
from unittest.mock import patch
from urllib.error import URLError
from zoneinfo import ZoneInfo

from django.test import TestCase
from django.utils import timezone

from events.models import Event
from events.services import SyncError, parse_record, sync_events

NYC = ZoneInfo("America/New_York")


def feed_record(**overrides):
    """Return a typical feed record, with fields overridable per test."""
    record = {
        "guid": "1001",
        "title": "Morning Yoga",
        "description": "A calm&nbsp;class in the&nbsp;park.",
        "starttime": "2099-01-15T09:00:00.000",
        "endtime": "2099-01-15T10:00:00.000",
        "location": "Prospect Park",
        "parkids": "Q097",
        "parknames": "Prospect Park",
        "categories": "Fitness | Yoga",
        "coordinates": "40.6602, -73.9690",
        "link": {"url": "http://example.org/events/morning-yoga"},
        "image": {"url": "http://example.org/img/yoga.png"},
        "registration_url": {"url": "http://example.org/register"},
        "instructor": "Alex Kim",
        "contact_phone": "212-555-0100",
        ":version": "rv-1",
    }
    record.update(overrides)
    return record


def run_sync(records):
    """Run a sync against a mocked feed and return its counts."""
    body = BytesIO(json.dumps(records).encode("utf-8"))
    with patch("events.services.urlopen", return_value=body):
        return sync_events()


class FetchTests(TestCase):
    """The feed download either returns records or raises SyncError."""

    def test_network_failure_raises_sync_error(self):
        """An unreachable feed raises SyncError and leaves the DB empty."""
        with patch("events.services.urlopen", side_effect=URLError("boom")):
            with self.assertRaisesMessage(SyncError, "could not fetch"):
                sync_events()
        self.assertEqual(Event.objects.count(), 0)

    def test_payload_that_is_not_a_list_raises(self):
        """A JSON object instead of a list is rejected before any write."""
        body = BytesIO(b'{"error": "not a list"}')
        with patch("events.services.urlopen", return_value=body):
            with self.assertRaisesMessage(SyncError, "list of records"):
                sync_events()
        self.assertEqual(Event.objects.count(), 0)

    def test_payload_with_non_record_entries_raises(self):
        """A list containing non-objects is rejected before any write."""
        body = BytesIO(b"[1, 2, 3]")
        with patch("events.services.urlopen", return_value=body):
            with self.assertRaisesMessage(SyncError, "list of records"):
                sync_events()
        self.assertEqual(Event.objects.count(), 0)

    def test_invalid_json_raises_sync_error(self):
        """A body that is not JSON is reported as a fetch failure."""
        body = BytesIO(b"<html>gateway error</html>")
        with patch("events.services.urlopen", return_value=body):
            with self.assertRaisesMessage(SyncError, "could not fetch"):
                sync_events()


class ParseRecordTests(TestCase):
    """Individual feed records are normalized into model fields."""

    def test_html_is_decoded_and_stripped_to_plain_text(self):
        """Entities become real characters and tags are removed."""
        data = parse_record(
            feed_record(
                title="NYC Parks&#8217; Open Call",
                description="<b>Bold</b> &amp; beautiful &nbsp; days",
            )
        )
        self.assertEqual(data["title"], "NYC Parks’ Open Call")
        self.assertEqual(data["description"], "Bold & beautiful days")

    def test_http_urls_are_upgraded_to_https(self):
        """Image, event and registration links load over https."""
        data = parse_record(feed_record())
        self.assertEqual(data["image_url"], "https://example.org/img/yoga.png")
        self.assertEqual(data["event_url"], "https://example.org/events/morning-yoga")
        self.assertEqual(data["registration_url"], "https://example.org/register")

    def test_missing_optional_fields_become_empty_values(self):
        """Records without extras still parse into blank, safe defaults."""
        data = parse_record({"guid": "9", "title": "Bare Event", ":version": "rv-9"})
        self.assertEqual(data["description"], "")
        self.assertEqual(data["image_url"], "")
        self.assertEqual(data["event_url"], "")
        self.assertEqual(data["registration_url"], "")
        self.assertIsNone(data["start_time"])
        self.assertIsNone(data["end_time"])
        self.assertIsNone(data["latitude"])
        self.assertIsNone(data["longitude"])

    def test_malformed_values_are_tolerated(self):
        """Unparseable times and coordinates become None instead of crashing."""
        data = parse_record(
            feed_record(starttime="not-a-date", coordinates="over there")
        )
        self.assertIsNone(data["start_time"])
        self.assertIsNone(data["latitude"])
        self.assertIsNone(data["longitude"])

    def test_nyc_times_become_aware_datetimes(self):
        """Naive feed timestamps are interpreted in New York City time."""
        data = parse_record(feed_record())
        self.assertEqual(
            data["start_time"],
            datetime(2099, 1, 15, 9, 0, tzinfo=NYC),
        )
        self.assertEqual(data["start_time"].utcoffset(), timedelta(hours=-5))

    def test_record_without_guid_or_title_raises(self):
        """A record that cannot identify an event aborts the whole sync."""
        for broken in (feed_record(guid=""), feed_record(title=" ")):
            with self.subTest(record=broken):
                with self.assertRaisesMessage(SyncError, "missing a guid or title"):
                    parse_record(broken)


class SyncTests(TestCase):
    """Syncs upsert by guid, skip unchanged rows, and mark finished events."""

    def test_new_records_are_created(self):
        """Every feed record becomes an upcoming event with audit fields."""
        counts = run_sync([feed_record(), feed_record(guid="1002", title="Birdwalk")])

        self.assertEqual(
            counts,
            {
                "fetched": 2,
                "created": 2,
                "updated": 0,
                "unchanged": 0,
                "marked_done": 0,
            },
        )
        event = Event.objects.get(guid="1001")
        self.assertEqual(event.title, "Morning Yoga")
        self.assertEqual(event.status, Event.Status.UPCOMING)
        self.assertEqual(event.latitude, 40.6602)
        self.assertEqual(event.source_version, "rv-1")
        self.assertEqual(len(event.content_hash), 64)
        self.assertIsNotNone(event.first_seen_at)
        self.assertIsNotNone(event.last_synced_at)

    def test_unchanged_records_are_not_rewritten(self):
        """A second identical sync writes nothing for existing rows."""
        run_sync([feed_record()])
        event = Event.objects.get(guid="1001")
        event.last_synced_at = timezone.now() - timedelta(hours=2)
        event.save()
        stale_synced_at = event.last_synced_at

        counts = run_sync([feed_record()])

        self.assertEqual(counts["unchanged"], 1)
        self.assertEqual(counts["updated"], 0)
        self.assertEqual(counts["created"], 0)
        event.refresh_from_db()
        self.assertEqual(event.last_synced_at, stale_synced_at)

    def test_source_version_changes_alone_do_not_count_as_changes(self):
        """The feed rewrites every row's version stamp, so it is ignored."""
        run_sync([feed_record()])
        counts = run_sync([feed_record(**{":version": "rv-2"})])

        self.assertEqual(counts["unchanged"], 1)
        self.assertEqual(counts["updated"], 0)

    def test_changed_records_are_updated_in_place(self):
        """Edited feed content replaces the stored copy for the same guid."""
        run_sync([feed_record()])
        counts = run_sync(
            [
                feed_record(
                    title="Morning Yoga (Moved)",
                    endtime="2099-01-15T11:00:00.000",
                    **{":version": "rv-2"},
                )
            ]
        )

        self.assertEqual(counts["updated"], 1)
        self.assertEqual(counts["created"], 0)
        event = Event.objects.get(guid="1001")
        self.assertEqual(event.title, "Morning Yoga (Moved)")
        self.assertEqual(event.end_time, datetime(2099, 1, 15, 11, 0, tzinfo=NYC))

    def test_records_that_left_the_feed_are_kept(self):
        """Events stay in the DB after the rolling feed drops them."""
        Event.objects.create(
            guid="old-1",
            title="Last Week's Fair",
            content_hash="c" * 64,
            status=Event.Status.DONE,
        )

        counts = run_sync([feed_record()])

        self.assertEqual(counts["fetched"], 1)
        self.assertTrue(Event.objects.filter(guid="old-1").exists())

    def test_finished_events_are_marked_done(self):
        """Upcoming events whose end time has passed become done."""
        Event.objects.create(
            guid="past-1",
            title="Yesterday's Concert",
            content_hash="d" * 64,
            status=Event.Status.UPCOMING,
            start_time=timezone.now() - timedelta(days=1),
            end_time=timezone.now() - timedelta(hours=1),
        )

        counts = run_sync([feed_record()])

        self.assertEqual(counts["marked_done"], 1)
        self.assertEqual(Event.objects.get(guid="past-1").status, Event.Status.DONE)

    def test_events_without_end_time_are_not_marked_done(self):
        """An undated event never looks finished to the sync."""
        Event.objects.create(
            guid="open-1",
            title="Ongoing Exhibit",
            content_hash="e" * 64,
            status=Event.Status.UPCOMING,
            end_time=None,
        )

        counts = run_sync([feed_record()])

        self.assertEqual(counts["marked_done"], 0)
        self.assertEqual(Event.objects.get(guid="open-1").status, Event.Status.UPCOMING)

    def test_parse_failure_leaves_existing_rows_untouched(self):
        """One broken record aborts the sync before the DB is written."""
        Event.objects.create(guid="1001", title="Old Title", content_hash="f" * 64)

        body = BytesIO(json.dumps([feed_record(), {"title": "No guid"}]).encode())
        with patch("events.services.urlopen", return_value=body):
            with self.assertRaisesMessage(SyncError, "missing a guid or title"):
                sync_events()

        self.assertEqual(Event.objects.get(guid="1001").title, "Old Title")
        self.assertEqual(Event.objects.count(), 1)
