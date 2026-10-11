"""Tests for filtering events by date range, borough, and category."""

from datetime import date, datetime
from unittest.mock import patch
from zoneinfo import ZoneInfo

from django.test import SimpleTestCase, TestCase
from django.urls import reverse

from events.filters import (
    CATEGORY_GROUPS,
    EventFilterForm,
    filter_events,
    filter_params,
    nyc_today,
    preset_range,
)
from events.models import Event
from events.views import SEARCH_PAGE_SIZE

NYC = ZoneInfo("America/New_York")


def make_event(guid, **fields):
    """Create an upcoming event with optional filterable fields."""
    fields.setdefault("title", "Community gathering")
    fields.setdefault("content_hash", "f" * 64)
    return Event.objects.create(guid=guid, **fields)


def at(day, hour=9):
    """Return an NYC-local datetime on ``day`` of January 2099."""
    return datetime(2099, 1, day, hour, tzinfo=NYC)


class PresetRangeTests(SimpleTestCase):
    """Quick date buttons turn into inclusive date ranges."""

    def test_today_is_a_single_day(self):
        """ "Today" starts and ends on the current date."""
        today = date(2026, 10, 14)
        self.assertEqual(preset_range("today", today), (today, today))

    def test_weekend_before_and_during_the_weekend(self):
        """The weekend is the coming Saturday and Sunday, minus days gone by."""
        cases = {
            date(2026, 10, 12): (date(2026, 10, 17), date(2026, 10, 18)),
            date(2026, 10, 16): (date(2026, 10, 17), date(2026, 10, 18)),
            date(2026, 10, 17): (date(2026, 10, 17), date(2026, 10, 18)),
            date(2026, 10, 18): (date(2026, 10, 18), date(2026, 10, 18)),
        }
        for today, expected in cases.items():
            with self.subTest(today=today):
                self.assertEqual(preset_range("weekend", today), expected)

    def test_month_runs_to_the_last_day(self):
        """ "This month" ends on the month's last day, including in December."""
        cases = {
            date(2026, 10, 10): date(2026, 10, 31),
            date(2028, 2, 3): date(2028, 2, 29),
            date(2026, 12, 31): date(2026, 12, 31),
        }
        for today, end in cases.items():
            with self.subTest(today=today):
                self.assertEqual(preset_range("month", today), (today, end))

    def test_today_uses_new_york_time(self):
        """Late evening in New York is still "today" even after UTC midnight."""
        utc_after_midnight = datetime(2026, 10, 11, 2, 30, tzinfo=ZoneInfo("UTC"))
        with patch("events.filters.timezone.now", return_value=utc_after_midnight):
            self.assertEqual(nyc_today(), date(2026, 10, 10))


class EventFilterFormTests(SimpleTestCase):
    """The filter form validates input and normalises quick date buttons."""

    def test_quick_dates_replace_typed_dates(self):
        """A quick date button wins over stale dates already in the inputs."""
        form = EventFilterForm(
            {"when": "today", "date_from": "2026-01-01", "date_to": "2026-01-02"}
        )
        with patch("events.filters.nyc_today", return_value=date(2026, 10, 10)):
            self.assertTrue(form.is_valid())

        self.assertEqual(
            filter_params(form.cleaned_data),
            [("date_from", "2026-10-10"), ("date_to", "2026-10-10")],
        )

    def test_reversed_dates_are_rejected(self):
        """An end date before the start date reports a readable error."""
        form = EventFilterForm({"date_from": "2026-10-20", "date_to": "2026-10-10"})

        self.assertFalse(form.is_valid())
        self.assertIn("on or after the start", form.errors["date_to"][0])

    def test_unknown_choices_are_rejected(self):
        """Only listed boroughs, categories, and quick dates are accepted."""
        form = EventFilterForm(
            {"borough": ["atlantis"], "category": ["fitness"], "when": "decade"}
        )

        self.assertFalse(form.is_valid())
        self.assertEqual(set(form.errors), {"borough", "when"})
        self.assertEqual(form.cleaned_data["category"], ["fitness"])

    def test_params_keep_selection_order_and_skip_empty_filters(self):
        """Only active filters become query-string pairs."""
        form = EventFilterForm(
            {"borough": ["queens", "brooklyn"], "category": ["yoga"]}
        )
        self.assertTrue(form.is_valid())

        self.assertEqual(
            filter_params(form.cleaned_data),
            [("borough", "queens"), ("borough", "brooklyn"), ("category", "yoga")],
        )


class FilterEventsTests(TestCase):
    """Filters narrow querysets and combine with each other."""

    def filtered(self, **data):
        """Return the events matching the submitted filter data."""
        form = EventFilterForm(data)
        self.assertTrue(form.is_valid(), form.errors)
        return filter_events(Event.objects.all(), form.cleaned_data)

    def test_date_range_keeps_events_overlapping_the_chosen_days(self):
        """Events starting, ending, or running through the range all match."""
        before = make_event("before", start_time=at(9), end_time=at(9, 10))
        inside = make_event("inside", start_time=at(10), end_time=at(10, 10))
        ongoing = make_event("ongoing", start_time=at(8), end_time=at(12))
        open_ended = make_event("open-ended", start_time=at(11))
        late = make_event("late", start_time=at(11, 23), end_time=at(12, 1))
        after = make_event("after", start_time=at(12), end_time=at(12, 10))
        undated = make_event("undated")

        matches = self.filtered(date_from="2099-01-10", date_to="2099-01-11")

        self.assertCountEqual(matches, [inside, ongoing, open_ended, late])
        for event in (before, after, undated):
            self.assertNotIn(event, matches)

    def test_one_sided_date_ranges(self):
        """Only a start date or only an end date leaves the other side open."""
        early = make_event("early", start_time=at(5), end_time=at(5, 10))
        later = make_event("later", start_time=at(20), end_time=at(20, 10))

        self.assertEqual(list(self.filtered(date_from="2099-01-10")), [later])
        self.assertEqual(list(self.filtered(date_to="2099-01-10")), [early])

    def test_boroughs_come_from_park_id_prefixes(self):
        """Park IDs decide the borough, case-insensitively, and any may match."""
        brooklyn = make_event("bk", park_ids="B073")
        manhattan = make_event("mn", park_ids="m391")
        make_event("si", park_ids="R062")
        make_event("no-park")

        self.assertEqual(list(self.filtered(borough=["brooklyn"])), [brooklyn])
        self.assertCountEqual(
            self.filtered(borough=["brooklyn", "manhattan"]), [brooklyn, manhattan]
        )

    def test_categories_match_whole_feed_names_only(self):
        """Group names map to exact feed categories wherever they appear."""
        only = make_event("only", categories="Yoga & Pilates Classes")
        first = make_event("first", categories="Yoga & Pilates Classes | Fitness")
        middle = make_event("middle", categories="Art | Yoga & Pilates Classes | Games")
        last = make_event("last", categories="Games | Yoga & Pilates Classes")
        make_event("partial", categories="Chair Yoga & Pilates Classes Online")
        make_event("other", categories="Fitness | Games")

        self.assertCountEqual(
            self.filtered(category=["yoga"]), [only, first, middle, last]
        )

    def test_category_groups_include_related_feed_names(self):
        """Choosing "Arts & Crafts" finds every art-related feed category."""
        painting = make_event("painting", categories="Arts & Crafts | Games")
        theater = make_event("theater", categories="Theater")
        make_event("party", categories="Party Time")

        self.assertCountEqual(self.filtered(category=["art"]), [painting, theater])

    def test_every_category_group_has_feed_names(self):
        """No category button can silently match nothing by construction."""
        for slug, label, names in CATEGORY_GROUPS:
            with self.subTest(slug=slug):
                self.assertTrue(label)
                self.assertTrue(names)

    def test_different_filters_must_all_match(self):
        """Dates, boroughs, and categories combine with AND."""
        match = make_event(
            "match",
            park_ids="B001",
            categories="Fitness",
            start_time=at(10),
            end_time=at(10, 10),
        )
        make_event("wrong-borough", park_ids="Q001", categories="Fitness")
        make_event("wrong-category", park_ids="B002", categories="Games")
        make_event(
            "wrong-day",
            park_ids="B003",
            categories="Fitness",
            start_time=at(20),
            end_time=at(20, 10),
        )

        matches = self.filtered(
            date_from="2099-01-10",
            date_to="2099-01-10",
            borough=["brooklyn"],
            category=["fitness"],
        )

        self.assertEqual(list(matches), [match])


class FilteredSearchViewTests(TestCase):
    """Visitors apply filters from the search box on the home and search pages."""

    @classmethod
    def setUpTestData(cls):
        cls.brooklyn_yoga = make_event(
            "bk-yoga",
            title="Morning Yoga",
            park_ids="B073",
            categories="Yoga & Pilates Classes | Fitness",
            start_time=at(15),
            end_time=at(15, 10),
        )
        cls.queens_art = make_event(
            "qn-art",
            title="Watercolor Workshop",
            park_ids="Q001",
            categories="Arts & Crafts",
            start_time=at(16),
            end_time=at(16, 11),
        )
        cls.brooklyn_art = make_event(
            "bk-art",
            title="Mural Painting",
            park_ids="B010",
            categories="Art",
            start_time=at(14),
            end_time=at(14, 11),
        )
        cls.done = make_event(
            "done",
            title="Past Brooklyn Yoga",
            park_ids="B073",
            categories="Yoga & Pilates Classes",
            status=Event.Status.DONE,
        )

    def get(self, **params):
        """Request the search page with the given query parameters."""
        return self.client.get(reverse("search"), params)

    def test_home_page_shows_the_filter_bar_under_the_search_box(self):
        """Every visitor sees date, borough, and category filters on the home page."""
        response = self.client.get(reverse("landing"))
        content = response.content.decode()

        self.assertLess(content.index('name="q"'), content.index('class="filter-bar"'))
        self.assertContains(response, 'name="when" value="weekend"')
        self.assertContains(response, 'name="when" value="month"')
        self.assertContains(response, 'type="date" name="date_from"')
        self.assertContains(response, 'type="date" name="date_to"')
        self.assertContains(response, 'name="borough" value="brooklyn"')
        self.assertContains(response, 'name="category" value="yoga"')
        self.assertNotContains(response, "Clear filters")

    def test_filters_alone_list_upcoming_matches_soonest_first(self):
        """No search term is needed; done events never appear."""
        response = self.get(borough="brooklyn")

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Filtered upcoming events")
        self.assertEqual(
            list(response.context["events"]), [self.brooklyn_art, self.brooklyn_yoga]
        )
        self.assertNotContains(response, self.done.title)

    def test_filters_combine_with_a_search_term(self):
        """Search results are narrowed by every selected filter."""
        response = self.get(q="painting OR yoga", borough="brooklyn", category="art")

        self.assertEqual(list(response.context["events"]), [self.brooklyn_art])

    def test_multiple_choices_and_dates_all_apply(self):
        """Several categories match any of them, within the chosen dates."""
        response = self.client.get(
            reverse("search"),
            {
                "category": ["yoga", "art"],
                "date_from": "2099-01-15",
                "date_to": "2099-01-16",
            },
        )

        self.assertEqual(
            list(response.context["events"]), [self.brooklyn_yoga, self.queens_art]
        )

    def test_selected_filters_stay_filled_in(self):
        """The form shows the active dates, boroughs, and categories again."""
        response = self.client.get(
            reverse("search"),
            {
                "q": "yoga",
                "date_from": "2099-01-01",
                "borough": ["brooklyn", "queens"],
                "category": "yoga",
            },
        )

        self.assertContains(response, 'name="date_from" value="2099-01-01"')
        self.assertContains(response, 'value="brooklyn" id="filter-borough_1" checked')
        self.assertContains(response, 'value="queens" id="filter-borough_2" checked')
        self.assertContains(response, 'value="yoga" id="filter-category_1" checked')
        self.assertNotContains(
            response, 'value="manhattan" id="filter-borough_0" checked'
        )

    def test_quick_date_buttons_fill_in_their_dates(self):
        """Choosing "This weekend" shows the matching dates in the calendar inputs."""
        with patch("events.filters.nyc_today", return_value=date(2099, 1, 12)):
            response = self.get(when="weekend")

        self.assertContains(response, 'name="date_from" value="2099-01-17"')
        self.assertContains(response, 'name="date_to" value="2099-01-18"')
        self.assertTrue(response.context["has_filters"])

    def test_pagination_keeps_the_search_term_and_filters(self):
        """Later pages use the same filters, so they stay filled in."""
        Event.objects.bulk_create(
            [
                Event(
                    guid=f"page-{index}",
                    title="Yoga Flow",
                    park_ids="B100",
                    categories="Yoga & Pilates Classes",
                    content_hash="p" * 64,
                )
                for index in range(SEARCH_PAGE_SIZE + 1)
            ]
        )
        params = {"q": "yoga flow", "borough": "brooklyn", "category": "yoga"}
        expected = "q=yoga%20flow&amp;borough=brooklyn&amp;category=yoga"

        first = self.client.get(reverse("search"), params)
        second = self.client.get(reverse("search"), {**params, "page": 2})

        self.assertContains(first, f'href="?{expected}&amp;page=2"')
        self.assertContains(second, f'href="?{expected}&amp;page=1"')
        self.assertContains(second, 'value="brooklyn" id="filter-borough_1" checked')
        self.assertEqual(len(second.context["events"]), 1)

    def test_clearing_filters_returns_to_the_unfiltered_list(self):
        """Clear keeps the search term, or goes back to the home page's list."""
        with_query = self.get(q="yoga flow", borough="brooklyn")
        without_query = self.get(borough="brooklyn")

        self.assertContains(with_query, 'href="?q=yoga%20flow">Clear filters</a>')
        self.assertContains(without_query, 'href="/">Clear filters</a>')

        cleared = self.get(q="yoga")
        self.assertEqual(list(cleared.context["events"]), [self.brooklyn_yoga])
        self.assertNotContains(cleared, "Clear filters")

    def test_no_matches_suggests_clearing_filters(self):
        """An empty filtered result explains how to widen the search."""
        response = self.get(borough="staten-island")

        self.assertContains(response, "No upcoming events match your search.")
        self.assertContains(response, "or clear some filters")

    def test_invalid_filters_show_errors_and_are_ignored(self):
        """Bad dates or choices never break the page or hide every event."""
        response = self.get(
            q="yoga", date_from="2099-01-20", date_to="2099-01-01", borough="atlantis"
        )

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "The end date must be on or after the start.")
        self.assertEqual(list(response.context["events"]), [self.brooklyn_yoga])

    def test_empty_submission_still_asks_for_input(self):
        """Submitting a blank search box and no filters lists nothing."""
        response = self.get(q="", date_from="", date_to="")

        self.assertContains(response, "Enter a search term or choose a filter")
        self.assertEqual(list(response.context["events"]), [])
