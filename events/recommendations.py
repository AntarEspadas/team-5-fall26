"""Personalized event recommendations based on recent search history."""

from collections import defaultdict
from datetime import timedelta

from django.utils import timezone

from .filters import filter_events
from .models import Event, EventInterest, Registration, SearchHistory
from .search import search_events

SEARCH_HISTORY_DAYS = 30
MAX_SEARCH_HISTORY = 50
RECOMMENDATION_HISTORY_LIMIT = 10
RECOMMENDATION_LIMIT = 5
PERSONALIZATION_FILTERS = {"time", "borough", "category"}


def record_search(user, query, filter_pairs):
    """Store a normalized search for an authenticated user and bound retention."""
    normalized_query = " ".join(query.split()).casefold()[:300]

    filters = defaultdict(list)
    for key, value in filter_pairs:
        if key in PERSONALIZATION_FILTERS:
            filters[key].append(value)
    normalized_filters = dict(filters)
    if "time" in normalized_filters:
        normalized_filters["time"] = normalized_filters["time"][0]
    if not normalized_query and not normalized_filters:
        return

    history = SearchHistory.objects.filter(user=user)
    cutoff = timezone.now() - timedelta(days=SEARCH_HISTORY_DAYS)
    history.filter(created_at__lt=cutoff).delete()

    latest = history.first()
    if (
        latest
        and latest.query == normalized_query
        and latest.filters == normalized_filters
        and latest.created_at >= timezone.now() - timedelta(minutes=30)
    ):
        return

    SearchHistory.objects.create(
        user=user,
        query=normalized_query,
        filters=normalized_filters,
    )
    stale_ids = list(
        history.order_by("-created_at", "-pk").values_list("pk", flat=True)[
            MAX_SEARCH_HISTORY:
        ]
    )
    if stale_ids:
        history.filter(pk__in=stale_ids).delete()


def recommended_events(user, limit=RECOMMENDATION_LIMIT):
    """Return upcoming events ranked by relevance to a user's recent searches."""
    if not user.is_authenticated or limit <= 0:
        return []

    history = list(
        SearchHistory.objects.filter(
            user=user,
            created_at__gte=timezone.now() - timedelta(days=SEARCH_HISTORY_DAYS),
        )[:RECOMMENDATION_HISTORY_LIMIT]
    )
    if not history:
        return []

    now = timezone.now()
    eligible = (
        Event.objects.filter(
            status=Event.Status.UPCOMING,
            start_time__gte=now,
        )
        .exclude(title__istartswith="cancelled")
        .exclude(title__istartswith="canceled")
        .exclude(pk__in=Registration.objects.filter(user=user).values("event_id"))
        .exclude(pk__in=EventInterest.objects.filter(user=user).values("event_id"))
    )

    scores = defaultdict(float)
    for history_position, search in enumerate(history):
        filtered = filter_events(eligible, search.filters)
        if search.query:
            matches = search_events(filtered, search.query)
        else:
            matches = filtered.order_by("start_time", "pk")
        recency_weight = 1 / (history_position + 1)
        for result_position, event in enumerate(matches.iterator(chunk_size=200)):
            scores[event.pk] += recency_weight / (result_position + 1)

    if not scores:
        return []

    events = Event.objects.filter(pk__in=scores).in_bulk()
    ranked = sorted(
        events.values(),
        key=lambda event: (
            -scores[event.pk],
            event.start_time is None,
            event.start_time or now,
            event.pk,
        ),
    )
    recommendations = []
    seen_series = set()
    for event in ranked:
        series = _event_series_key(event)
        if series in seen_series:
            continue
        seen_series.add(series)
        recommendations.append(event)
        if len(recommendations) == limit:
            break
    return recommendations


def _event_series_key(event):
    """Group dated instances of the same titled event at the same venue."""
    venue = event.location or event.park_names
    if not venue:
        return ("event", event.pk)

    def normalize(value):
        return " ".join(value.split()).casefold()

    return (
        normalize(event.title),
        normalize(venue),
        normalize(event.categories),
        event.host_id,
    )
