"""Understand plain-language searches such as "kids events in Brooklyn tonight".

When a visitor submits the search box, what they typed comes here first.
Dates, times of day, boroughs, and broad kinds of event become the same
filters as the filter bar, filler such as "show me" or "things to do" is
dropped, and the rest stays as search keywords. Quoted phrases and
``-exclusions`` are kept exactly as typed.

With Gemini configured (``CHATBOT_ENABLED``), prompts of a few words or more
are interpreted by Gemini, so other phrasings and languages work too. The
rules here are the fallback whenever Gemini is off or fails.

The archive of past events uses the same reading with ``past=True``, so
"yesterday", "last weekend", or "Monday" mean days that have gone by.
"""

import logging
import re
from dataclasses import dataclass, field
from datetime import date, timedelta
from urllib.parse import quote, urlencode

from django.conf import settings
from django.urls import reverse

from .filters import (
    BOROUGHS,
    CATEGORY_GROUPS,
    TIMES_OF_DAY,
    EventFilterForm,
    filter_events,
    filter_params,
    nyc_today,
    preset_range,
)
from .models import Event
from .search import search_events

logger = logging.getLogger(__name__)

MAX_PROMPT_LENGTH = 300
MAX_KEYWORDS_LENGTH = 200
# Shorter prompts are usually plain keywords, which the rules handle well.
GEMINI_MIN_WORDS = 3

BOROUGH_LABELS = {slug: label for slug, label, _ in BOROUGHS}
CATEGORY_LABELS = {slug: label for slug, label, _ in CATEGORY_GROUPS}
TIME_LABELS = {slug: label for slug, label, _, _ in TIMES_OF_DAY}

WEEKDAYS = (
    "monday",
    "tuesday",
    "wednesday",
    "thursday",
    "friday",
    "saturday",
    "sunday",
)
MONTHS = {
    "jan": 1,
    "january": 1,
    "feb": 2,
    "february": 2,
    "mar": 3,
    "march": 3,
    "apr": 4,
    "april": 4,
    "may": 5,
    "jun": 6,
    "june": 6,
    "jul": 7,
    "july": 7,
    "aug": 8,
    "august": 8,
    "sep": 9,
    "sept": 9,
    "september": 9,
    "oct": 10,
    "october": 10,
    "nov": 11,
    "november": 11,
    "dec": 12,
    "december": 12,
}
MONTH = "|".join(sorted(MONTHS, key=len, reverse=True))
WEEKDAY = "|".join(WEEKDAYS)
ORDINAL = r"(?:st|nd|rd|th)?"

# Quoted phrases and -exclusions use the search's own syntax; keep them.
PROTECTED = re.compile(r'"[^"]*"?|(?<!\S)-\S+')

# Words that only name a borough. Manhattan Beach is a park in Brooklyn.
BOROUGH_PATTERNS = {
    "manhattan": r"manhattan(?!\s+beach)",
    "brooklyn": r"brooklyn",
    "queens": r"queens",
    "bronx": r"(?:the\s+)?bronx",
    "staten-island": r"staten\s+island",
}

# Audiences and broad kinds of event, which listings mark with a category
# but rarely spell out. Specific activities such as "yoga" or "concert"
# stay keywords, because full-text search finds them in titles too.
CATEGORY_PATTERNS = {
    "kids": (
        r"(?:kid|child|family)[- ]friendly|kids?|child(?:ren)?(?:'s)?"
        r"|famil(?:y|ies)|toddlers?"
    ),
    "seniors": (
        r"seniors?|older\s+adults|elderly|(?:wheelchair[- ])?accessible|wheelchair"
    ),
    "fitness": r"fitness|workouts?|exercis(?:e|es|ing)",
    "sports": r"sports?",
    "art": r"(?<!martial )(?:arts?(?:\s+(?:and|&)\s+crafts)?|crafts?)",
    "music-film": r"music",
    "nature": r"nature",
    "history": r"histor(?:y|ic|ical)",
    "education": r"education(?:al)?",
}

TIME_PATTERNS = {
    "morning": r"(?:in\s+the\s+)?mornings?",
    "afternoon": r"(?:in\s+the\s+)?afternoons?|noon",
    "evening": r"(?:in\s+the\s+)?evenings?|(?:at\s+)?night(?:s|time)?|after\s+work",
}
CLOCK_TIME = re.compile(
    r"\b(?:(?:at|after|around|from|by|before)\s+)?(\d{1,2})(?::\d{2})?\s*(am|pm)\b"
)

FILLER_PHRASES = re.compile(
    r"\b(?:things|stuff)\s+to\s+do\b|\bwhat'?s\s+on\b|\blooking\s+for\b"
    r"|\b(?:near|around|close\s+to)\s+me\b|\bnew\s+york(?:\s+city)?\b|\bnyc\b"
    r"|\bin\s+the\s+city\b|\bshow\s+(?:me|us)\b"
)
# Words that are not search stop words but would make the search require
# them; plus common stop words, so the keywords read cleanly. Generic words
# such as "classes" are dropped too: "yoga classes" should find every yoga
# event, not only listings that also say "class". "show" is only filler in
# "show me", since a puppet show is something to search for.
FILLER_WORDS = frozenset("""
    a about activities activity all also am an and any anything are around at
    be between by can class classes coming could do does during event events
    find for free from fun get give go going happening how i i'd i'm im in is
    it just lesson lessons let's lets like list looking maybe me my near nearby
    need next of on options our out parks please plans program programs really
    recommend search see session sessions so some something stuff suggest tell
    that the there these thing things this through till to until up upcoming
    us very want wanna we what what's whats when where which will with would
    you your
    """.split())
PUNCTUATION = ".,!?;:()[]{}\"'"


@dataclass
class Interpretation:
    """Keywords and filters read from a prompt; empty fields mean "any"."""

    keywords: str = ""
    date_from: date | None = None
    date_to: date | None = None
    time: str = ""
    boroughs: list = field(default_factory=list)
    categories: list = field(default_factory=list)

    @property
    def has_filters(self):
        """Return whether the prompt asked for any date, time, or place."""
        return bool(
            self.date_from
            or self.date_to
            or self.time
            or self.boroughs
            or self.categories
        )

    def changes(self, prompt):
        """Return whether searching this differs from searching the prompt."""
        return self.has_filters or self.keywords.lower() != prompt.lower()

    def merged_with(self, filters):
        """Combine with filters already chosen in the filter bar.

        Dates and times in the prompt replace the chosen ones; boroughs and
        categories are added to them.
        """
        merged = {
            "date_from": filters.get("date_from"),
            "date_to": filters.get("date_to"),
            "time": filters.get("time") or "",
            "borough": list(filters.get("borough") or []),
            "category": list(filters.get("category") or []),
        }
        if self.date_from or self.date_to:
            merged["date_from"], merged["date_to"] = self.date_from, self.date_to
        if self.time:
            merged["time"] = self.time
        for key, chosen in (("borough", self.boroughs), ("category", self.categories)):
            merged[key] += [slug for slug in chosen if slug not in merged[key]]
        return merged


def natural_search_url(request, past=False):
    """Return the search a plain-language query stands for, or None.

    The search form's hidden ``nl`` field identifies a newly typed prompt.
    Shared links without it only use the built-in rules when they contain an
    explicit date phrase; ordinary shared keyword links stay unchanged.
    An ``exact=1`` link always bypasses interpretation.

    Args:
        request: the search request.
        past: read the prompt as a search of the archive of past events,
            and send it back to the archive instead of the upcoming search.
    """
    prompt = " ".join(request.GET.get("q", "").split())[:MAX_PROMPT_LENGTH]
    shown = " ".join(request.GET.get("nl", "").split())
    if not prompt or prompt == shown or request.GET.get("exact") == "1":
        return None
    if "asked" in request.GET:
        # A resolved prompt must not be interpreted again after the redirect.
        return None

    today = nyc_today()
    if "nl" in request.GET:
        meaning = interpret(prompt, today, past=past)
    else:
        # A direct link like /search?q=kids+event+this+week should work.
        # Do not change existing links that only supply keywords, boroughs,
        # or categories, and do not make an AI request for shared URLs.
        meaning = parse_prompt(prompt, today, past=past)
        if not (meaning.date_from or meaning.date_to):
            return None
    if not meaning.changes(prompt):
        return None
    form = EventFilterForm(request.GET)
    form.is_valid()
    filters = meaning.merged_with(form.cleaned_data)
    keywords = meaning.keywords
    if not keywords and not filter_params(filters) and not past:
        # "What's on?" lists everything coming up.
        filters["date_from"] = today
    status = Event.Status.DONE if past else Event.Status.UPCOMING
    broader = any_word_query(keywords)
    if broader:
        events = filter_events(
            Event.objects.listed().filter(status=status), filters
        )
        if not search_events(events, keywords).exists():
            keywords = broader
    params = [("q", keywords)] if keywords else []
    params += filter_params(filters) + [("asked", prompt)]
    page = reverse("archived_events" if past else "search")
    return f"{page}?{urlencode(params, quote_via=quote)}"

def interpret(prompt, today, past=False):
    """Return what a prompt asks for, using Gemini when it is configured.

    With ``past`` the prompt searches past events, so dates without a year
    or a "last"/"next" mean the most recent such day.
    """
    prompt = " ".join(str(prompt).split())[:MAX_PROMPT_LENGTH]
    if wants_gemini(prompt):
        # Imported here so the events app only needs the assistant for this.
        from chatbot.gemini import GeminiError, generate_json

        try:
            data = generate_json(
                [{"text": gemini_instructions(prompt, today, past)}], GEMINI_SCHEMA
            )
        except GeminiError as error:
            logger.warning("Gemini could not read a search, using rules: %s", error)
        else:
            meaning = from_gemini(data)
            if meaning is not None:
                return meaning
            logger.warning("Gemini returned an unusable search, using rules")
    return parse_prompt(prompt, today, past=past)


def wants_gemini(prompt):
    """Return whether Gemini should read the prompt instead of the rules."""
    return (
        bool(getattr(settings, "CHATBOT_ENABLED", False))
        and len(prompt.split()) >= GEMINI_MIN_WORDS
        and not PROTECTED.search(prompt)
    )


GEMINI_SCHEMA = {
    "type": "OBJECT",
    "properties": {
        "keywords": {"type": "STRING"},
        "boroughs": {
            "type": "ARRAY",
            "items": {"type": "STRING", "enum": list(BOROUGH_LABELS)},
        },
        "categories": {
            "type": "ARRAY",
            "items": {"type": "STRING", "enum": list(CATEGORY_LABELS)},
        },
        "date_from": {"type": "STRING"},
        "date_to": {"type": "STRING"},
        "time_of_day": {"type": "STRING", "enum": ["any", *TIME_LABELS]},
    },
    "required": [
        "keywords",
        "boroughs",
        "categories",
        "date_from",
        "date_to",
        "time_of_day",
    ],
}


def gemini_instructions(prompt, today, past=False):
    """Return the request that asks Gemini to turn a prompt into filters."""
    if past:
        saturday, sunday = weekend("last", today)
        weekend_name, events = "last weekend", "past events that already ended"
        dates = (
            "Dates mean the most recent such day on or before today, e.g. "
            "'Monday' is the last Monday and 'yesterday' is the day before "
            "today. "
        )
    else:
        saturday, sunday = preset_range("weekend", today)
        weekend_name, events, dates = "this weekend", "upcoming events", ""
    categories = "; ".join(
        f"{slug} ({label})" for slug, label in CATEGORY_LABELS.items()
    )
    return (
        f"Today is {today:%A, %B} {today.day}, {today.year} in New York City "
        f"({today.isoformat()}); {weekend_name} is {saturday.isoformat()} to "
        f"{sunday.isoformat()}. A visitor typed the search below on an NYC "
        f"Parks events website. Turn it into filters for {events}. {dates}\n"
        "- keywords: English words for the activity, topic, or place that "
        "event listings should mention, such as 'yoga' or 'central park'. "
        "Translate other languages. Leave out dates, times, boroughs, "
        "generic words such as 'class' or 'session', and filler such as "
        "'events', 'things to do', 'free', or 'near me'. Empty if nothing is "
        "left.\n"
        "- boroughs: boroughs the visitor named, or that a park or "
        "neighborhood they named is in.\n"
        f"- categories: only broad kinds of event they asked for: {categories}. "
        "Leave a specific activity in keywords instead.\n"
        "- date_from and date_to: the first and last day they asked about, as "
        "YYYY-MM-DD, or empty when they gave no date.\n"
        "- time_of_day: morning (before noon), afternoon (noon to 5 PM), "
        "evening (after 5 PM), or any.\n"
        f'Search: """{prompt}"""'
    )


def from_gemini(data):
    """Return Gemini's reply as an Interpretation, or None if it is unusable."""
    if not isinstance(data, dict):
        return None
    start, end = iso_date(data.get("date_from")), iso_date(data.get("date_to"))
    if start and end and end < start:
        start, end = end, start
    time = data.get("time_of_day")
    keywords = clean_keywords(str(data.get("keywords") or ""))
    words = set(keywords.lower().split())
    return Interpretation(
        keywords=keywords,
        date_from=start,
        date_to=end,
        time=time if time in TIME_LABELS else "",
        boroughs=allowed(data.get("boroughs"), BOROUGH_LABELS),
        # Keywords already search categories, so a category that repeats one
        # would only hide listings filed elsewhere, like yoga under Fitness.
        categories=[
            slug
            for slug in allowed(data.get("categories"), CATEGORY_LABELS)
            if not words & label_words(slug)
        ],
    )


def label_words(slug):
    """Return the lowercase words of a category's label, e.g. {"yoga", "pilates"}."""
    return set(re.findall(r"[a-z]+", CATEGORY_LABELS[slug].lower()))


def iso_date(value):
    """Return a date from YYYY-MM-DD text, or None."""
    try:
        return date.fromisoformat(str(value or "")[:10])
    except ValueError:
        return None


def allowed(values, choices):
    """Keep known slugs from a list, in order and without repeats."""
    if not isinstance(values, list):
        return []
    return list(dict.fromkeys(value for value in values if value in choices))


def parse_prompt(prompt, today, past=False):
    """Read a prompt with built-in English rules relative to ``today``.

    With ``past``, dates point back in time: "Monday" or "Nov 17" mean the
    most recent such day, and "this week" or "this month" end today.
    """
    text = prompt.replace("“", '"').replace("”", '"').replace("’", "'")
    protected = []

    def protect(match):
        protected.append(match.group(0))
        return f" {chr(0xE000 + len(protected) - 1)} "

    text = PROTECTED.sub(protect, text).lower()
    meaning = Interpretation()
    ranges, times = [], []

    def take(pattern, read):
        """Blank out every match that ``read`` understands."""
        nonlocal text

        def replace(match):
            return " " if read(match) else match.group(0)

        text = re.sub(pattern, replace, text)

    def add_range(start, end=None):
        ranges.append((start, end or start))
        return True

    def add_time(slug):
        times.append(slug)
        return True

    def add_clock(match):
        hour = int(match[1]) % 12 + (12 if match[2] == "pm" else 0)
        return add_time(next(s for s, _, _, end in TIMES_OF_DAY if hour < end))

    def add_date(year, month, day):
        try:
            value = date(int(year) if year else today.year, int(month), int(day))
        except ValueError:
            return False
        if not year and past and value > today:
            value = value.replace(year=today.year - 1)
        elif not year and not past and value < today:
            value = value.replace(year=today.year + 1)
        return add_range(value)

    take(
        r"\b(\d{4})-(\d{1,2})-(\d{1,2})\b",
        lambda m: add_date(m[1], m[2], m[3]),
    )
    take(
        r"\b(\d{1,2})/(\d{1,2})(?:/(\d{4}|\d{2}))?\b",
        lambda m: add_date(full_year(m[3]), m[1], m[2]),
    )
    take(
        rf"\b({MONTH})\.?\s+(\d{{1,2}}){ORDINAL}\b(?:,?\s+(\d{{4}})\b)?",
        lambda m: add_date(m[3], MONTHS[m[1]], m[2]),
    )
    take(
        rf"\b(\d{{1,2}}){ORDINAL}\s+(?:of\s+)?({MONTH})\b(?:,?\s+(\d{{4}})\b)?",
        lambda m: add_date(m[3], MONTHS[m[2]], m[1]),
    )
    take(
        rf"\b(?:in|during)\s+({MONTH})\b",
        lambda m: add_range(*month_range(MONTHS[m[1]], today, past)),
    )
    take(
        r"\b(?:last|past|previous)\s+(\d{1,2})\s+(days?|weeks?)\b",
        lambda m: add_range(today - recent_span(m[1], m[2]), today),
    )
    take(r"\btonight\b", lambda m: add_range(today) and add_time("evening"))
    take(r"\btoday\b", lambda m: add_range(today))
    take(r"\byesterday\b", lambda m: add_range(today - timedelta(days=1)))
    take(r"\b(?:tomorrow|tmrw)\b", lambda m: add_range(today + timedelta(days=1)))
    take(
        r"\b(?:(this|next|coming|the|last|past|previous)\s+)?weekends?\b",
        lambda m: add_range(*weekend(qualifier(m[1], past), today)),
    )
    take(
        r"\b(this|next|coming|last|past|previous)\s+week\b",
        lambda m: add_range(*week(qualifier(m[1], past), today)),
    )
    take(
        r"\b(this|next|coming|last|past|previous)\s+month\b",
        lambda m: add_range(*month_phrase(qualifier(m[1], past), today)),
    )
    take(
        rf"\b(?:(this|next|coming|on|last|past|previous)\s+)?({WEEKDAY})s?\b",
        lambda m: add_range(
            weekday(WEEKDAYS.index(m[2]), qualifier(m[1], past), today)
        ),
    )
    take(CLOCK_TIME, add_clock)
    for slug, pattern in TIME_PATTERNS.items():
        take(rf"\b(?:{pattern})\b", lambda m, slug=slug: add_time(slug))
    for slug, pattern in BOROUGH_PATTERNS.items():
        take(
            rf"\b(?:{pattern})\b", lambda m, slug=slug: add_once(meaning.boroughs, slug)
        )
    for slug, pattern in CATEGORY_PATTERNS.items():
        take(
            rf"\b(?:{pattern})\b",
            lambda m, slug=slug: add_once(meaning.categories, slug),
        )
    text = FILLER_PHRASES.sub(" ", text)

    if ranges:
        meaning.date_from = min(start for start, _ in ranges)
        meaning.date_to = max(end for _, end in ranges)
    if len(set(times)) == 1:
        meaning.time = times[0]
    meaning.keywords = clean_words(
        (protected[ord(word) - 0xE000], True) if is_placeholder(word) else (word, False)
        for word in text.split()
    )
    return meaning


def add_once(values, slug):
    """Append ``slug`` unless it is already there; always report a match."""
    if slug not in values:
        values.append(slug)
    return True


def is_placeholder(word):
    """Return whether a word stands for a protected phrase."""
    return len(word) == 1 and 0xE000 <= ord(word) < 0xF000


def full_year(value):
    """Return a four-digit year from "26" or "2026", or None."""
    if not value:
        return None
    return int(value) + 2000 if len(value) == 2 else int(value)


def qualifier(word, past):
    """Normalize the word before a date phrase, e.g. "previous" to "last".

    In the archive a bare "weekend" or "Monday" means the latest one that
    has started, written here as "recent".
    """
    if word in ("last", "past", "previous"):
        return "last"
    if past and word in (None, "the", "on"):
        return "recent"
    if past and word == "this":
        return "this-past"
    return word


def recent_span(count, unit):
    """Return how far back "last 3 days" or "past 2 weeks" reaches."""
    days = int(count) * (7 if unit.startswith("week") else 1)
    return timedelta(days=max(days - 1, 0))


def month_range(month, today, past=False):
    """Return the rest of this month, or all of the next such month.

    With ``past``, return the latest such month up to today instead.
    """
    if past:
        year = today.year if month <= today.month else today.year - 1
    else:
        year = today.year if month >= today.month else today.year + 1
    first = date(year, month, 1)
    last = next_month_start(first) - timedelta(days=1)
    if past:
        return first, min(last, today)
    return max(first, today), last


def next_month_start(day):
    """Return the first day of the month after ``day``."""
    return (day.replace(day=28) + timedelta(days=4)).replace(day=1)


def month_phrase(qualifier, today):
    """Return the days a "this/next/last month" phrase covers.

    "This month" is the rest of it, or its first days up to today in the
    archive; "next month" and "last month" are the whole month.
    """
    first = today.replace(day=1)
    if qualifier == "next":
        return month_range(next_month_start(today).month, today)
    if qualifier == "last":
        last = first - timedelta(days=1)
        return last.replace(day=1), last
    if qualifier in ("recent", "this-past"):
        return first, today
    return preset_range("month", today)


def weekend(qualifier, today):
    """Return the Saturday and Sunday a "weekend" phrase means.

    "This weekend" is the coming one and "next weekend" the one after next
    Monday. "Last weekend" is the latest one that has ended, and in the
    archive a bare "weekend" is the latest one that has started.
    """
    if qualifier in ("last", "recent", "this-past"):
        # Days back to the latest Saturday; on a Saturday or Sunday that
        # weekend has started, but it is only over for "last weekend".
        back = (today.weekday() - 5) % 7
        if qualifier == "last" and back < 2:
            back += 7
        saturday = today - timedelta(days=back)
        return saturday, min(saturday + timedelta(days=1), today)
    saturday, sunday = preset_range("weekend", today)
    if qualifier == "next":
        next_monday = today + timedelta(days=7 - today.weekday())
        saturday = next_monday + timedelta(days=5)
        sunday = saturday + timedelta(days=1)
    return saturday, sunday


def week(qualifier, today):
    """Return the days a "this/next/last week" phrase covers.

    Weeks run Monday to Sunday. "This week" is the rest of it, or its
    first days up to today in the archive.
    """
    monday = today - timedelta(days=today.weekday())
    sunday = monday + timedelta(days=6)
    if qualifier == "next":
        return sunday + timedelta(days=1), sunday + timedelta(days=7)
    if qualifier == "last":
        return monday - timedelta(days=7), monday - timedelta(days=1)
    if qualifier in ("recent", "this-past"):
        return monday, today
    return today, sunday


def weekday(index, qualifier, today):
    """Return the day a weekday name means; today counts as the nearest.

    The next such weekday by default, next week's with "next", and the
    latest one before today with "last" (or up to today in the archive).
    """
    if qualifier == "next":
        next_monday = today + timedelta(days=7 - today.weekday())
        return next_monday + timedelta(days=index)
    if qualifier == "last":
        return today - timedelta(days=(today.weekday() - index - 1) % 7 + 1)
    if qualifier in ("recent", "this-past"):
        return today - timedelta(days=(today.weekday() - index) % 7)
    return today + timedelta(days=(index - today.weekday()) % 7)


def clean_keywords(text):
    """Drop filler words and stray punctuation from keyword text."""
    return clean_words((word, False) for word in text.split())


def clean_words(tokens):
    """Join ``(word, protected)`` tokens into keywords without filler.

    Protected phrases and exclusions stay exactly as typed. "or" stays only
    between two words, where it means either one.
    """
    words = []
    for word, protected in tokens:
        if not protected:
            word = word.strip(PUNCTUATION)
            if word != "or" and (not word or word.lower() in FILLER_WORDS):
                continue
        if word == "or" and (not words or words[-1] == "or"):
            continue
        words.append(word)
    while words and words[-1] == "or":
        words.pop()
    return " ".join(words)[:MAX_KEYWORDS_LENGTH].strip()


def any_word_query(keywords):
    """Return the keywords joined by "or", if they are plain words.

    Used when every word together finds nothing. Searches that already use
    quotes, exclusions, or "or" are left as typed.
    """
    words = keywords.split()
    if len(words) < 2 or PROTECTED.search(keywords) or "or" in words:
        return None
    return " or ".join(words)


def describe_dates(start, end, today):
    """Describe a date range briefly, e.g. "Sat, Nov 14 – Sun, Nov 15"."""

    def day(value):
        text = f"{value:%a, %b} {value.day}"
        return text if value.year == today.year else f"{text}, {value.year}"

    if start and end:
        return day(start) if start == end else f"{day(start)} – {day(end)}"
    if start:
        return f"From {day(start)}"
    return f"Until {day(end)}" if end else ""


def describe_search(query, filters, today=None):
    """Return short descriptions of the keywords and filters being used."""
    today = today or nyc_today()
    terms = [f"“{query}”"] if query else []
    terms += [CATEGORY_LABELS[slug] for slug in filters.get("category") or []]
    terms += [BOROUGH_LABELS[slug] for slug in filters.get("borough") or []]
    dates = describe_dates(filters.get("date_from"), filters.get("date_to"), today)
    if dates:
        terms.append(dates)
    if filters.get("time"):
        terms.append(TIME_LABELS[filters["time"]])
    return terms
