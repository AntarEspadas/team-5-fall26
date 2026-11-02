"""Read-only database tools the assistant can call through Gemini.

Every tool returns plain JSON-safe data. Tools only expose information the
site already shows the current visitor: public event details, attendance
counts (never attendee identities), and the signed-in user's own activity.
"""

import logging
from datetime import date

from django.db.models import F, Q
from django.urls import reverse
from django.utils.text import Truncator

from events.filters import BOROUGHS, CATEGORY_GROUPS, filter_events, nyc_today
from events.models import DirectMessage, Event, EventInterest
from events.notifications import describe_when
from events.search import search_events

from .inquiries import create_inquiry

logger = logging.getLogger(__name__)

MAX_SEARCH_RESULTS = 10
DEFAULT_SEARCH_RESULTS = 6
DESCRIPTION_LIMIT = 1500
BOROUGH_LABELS = {slug: label for slug, label, _ in BOROUGHS}
BOROUGH_BY_PREFIX = {prefix: label for _, label, prefix in BOROUGHS}
CATEGORY_SLUGS = [slug for slug, _, _ in CATEGORY_GROUPS]

DECLARATIONS = [
    {
        "name": "search_events",
        "description": (
            "Search upcoming events on the site (NYC Parks events and events "
            "posted by users). Use it for any question about what is on, "
            "when, or where. Search words must be in English. Combine with "
            "filters; leave query empty to list by filters only."
        ),
        "parameters": {
            "type": "OBJECT",
            "properties": {
                "query": {
                    "type": "STRING",
                    "description": (
                        "English keywords, e.g. 'yoga', 'concert central park'."
                    ),
                },
                "boroughs": {
                    "type": "ARRAY",
                    "items": {"type": "STRING", "enum": list(BOROUGH_LABELS)},
                },
                "categories": {
                    "type": "ARRAY",
                    "items": {"type": "STRING", "enum": CATEGORY_SLUGS},
                },
                "date_from": {
                    "type": "STRING",
                    "description": "First day, YYYY-MM-DD, New York time.",
                },
                "date_to": {
                    "type": "STRING",
                    "description": "Last day, YYYY-MM-DD, New York time.",
                },
                "limit": {"type": "INTEGER", "description": "At most 10."},
            },
        },
    },
    {
        "name": "get_event_details",
        "description": (
            "Get everything the site knows about one event: description, "
            "times, place, host, contact, registration link, status, and how "
            "many people are interested."
        ),
        "parameters": {
            "type": "OBJECT",
            "properties": {"event_id": {"type": "INTEGER"}},
            "required": ["event_id"],
        },
    },
    {
        "name": "get_my_activity",
        "description": (
            "Get the signed-in visitor's own activity: events they are "
            "interested in or want to attend, events they host, unread "
            "notifications and messages, pending contact requests, and their "
            "privacy settings."
        ),
        "parameters": {"type": "OBJECT", "properties": {}},
    },
    {
        "name": "escalate_to_event_manager",
        "description": (
            "Forward a question to the event manager when the answer is not "
            "in the site data or the site guide (for example prices, "
            "accessibility details, or policies not listed). Call it at most "
            "once per question, then tell the visitor you will check with "
            "the event manager and let them know."
        ),
        "parameters": {
            "type": "OBJECT",
            "properties": {
                "question": {
                    "type": "STRING",
                    "description": "The visitor's question, in their words.",
                },
                "question_english": {
                    "type": "STRING",
                    "description": "The question translated into English.",
                },
                "language": {
                    "type": "STRING",
                    "description": "Language of the question, e.g. 'Spanish'.",
                },
                "event_id": {
                    "type": "INTEGER",
                    "description": "The event the question is about, if any.",
                },
            },
            "required": ["question", "question_english", "language"],
        },
    },
]


def borough_label(event):
    """Return the event's borough name, from its field or NYC Parks ID."""
    if event.borough:
        return BOROUGH_LABELS.get(event.borough, "")
    return BOROUGH_BY_PREFIX.get(event.park_ids[:1].upper(), "")


def event_status(event):
    """Describe whether an event is upcoming, under way, over, or cancelled."""
    if event.is_cancelled:
        return "cancelled"
    if event.has_ended:
        return "ended"
    return "upcoming"


def event_summary(event):
    """Return the short form of an event used in search results."""
    return {
        "id": event.pk,
        "title": event.title,
        "when": describe_when(event.start_time, event.end_time),
        "where": event.location or event.park_names,
        "borough": borough_label(event),
        "categories": event.categories,
        "posted_by_user": bool(event.host_id),
        "status": event_status(event),
        "page": reverse("event_detail", args=[event.pk]),
    }


def parse_date(value):
    """Return a date from YYYY-MM-DD text, or None when it isn't one."""
    try:
        return date.fromisoformat(str(value)[:10])
    except ValueError:
        return None


def clean_list(values, allowed):
    """Keep only allowed slugs from a list the model sent."""
    if not isinstance(values, list):
        return []
    return [value for value in values if value in allowed]


class Toolbox:
    """Run the assistant's tools on behalf of one visitor's request."""

    def __init__(self, user, inquiry_ids=None):
        self.user = user
        self.inquiry_ids = inquiry_ids if inquiry_ids is not None else []
        self.escalated = False

    declarations = DECLARATIONS

    def call(self, name, args):
        """Run a tool by name and return its JSON-safe result."""
        handler = {
            "search_events": self.search_events,
            "get_event_details": self.get_event_details,
            "get_my_activity": self.get_my_activity,
            "escalate_to_event_manager": self.escalate_to_event_manager,
        }.get(name)
        if handler is None:
            return {"error": f"Unknown tool {name}."}
        if not isinstance(args, dict):
            args = {}
        try:
            return handler(**args)
        except TypeError:
            return {"error": "Invalid arguments."}

    def search_events(
        self,
        query="",
        boroughs=None,
        categories=None,
        date_from="",
        date_to="",
        limit=DEFAULT_SEARCH_RESULTS,
    ):
        """Find upcoming events by keywords, borough, category, and dates."""
        start = parse_date(date_from) if date_from else None
        end = parse_date(date_to) if date_to else None
        if not start and not query:
            start = nyc_today()
        filters = {
            "date_from": start,
            "date_to": end,
            "borough": clean_list(boroughs, BOROUGH_LABELS),
            "category": clean_list(categories, CATEGORY_SLUGS),
        }
        try:
            limit = max(1, min(int(limit), MAX_SEARCH_RESULTS))
        except (TypeError, ValueError):
            limit = DEFAULT_SEARCH_RESULTS
        events = filter_events(
            Event.objects.filter(status=Event.Status.UPCOMING), filters
        )
        query = str(query or "").strip()
        if query:
            matches = search_events(events, query)
            words = query.split()
            if len(words) > 1 and not matches.exists():
                # Fall back to any word matching when every word together
                # finds nothing, e.g. "free jazz concert brooklyn".
                matches = search_events(events, " or ".join(words))
            events = matches
        else:
            events = events.order_by(
                F("start_time").asc(nulls_last=True), "end_time", "title", "pk"
            )
        results = [event_summary(event) for event in events[:limit]]
        return {"count": len(results), "events": results}

    def get_event_details(self, event_id=None):
        """Return the full public details of one event."""
        try:
            event = Event.objects.select_related("host").get(pk=int(event_id))
        except (TypeError, ValueError, Event.DoesNotExist):
            return {"error": "No event has that id."}
        interests = EventInterest.objects.filter(event=event)
        details = event_summary(event)
        details.update(
            {
                "description": Truncator(event.description).chars(DESCRIPTION_LIMIT),
                "park_names": event.park_names,
                "location": event.location,
                "host": event.host.get_display_name() if event.host else "",
                "source": "posted by a user" if event.host_id else "NYC Parks",
                "instructor": event.instructor,
                "contact_phone": event.contact_phone,
                "registration_link": event.registration_url,
                "nyc_parks_page": event.event_url,
                "interested_count": interests.filter(
                    status=EventInterest.Status.INTERESTED
                ).count(),
                "want_to_attend_count": interests.filter(
                    status=EventInterest.Status.WANT_TO_ATTEND
                ).count(),
            }
        )
        if self.user.is_authenticated:
            mine = interests.filter(user=self.user).first()
            details["my_attendance"] = mine.get_status_display() if mine else "none"
            details["i_am_the_host"] = event.host_id == self.user.pk
        return details

    def get_my_activity(self):
        """Return the signed-in visitor's own plans, hosting, and inbox counts."""
        user = self.user
        if not user.is_authenticated:
            return {
                "error": "The visitor is not signed in.",
                "sign_in_page": reverse("login"),
                "sign_up_page": reverse("register"),
            }
        interests = (
            user.event_interests.filter(event__status=Event.Status.UPCOMING)
            .select_related("event")
            .order_by(F("event__start_time").asc(nulls_last=True))
        )
        hosted = user.hosted_events.order_by(F("start_time").asc(nulls_last=True))
        unread_messages = (
            DirectMessage.objects.filter(
                Q(conversation__user_one=user) | Q(conversation__user_two=user),
                read_at__isnull=True,
            )
            .exclude(sender=user)
            .count()
        )
        return {
            "name": user.get_display_name(),
            "username": user.username,
            "planned_events": [
                dict(event_summary(i.event), attendance=i.get_status_display())
                for i in interests[:MAX_SEARCH_RESULTS]
                if i.event.is_open
            ],
            "hosted_upcoming_events": [
                event_summary(event)
                for event in hosted.filter(status=Event.Status.UPCOMING)[
                    :MAX_SEARCH_RESULTS
                ]
                if event.is_open
            ],
            "hosted_events_total": hosted.count(),
            "unread_notifications": user.notifications.filter(
                read_at__isnull=True
            ).count(),
            "unread_messages": unread_messages,
            "pending_contact_requests": user.received_contact_requests.filter(
                status="pending"
            ).count(),
            "settings": {
                "contact_permission": user.get_contact_permission_display(),
                "share_profile_with_attendees": user.share_profile_with_attendees,
                "settings_page": reverse("account_settings"),
            },
        }

    def escalate_to_event_manager(
        self, question="", question_english="", language="", event_id=None
    ):
        """Save the question for the event manager, once per visitor message."""
        if self.escalated:
            return {"status": "already_forwarded"}
        question = str(question or "").strip()[:2000]
        if not question:
            return {"error": "The question is empty."}
        event = None
        if event_id not in (None, ""):
            try:
                event = Event.objects.select_related("host").get(pk=int(event_id))
            except (TypeError, ValueError, Event.DoesNotExist):
                event = None
        user = self.user if self.user.is_authenticated else None
        inquiry = create_inquiry(
            user,
            question,
            question_english=str(question_english or "").strip()[:2000],
            language=str(language or "").strip()[:40],
            event=event,
        )
        self.escalated = True
        self.inquiry_ids.append(inquiry.pk)
        how = "in this chat window"
        if user:
            how += ", by email" + (" and in their notifications" if event else "")
        return {"status": "forwarded", "visitor_will_be_told": how}
