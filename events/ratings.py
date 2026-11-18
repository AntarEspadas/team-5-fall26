"""Rules for rating an event's host and summarising a host's ratings."""

from datetime import timedelta

from django.db.models import Avg, Count
from django.utils import timezone

from accounts.ban_services import get_active_ban

from .models import HostRating, Registration

RATING_WINDOW = timedelta(days=14)
MIN_RATINGS_FOR_AVERAGE = 3
LATEST_COMMENTS = 5


def rating_closes_at(event):
    """Return when ratings stop being accepted for an ended event.

    The window runs from when the event counts as over (see
    ``Event.ended_at``): its end time, or midnight in New York after its
    start date when it has no end time. Events with neither never close.
    """
    ended_at = event.ended_at
    if ended_at is None:
        return None
    return ended_at + RATING_WINDOW


def rating_window_open(event):
    """Return whether an event is over, not cancelled, and still rateable."""
    if event.is_cancelled or not event.has_ended:
        return False
    closes_at = rating_closes_at(event)
    return closes_at is None or timezone.now() <= closes_at


def rating_block_reason(event, user):
    """Explain why ``user`` can't rate this event's host, or return None.

    The messages say what to do next, so the view can show them as toasts.
    """
    if event.host_id is None:
        return "Only events posted on this site have a host you can rate."
    if event.host_id == user.pk:
        return "You can't rate your own event."
    if event.is_cancelled:
        return "This event was cancelled, so there's nothing to rate."
    if not event.has_ended:
        return "You can rate this host once the event has ended."
    if not Registration.objects.filter(event=event, user=user).exists():
        return "Only people who registered for this event can rate its host."
    closes_at = rating_closes_at(event)
    if closes_at is not None and timezone.now() > closes_at:
        return "Ratings close 14 days after an event ends."
    if get_active_ban(user) is not None:
        return "Your account is suspended, so you can't rate hosts right now."
    return None


def host_rating_summary(host):
    """Return a host's rating count, average, and whether to show the average.

    The average stays hidden until ``MIN_RATINGS_FOR_AVERAGE`` people have
    rated the host, so one early rating doesn't define a new host.
    """
    totals = HostRating.objects.filter(host=host).aggregate(
        count=Count("pk"), average=Avg("stars")
    )
    count = totals["count"]
    average = round(totals["average"], 1) if totals["average"] is not None else None
    return {
        "count": count,
        "average": average,
        "show_average": count >= MIN_RATINGS_FOR_AVERAGE,
        "stars_filled": round(average) if average is not None else 0,
    }


def latest_host_comments(host, limit=LATEST_COMMENTS):
    """Return the newest ratings for ``host`` that include a comment."""
    return (
        HostRating.objects.filter(host=host)
        .exclude(comment="")
        .select_related("event")
        .order_by("-updated_at", "-pk")[:limit]
    )
