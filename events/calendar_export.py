"""Let people save an event to Google Calendar, Apple Calendar, or other apps.

Google Calendar opens a prefilled event from a link. Apple Calendar, Outlook,
and most other calendar apps read an iCalendar (.ics) file, the RFC 5545
format built here without extra dependencies. A ``webcal://`` link to that
file opens the calendar app directly and subscribes to it, so the app keeps
the event up to date; the file itself can also be downloaded and imported.
Both use UTC times, so the file needs no time zone definitions and each app
shows the event in its own zone.
"""

import re
from datetime import timedelta
from datetime import timezone as dt_timezone
from urllib.parse import urlencode, urlsplit

from django.utils import timezone
from django.utils.text import Truncator, slugify

GOOGLE_CALENDAR_URL = "https://calendar.google.com/calendar/render"
PRODUCT_ID = "-//Team 5//NYC Parks Events//EN"
UID_DOMAIN = "nyc-parks-events"

# How often subscribed calendar apps should check the file for changes.
REFRESH_INTERVAL = "PT12H"

# Calendar entries need an end, but hosted events don't have to set one.
DEFAULT_DURATION = timedelta(hours=1)

# Keeps Google links well under URL length limits. The event page link after
# the description leads to the full text.
GOOGLE_DETAILS_LIMIT = 1500

# RFC 5545 content lines should not be longer than 75 octets, excluding CRLF.
MAX_LINE_OCTETS = 75

UTC_FORMAT = "%Y%m%dT%H%M%SZ"

# iCalendar text may not contain control characters other than tab; line
# breaks are escaped separately.
CONTROL_CHARACTERS = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")


def can_add_to_calendar(event):
    """Return whether the event has a start time and is still open.

    Events without a date, and events that have ended or were cancelled,
    have nothing worth saving to a calendar.
    """
    return bool(event.start_time) and event.is_open and not event.is_cancelled


def has_calendar_file(event):
    """Return whether the event has a start time, which every entry needs.

    Unlike the menu, the file stays available after an event ends or is
    cancelled, because subscribed calendar apps keep refreshing it.
    """
    return bool(event.start_time)


def webcal_url(url):
    """Return the ``webcal://`` form of a file URL.

    Browsers hand webcal links to the default calendar app, such as Apple
    Calendar on iPhones and Macs or Outlook on Windows, which then
    subscribes to the file instead of downloading it.
    """
    return urlsplit(url)._replace(scheme="webcal").geturl()


def calendar_title(event):
    """Return the title, marked when the host cancelled the event.

    Feed events have no cancelled status; the feed puts it in the title.
    """
    if event.status == event.Status.CANCELLED:
        return f"Cancelled: {event.title}"
    return event.title


def calendar_times(event):
    """Return the calendar entry's start and end.

    Events without an end time, or with one that is not after the start,
    last ``DEFAULT_DURATION``.
    """
    start, end = event.start_time, event.end_time
    if not end or end <= start:
        end = start + DEFAULT_DURATION
    return start, end


def calendar_location(event):
    """Return the place shown on the event page, followed by its borough."""
    place = event.location or event.park_names
    if not event.borough:
        return place
    borough = event.get_borough_display()
    return f"{place}, {borough}" if place else borough


def calendar_details(event, event_url, limit=None):
    """Return the description, cut to ``limit`` characters, and the page link."""
    description = event.description.strip()
    if limit is not None:
        description = Truncator(description).chars(limit)
    link = f"Event page: {event_url}"
    return f"{description}\n\n{link}" if description else link


def format_utc(value):
    """Format an aware datetime as an iCalendar UTC time, e.g. 20990115T140000Z."""
    return value.astimezone(dt_timezone.utc).strftime(UTC_FORMAT)


def google_calendar_url(event, event_url):
    """Return a link that opens Google Calendar with the event filled in."""
    start, end = calendar_times(event)
    params = {
        "action": "TEMPLATE",
        "text": event.title,
        "dates": f"{format_utc(start)}/{format_utc(end)}",
        "details": calendar_details(event, event_url, GOOGLE_DETAILS_LIMIT),
    }
    location = calendar_location(event)
    if location:
        params["location"] = location
    return f"{GOOGLE_CALENDAR_URL}?{urlencode(params)}"


def escape_text(value):
    """Escape an iCalendar TEXT value (RFC 5545 section 3.3.11)."""
    value = CONTROL_CHARACTERS.sub("", value)
    value = value.replace("\\", "\\\\").replace(";", "\\;").replace(",", "\\,")
    return value.replace("\r\n", "\\n").replace("\r", "\\n").replace("\n", "\\n")


def fold_line(line):
    """Fold a content line into pieces of at most 75 octets (RFC 5545 3.1).

    Continuation pieces start with a space, which counts toward their
    length, and multi-byte UTF-8 characters are never split.
    """
    pieces, piece, size, limit = [], "", 0, MAX_LINE_OCTETS
    for character in line:
        octets = len(character.encode("utf-8"))
        if size + octets > limit:
            pieces.append(piece)
            piece, size, limit = "", 0, MAX_LINE_OCTETS - 1
        piece += character
        size += octets
    pieces.append(piece)
    return "\r\n ".join(pieces)


def icalendar_text(event, event_url, now=None):
    """Return an iCalendar file holding the event as a single VEVENT.

    The UID comes from the event's stable ``guid``, so calendar apps that
    match on it update a saved event when the file is imported again.
    Subscribed apps name the calendar after the event, refresh it every
    ``REFRESH_INTERVAL``, and see cancellations through ``STATUS``.
    """
    start, end = calendar_times(event)
    title = escape_text(calendar_title(event))
    lines = [
        "BEGIN:VCALENDAR",
        "VERSION:2.0",
        f"PRODID:{PRODUCT_ID}",
        "CALSCALE:GREGORIAN",
        "METHOD:PUBLISH",
        f"X-WR-CALNAME:{title}",
        f"REFRESH-INTERVAL;VALUE=DURATION:{REFRESH_INTERVAL}",
        f"X-PUBLISHED-TTL:{REFRESH_INTERVAL}",
        "BEGIN:VEVENT",
        f"UID:{escape_text(event.guid)}@{UID_DOMAIN}",
        f"DTSTAMP:{format_utc(now or timezone.now())}",
        f"DTSTART:{format_utc(start)}",
        f"DTEND:{format_utc(end)}",
        f"SUMMARY:{title}",
        f"STATUS:{'CANCELLED' if event.is_cancelled else 'CONFIRMED'}",
        f"DESCRIPTION:{escape_text(calendar_details(event, event_url))}",
    ]
    location = calendar_location(event)
    if location:
        lines.append(f"LOCATION:{escape_text(location)}")
    if event.latitude is not None and event.longitude is not None:
        lines.append(f"GEO:{event.latitude:.6f};{event.longitude:.6f}")
    lines += [f"URL:{event_url}", "END:VEVENT", "END:VCALENDAR"]
    return "".join(f"{fold_line(line)}\r\n" for line in lines)


def ics_filename(event):
    """Return a download name built from the title, e.g. "sunset-yoga.ics"."""
    slug = slugify(event.title)[:60].strip("-_") or "event"
    return f"{slug}.ics"
