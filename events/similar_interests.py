"""Find opted-in people interested in events with related categories.

Only public event-category labels are used as explanations. User email,
private messages, and attendance details are never included in suggestions.
"""

from django.db.models import Exists, OuterRef, Q
from django.utils import timezone

from accounts.ban_services import get_active_ban
from accounts.models import UserBan

from .filters import CATEGORY_GROUPS, CATEGORY_NAMES, CATEGORY_SEPARATOR
from .models import Event, EventInterest, Registration

DEFAULT_SUGGESTION_LIMIT = 6


# Saved "My Interests" choices use different identifiers from NYC Parks
# event categories. Only count an interest when both event categories support
# that specific choice; selecting "other" does not match every event.
INTEREST_EVENT_CATEGORIES = {
    "walking": ("Walking", "Hiking"),
    "gardening": ("Gardening",),
    "volunteering": CATEGORY_NAMES["volunteer"],
    "nature": CATEGORY_NAMES["nature"],
    "sports": (
        *CATEGORY_NAMES["sports"],
        *CATEGORY_NAMES["fitness"],
        *CATEGORY_NAMES["yoga"],
    ),
    "photography": ("Photography",),
    "community": ("Community Events", *CATEGORY_NAMES["festivals"]),
    "cleanup": ("Park Cleanup", "Clean Up", "Cleanup", "It's My Park"),
    "arts": (
        *CATEGORY_NAMES["art"],
        *CATEGORY_NAMES["dance"],
        *CATEGORY_NAMES["music-film"],
    ),
}


def shares_selected_interest(source_tags, other_categories, viewer, member):
    """Confirm a saved interest still belongs to both people and both events."""
    if viewer is None or not viewer.is_authenticated:
        return False

    shared_choices = set(viewer.interests or []) & set(member.interests or [])
    if not shared_choices:
        return False

    source_names = {tag.casefold() for tag in source_tags}
    other_names = {tag.casefold() for tag in category_tags(other_categories)}
    for choice in shared_choices:
        categories = {
            name.casefold() for name in INTEREST_EVENT_CATEGORIES.get(choice, ())
        }
        if source_names & categories and other_names & categories:
            return True
    return False


def available_event(event):
    """Accept only live, non-cancelled events, even before the hourly sync."""
    return event.is_open and not event.is_cancelled


def category_tags(categories):
    """Split the NYC Parks category string into nonempty public labels."""
    return [tag.strip() for tag in (categories or "").split("|") if tag.strip()]


def category_terms(tags):
    """Include related labels in the same existing event-filter category group."""
    terms = set(tags)
    normalized = {tag.casefold() for tag in tags}

    for slug, _, _ in CATEGORY_GROUPS:
        members = CATEGORY_NAMES[slug]
        if normalized.intersection(name.casefold() for name in members):
            terms.update(members)

    return sorted(terms)


def category_query(field, label):
    """Match a complete category in a delimiter-separated database field."""
    separator = CATEGORY_SEPARATOR
    return (
        Q(**{f"{field}__iexact": label})
        | Q(**{f"{field}__istartswith": label + separator})
        | Q(**{f"{field}__iendswith": separator + label})
        | Q(**{f"{field}__icontains": separator + label + separator})
    )


def shared_interest_label(source_tags, other_categories):
    """Describe the actual category overlap without revealing private data."""
    other_tags = category_tags(other_categories)
    other_names = {name.casefold() for name in other_tags}

    for tag in source_tags:
        if tag.casefold() in other_names:
            return tag

    source_names = {name.casefold() for name in source_tags}
    for slug, label, _ in CATEGORY_GROUPS:
        members = {name.casefold() for name in CATEGORY_NAMES[slug]}
        if source_names & members and other_names & members:
            return label

    return None


def public_match_for_event(event, recipient, viewer):
    """Return a related public interest for an opted-in member, if any.

    Never reveal the name or details of an event the recipient follows.
    Both people must still select a relevant shared interest; only the
    matching event category is disclosed.
    """
    if (
        viewer is None
        or not viewer.is_authenticated
        or not available_event(event)
        or not category_tags(event.categories)
        or not recipient.share_profile_with_attendees
        or not recipient.discoverable_for_similar_events
        or not recipient.is_active
        or get_active_ban(recipient) is not None
    ):
        return None

    interests = (
        EventInterest.objects.filter(
            user=recipient,
            status=EventInterest.Status.INTERESTED,
            event__status=Event.Status.UPCOMING,
        )
        .exclude(event=event)
        .select_related("event")
        .order_by("event__start_time", "pk")
    )
    source_tags = category_tags(event.categories)
    for interest in interests:
        related_event = interest.event
        if not available_event(related_event):
            continue
        label = shared_interest_label(source_tags, related_event.categories)
        if label and shares_selected_interest(
            source_tags, related_event.categories, viewer, recipient
        ):
            return label
    return None


def find_similar_interest_users(event, viewer=None, limit=DEFAULT_SUGGESTION_LIMIT):
    """Return unique opted-in users interested in other upcoming related events.

    Users on this event's interested or registered lists are left out to avoid
    duplicate suggestions. Both public-profile sharing and the separate
    similar-event discovery consent must be enabled, and both people must
    currently select a shared My Interests topic relevant to the events.
    """
    tags = category_tags(event.categories)
    if (
        not available_event(event)
        or not tags
        or limit < 1
        or viewer is None
        or not viewer.is_authenticated
        or not viewer.interests
    ):
        return []

    matching_categories = Q()
    for label in category_terms(tags):
        matching_categories |= category_query("event__categories", label)

    same_event_interests = EventInterest.objects.filter(event=event).values("user_id")
    same_event_registrations = Registration.objects.filter(event=event).values(
        "user_id"
    )
    # A ban does not set is_active=False, so check active bans separately.
    active_bans = UserBan.objects.filter(
        user_id=OuterRef("user_id"),
        lifted_at__isnull=True,
    )
    active_bans = active_bans.filter(
        Q(expires_at__isnull=True) | Q(expires_at__gt=timezone.now())
    )
    candidates = EventInterest.objects.filter(
        matching_categories,
        status=EventInterest.Status.INTERESTED,
        event__status=Event.Status.UPCOMING,
        user__is_active=True,
        user__share_profile_with_attendees=True,
        user__discoverable_for_similar_events=True,
    )
    candidates = candidates.alias(has_active_ban=Exists(active_bans))
    candidates = (
        candidates.filter(has_active_ban=False)
        .exclude(event=event)
        .exclude(user_id__in=same_event_interests)
        .exclude(user_id__in=same_event_registrations)
        .select_related("user", "event")
        .order_by("user__display_name", "user__username", "event__start_time", "pk")
    )
    if viewer is not None and viewer.is_authenticated:
        candidates = candidates.exclude(user=viewer)

    suggestions = []
    seen_users = set()
    for interest in candidates.iterator(chunk_size=200):
        if interest.user_id in seen_users or not available_event(interest.event):
            continue
        label = shared_interest_label(tags, interest.event.categories)
        if label is None or not shares_selected_interest(
            tags, interest.event.categories, viewer, interest.user
        ):
            continue
        seen_users.add(interest.user_id)
        suggestions.append({"user": interest.user, "shared_interest": label})
        if len(suggestions) >= limit:
            break

    return suggestions
