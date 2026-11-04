"""Filters that narrow event listings by date range, borough, and category."""

from datetime import datetime, time, timedelta
from zoneinfo import ZoneInfo

from django import forms
from django.db.models import Q
from django.utils import timezone

NYC_TIMEZONE = ZoneInfo("America/New_York")

DATE_PRESETS = (
    ("today", "Today"),
    ("weekend", "This weekend"),
    ("month", "This month"),
)

# NYC Parks property IDs start with a borough letter, e.g. "B073" is in Brooklyn.
BOROUGHS = (
    ("manhattan", "Manhattan", "M"),
    ("brooklyn", "Brooklyn", "B"),
    ("queens", "Queens", "Q"),
    ("bronx", "The Bronx", "X"),
    ("staten-island", "Staten Island", "R"),
)
BOROUGH_PREFIXES = {slug: prefix for slug, _, prefix in BOROUGHS}

# Friendly groups of the feed's own category names, which must match exactly.
CATEGORY_GROUPS = (
    (
        "fitness",
        "Fitness",
        (
            "Fitness",
            "Exercise Classes",
            "Outdoor Fitness",
            "Shape Up NYC",
            "Running/Jogging",
            "Strength Training/Weightlifting",
            "Walking",
        ),
    ),
    ("yoga", "Yoga & Pilates", ("Yoga & Pilates Classes",)),
    (
        "sports",
        "Sports",
        (
            "Sports",
            "Basketball/Netball",
            "Baseball/Softball",
            "Football",
            "Social Sports",
            "Sports Camps",
            "Swimming/Aquatics",
            "Track & Field",
            "Adventure Courses/Rock Climbing",
        ),
    ),
    (
        "art",
        "Arts & Crafts",
        ("Art", "Arts & Crafts", "Arts, Culture & Fun Series", "Theater"),
    ),
    ("dance", "Dance", ("Dance", "Dance Classes")),
    (
        "music-film",
        "Music & Film",
        (
            "Concerts",
            "Free Summer Concerts",
            "Film",
            "Movies Under the Stars",
            "Free Summer Movies",
        ),
    ),
    (
        "nature",
        "Nature & Outdoors",
        (
            "Nature",
            "Gardening",
            "Wildlife",
            "Birding",
            "Hiking",
            "Waterfront",
            "Urban Park Rangers",
            "Astronomy",
        ),
    ),
    (
        "kids",
        "Kids & Family",
        ("Best for Kids", "Family Camping with the Urban Park Rangers"),
    ),
    ("history", "History & Tours", ("History", "Tours", "Open House New York")),
    (
        "education",
        "Education & Talks",
        ("Education", "STEM Classes", "Talks", "Workshops"),
    ),
    ("games", "Games", ("Games",)),
    (
        "festivals",
        "Festivals & Food",
        ("Festivals", "Fall Festivals", "Halloween", "Food", "Markets"),
    ),
    ("volunteer", "Volunteering", ("Volunteer", "It's My Park")),
    ("seniors", "Seniors & Accessible", ("Seniors", "Accessible Activities")),
)
# Hosted events store the group labels themselves, so labels match as well.
CATEGORY_NAMES = {
    slug: tuple(dict.fromkeys((label, *names)))
    for slug, label, names in CATEGORY_GROUPS
}

# Categories are stored as one string, e.g. "Art | Games".
CATEGORY_SEPARATOR = " | "


def nyc_today():
    """Return today's date in New York, where every event takes place."""
    return timezone.localtime(timezone.now(), NYC_TIMEZONE).date()


def preset_range(preset, today):
    """Return the inclusive ``(start, end)`` dates a quick date button stands for.

    "This weekend" is the coming Saturday and Sunday, or only the remaining
    days once the weekend has started; "This month" runs from today to the
    last day of the month.
    """
    if preset == "today":
        return today, today
    if preset == "weekend":
        if today.weekday() == 6:
            return today, today
        saturday = today + timedelta(days=5 - today.weekday())
        return saturday, saturday + timedelta(days=1)
    next_month = (today.replace(day=28) + timedelta(days=4)).replace(day=1)
    return today, next_month - timedelta(days=1)


def start_of_day(day):
    """Return midnight in New York at the start of ``day``."""
    return datetime.combine(day, time.min, tzinfo=NYC_TIMEZONE)


def category_match(name):
    """Match events whose category list contains exactly ``name``."""
    separator = CATEGORY_SEPARATOR
    return (
        Q(categories=name)
        | Q(categories__startswith=name + separator)
        | Q(categories__endswith=separator + name)
        | Q(categories__contains=separator + name + separator)
    )


class EventFilterForm(forms.Form):
    """Validate the filter bar shown under the event search box.

    A quick date button submits ``when``, which replaces any typed dates so
    the dates on screen always describe the results.
    """

    when = forms.ChoiceField(choices=DATE_PRESETS, required=False)
    date_from = forms.DateField(
        label="From",
        required=False,
        widget=forms.DateInput(attrs={"type": "date"}, format="%Y-%m-%d"),
    )
    date_to = forms.DateField(
        label="To",
        required=False,
        widget=forms.DateInput(attrs={"type": "date"}, format="%Y-%m-%d"),
    )
    borough = forms.MultipleChoiceField(
        choices=[(slug, label) for slug, label, _ in BOROUGHS],
        required=False,
        widget=forms.CheckboxSelectMultiple,
    )
    category = forms.MultipleChoiceField(
        choices=[(slug, label) for slug, label, _ in CATEGORY_GROUPS],
        required=False,
        widget=forms.CheckboxSelectMultiple,
    )

    def __init__(self, *args, **kwargs):
        """Prefix element IDs so they never clash with other forms on a page."""
        kwargs.setdefault("auto_id", "filter-%s")
        super().__init__(*args, **kwargs)

    def clean(self):
        """Expand quick date buttons and drop date ranges that end too early."""
        cleaned = super().clean()
        if cleaned.get("when"):
            cleaned["date_from"], cleaned["date_to"] = preset_range(
                cleaned["when"], nyc_today()
            )
        start, end = cleaned.get("date_from"), cleaned.get("date_to")
        if start and end and start > end:
            self.add_error("date_to", "The end date must be on or after the start.")
            cleaned.pop("date_from")
        return cleaned


def filter_params(filters):
    """Return the active filters as ordered query-string pairs.

    Quick date buttons are expanded to their dates, so the pairs can rebuild
    the same results on another page or populate the form again.
    """
    params = []
    for key in ("date_from", "date_to"):
        if filters.get(key):
            params.append((key, filters[key].isoformat()))
    params.extend(("borough", slug) for slug in filters.get("borough", []))
    params.extend(("category", slug) for slug in filters.get("category", []))
    return params


def filter_events(events, filters):
    """Narrow an event queryset to events matching every active filter.

    Events count as being on a date range when they overlap it. Selecting
    several boroughs or categories matches any of them.
    """
    start, end = filters.get("date_from"), filters.get("date_to")
    if start:
        events = events.filter(
            Q(end_time__gte=start_of_day(start))
            | Q(end_time__isnull=True, start_time__gte=start_of_day(start))
        )
    if end:
        events = events.filter(start_time__lt=start_of_day(end + timedelta(days=1)))
    boroughs = Q()
    for slug in filters.get("borough", []):
        boroughs |= Q(borough=slug) | Q(park_ids__istartswith=BOROUGH_PREFIXES[slug])
    categories = Q()
    for slug in filters.get("category", []):
        for name in CATEGORY_NAMES[slug]:
            categories |= category_match(name)
    return events.filter(boroughs, categories)
