"""Fetch NYC Parks events from the open data feed and sync them into the DB.

The feed (https://data.cityofnewyork.us/api/v3/views/w3wp-dpdi/query.json)
publishes a rolling two-week window of events in a single JSON array. Each
sync upserts every record by its unique ``guid`` and only writes rows whose
content actually changed, detected with a hash of the imported fields; the
dataset is rewritten wholesale on every refresh, so the feed's own version
stamps would flag every row as changed even when nothing moved.
"""

import hashlib
import html
import json
import logging
from datetime import datetime
from urllib.error import URLError
from urllib.request import Request, urlopen
from zoneinfo import ZoneInfo

from django.db import transaction
from django.db.models import Q
from django.utils import timezone
from django.utils.html import strip_tags

from .models import Event

logger = logging.getLogger(__name__)

FEED_URL = "https://data.cityofnewyork.us/api/v3/views/w3wp-dpdi/query.json"
REQUEST_TIMEOUT_SECONDS = 30
NYC_TIMEZONE = ZoneInfo("America/New_York")

# Imported fields compared between syncs to detect real changes.
HASHED_FIELDS = (
    "title",
    "description",
    "start_time",
    "end_time",
    "location",
    "park_names",
    "park_ids",
    "categories",
    "latitude",
    "longitude",
    "event_url",
    "registration_url",
    "image_url",
    "instructor",
    "contact_phone",
)


class SyncError(Exception):
    """Raised when the feed cannot be fetched or parsed; the DB is untouched."""


def fetch_feed():
    """Download the events feed and return its list of records.

    Returns:
        list: raw records exactly as published by the feed.

    Raises:
        SyncError: if the request fails or the payload is not a list of
            records, leaving the database untouched.
    """
    request = Request(
        FEED_URL,
        headers={
            "Accept": "application/json",
            "User-Agent": "team-5-events-sync/1.0",
        },
    )
    try:
        with urlopen(request, timeout=REQUEST_TIMEOUT_SECONDS) as response:
            payload = json.load(response)
    except (URLError, OSError, ValueError) as error:
        raise SyncError(f"could not fetch the events feed: {error}") from error
    if not isinstance(payload, list) or not all(
        isinstance(record, dict) for record in payload
    ):
        raise SyncError("the events feed did not contain a list of records")
    return payload


def clean_text(value):
    """Return plain text with HTML entities decoded and tags removed."""
    if not value:
        return ""
    text = strip_tags(html.unescape(str(value)))
    return " ".join(text.split())


def secure_url(value):
    """Return the URL served over https, because the feed only links via http."""
    url = str(value or "").strip()
    if url.startswith("http://"):
        return "https://" + url[len("http://") :]
    return url


def nested_url(record, key):
    """Return the https URL stored under ``record[key].url``, if present."""
    container = record.get(key) or {}
    return secure_url(container.get("url"))


def parse_datetime(value):
    """Parse a naive NYC-local ISO timestamp into an aware datetime, or None."""
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    return parsed.replace(tzinfo=NYC_TIMEZONE)


def parse_coordinates(value):
    """Parse the feed's "latitude, longitude" string into a (lat, lon) pair."""
    try:
        latitude, longitude = (float(part) for part in str(value).split(","))
    except (TypeError, ValueError):
        return None, None
    return latitude, longitude


def content_hash(data):
    """Fingerprint the imported fields so unchanged rows can be skipped."""
    material = json.dumps(
        {field: data[field] for field in HASHED_FIELDS},
        sort_keys=True,
        ensure_ascii=False,
        default=str,
    )
    return hashlib.sha256(material.encode("utf-8")).hexdigest()


def parse_record(record):
    """Convert one feed record into validated field values for :class:`Event`.

    Returns:
        dict: model field values including ``content_hash`` and
        ``source_version``.

    Raises:
        SyncError: if the record has no usable ``guid`` or ``title``.
    """
    guid = str(record.get("guid") or "").strip()
    title = clean_text(record.get("title"))
    if not guid or not title:
        raise SyncError(f"feed record is missing a guid or title: {guid!r}")
    latitude, longitude = parse_coordinates(record.get("coordinates"))
    data = {
        "guid": guid,
        "title": title[:300],
        "description": clean_text(record.get("description")),
        "start_time": parse_datetime(record.get("starttime")),
        "end_time": parse_datetime(record.get("endtime")),
        "location": clean_text(record.get("location"))[:300],
        "park_names": clean_text(record.get("parknames"))[:300],
        "park_ids": clean_text(record.get("parkids"))[:100],
        "categories": clean_text(record.get("categories"))[:500],
        "latitude": latitude,
        "longitude": longitude,
        "event_url": nested_url(record, "link")[:500],
        "registration_url": nested_url(record, "registration_url")[:500],
        "image_url": nested_url(record, "image")[:500],
        "instructor": clean_text(record.get("instructor"))[:100],
        "contact_phone": clean_text(record.get("contact_phone"))[:50],
        "source_version": str(record.get(":version") or "")[:64],
    }
    data["content_hash"] = content_hash(data)
    return data


def sync_events():
    """Fetch the feed and upsert events, then mark finished ones done.

    New records are created, records whose content changed are updated, and
    unchanged records are left alone. Records that vanished from the feed are
    kept because the feed only publishes a rolling window. Events posted by
    hosts never appear in the feed, so only this last step affects them:
    upcoming events whose end time has passed are marked done, and hosted
    events without an end time are done once the day they start has ended.

    Returns:
        dict: counts for ``fetched``, ``created``, ``updated``, ``unchanged``
        and ``marked_done``.

    Raises:
        SyncError: if the feed cannot be fetched or parsed; no rows change.
    """
    records = fetch_feed()
    parsed = [parse_record(record) for record in records]
    counts = {
        "fetched": len(parsed),
        "created": 0,
        "updated": 0,
        "unchanged": 0,
        "marked_done": 0,
    }
    now = timezone.now()
    with transaction.atomic():
        existing = {event.guid: event for event in Event.objects.all()}
        created = []
        for data in parsed:
            current = existing.get(data["guid"])
            if current is None:
                created.append(Event(**data, last_synced_at=now))
                continue
            if current.content_hash == data["content_hash"]:
                counts["unchanged"] += 1
                continue
            for field, value in data.items():
                setattr(current, field, value)
            current.last_synced_at = now
            current.save()
            counts["updated"] += 1
        if created:
            Event.objects.bulk_create(created, batch_size=100)
            counts["created"] = len(created)
        start_of_today = timezone.localtime(now, NYC_TIMEZONE).replace(
            hour=0, minute=0, second=0, microsecond=0
        )
        counts["marked_done"] = (
            Event.objects.filter(status=Event.Status.UPCOMING)
            .filter(
                Q(end_time__lt=now)
                | Q(
                    host__isnull=False,
                    end_time__isnull=True,
                    start_time__lt=start_of_today,
                )
            )
            .update(status=Event.Status.DONE)
        )
    logger.info("event sync finished: %s", counts)
    return counts
