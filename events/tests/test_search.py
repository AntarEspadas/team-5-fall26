"""PostgreSQL integration tests for public event search and its cards."""

from datetime import datetime
from unittest.mock import patch
from urllib.parse import quote
from zoneinfo import ZoneInfo

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.urls import reverse
from django.utils.html import escape

from events.models import Event
from events.search import search_events
from events.services import sync_events
from events.views import SEARCH_PAGE_SIZE

NYC = ZoneInfo("America/New_York")


def make_event(guid, **fields):
    """Create an event with optional discovery fields."""
    fields.setdefault("title", "Community gathering")
    fields.setdefault("content_hash", "s" * 64)
    return Event.objects.create(guid=guid, **fields)


class EventSearchTests(TestCase):
    """Search uses stemming, weighted ranking, and live database contents."""

    def search(self, query):
        """Run the full-text query against all events in this test."""
        return search_events(Event.objects.all(), query)

    def test_all_discovery_fields_support_case_insensitive_stemming(self):
        """Inflected words match in every field included in the document."""
        events = [
            make_event(field, **{field: "Walking"})
            for field in (
                "title",
                "categories",
                "park_names",
                "location",
                "description",
            )
        ]
        make_event("unrelated", title="Swimming")

        self.assertCountEqual(self.search("WALKS"), events)

    def test_title_matches_rank_above_categories_and_descriptions(self):
        """Identical terms favor the title, then discovery metadata."""
        description = make_event("description", description="Yoga")
        category = make_event("category", categories="Yoga")
        title = make_event("title", title="Yoga")

        self.assertEqual(list(self.search("yoga")), [title, category, description])

    def test_multiple_terms_can_match_across_fields(self):
        """All ordinary keywords must match, even across different fields."""
        match = make_event("prospect", title="Yoga", location="Prospect Park")
        make_event("central", title="Yoga", location="Central Park")

        self.assertEqual(list(self.search("yoga prospect")), [match])

    def test_websearch_supports_phrases_or_and_exclusions(self):
        """Visitors can quote phrases, combine alternatives, or exclude words."""
        morning = make_event("morning", title="Morning Yoga")
        kids = make_event("kids", title="Evening Yoga", description="Kids welcome")
        birds = make_event("birds", title="Bird Watching")

        self.assertEqual(list(self.search('"morning yoga"')), [morning])
        self.assertCountEqual(self.search("yoga OR bird"), [morning, kids, birds])
        self.assertEqual(list(self.search("yoga -kids")), [morning])

    def test_equal_ranks_sort_by_date_then_id_with_undated_events_last(self):
        """Pagination has deterministic ordering when relevance is equal."""
        undated = make_event("undated", title="Yoga")
        later = make_event(
            "later", title="Yoga", start_time=datetime(2099, 1, 16, 9, tzinfo=NYC)
        )
        first = make_event(
            "first", title="Yoga", start_time=datetime(2099, 1, 15, 9, tzinfo=NYC)
        )
        second = make_event("second", title="Yoga", start_time=first.start_time)

        self.assertEqual(list(self.search("yoga")), [first, second, later, undated])

    def test_imported_events_and_feed_updates_are_searchable_immediately(self):
        """The sync's bulk inserts and saves maintain the search document."""
        with patch(
            "events.services.fetch_feed",
            return_value=[{"guid": "imported", "title": "Outdoor Chess"}],
        ):
            sync_events()
        event = Event.objects.get(guid="imported")
        self.assertEqual(list(self.search("chess")), [event])

        with patch(
            "events.services.fetch_feed",
            return_value=[{"guid": "imported", "title": "Outdoor Painting"}],
        ):
            sync_events()
        event.refresh_from_db()

        self.assertFalse(self.search("chess").exists())
        self.assertEqual(list(self.search("painting")), [event])

    def test_queryset_updates_do_not_leave_stale_search_matches(self):
        """Database updates also work without model save hooks or signals."""
        event = make_event("updated", title="Pickleball")
        self.assertEqual(list(self.search("pickleball")), [event])

        Event.objects.filter(pk=event.pk).update(title="Tennis")
        event.refresh_from_db()

        self.assertFalse(self.search("pickleball").exists())
        self.assertEqual(list(self.search("tennis")), [event])


class SearchViewTests(TestCase):
    """Any visitor can submit a query and browse locally linked event cards."""

    @classmethod
    def setUpTestData(cls):
        cls.user = get_user_model().objects.create_user(username="searcher")
        cls.yoga = make_event(
            "yoga",
            title="Morning Yoga",
            description="Stretch and breathe in the park.",
            categories="Fitness | Yoga",
            location="Prospect Park",
            start_time=datetime(2099, 1, 15, 9, tzinfo=NYC),
            end_time=datetime(2099, 1, 15, 10, tzinfo=NYC),
            image_url="https://example.org/yoga.png",
            event_url="https://example.org/yoga",
            registration_url="https://example.org/register",
        )
        cls.done = make_event("done", title="Past Yoga", status=Event.Status.DONE)

    def test_search_is_public_and_only_lists_upcoming_matches(self):
        """Anonymous requests render results directly at the requested URL."""
        self.assertEqual(reverse("search"), "/search")
        response = self.client.get(reverse("search"), {"q": "yoga"})

        self.assertEqual(response.status_code, 200)
        self.assertTemplateUsed(response, "events/search.html")
        self.assertEqual(list(response.context["events"]), [self.yoga])
        self.assertNotContains(response, self.done.title)

    def test_signed_in_visitors_receive_the_same_results(self):
        """Signing in does not change which upcoming events match."""
        anonymous = self.client.get(reverse("search"), {"q": "yoga"})
        self.client.force_login(self.user)
        signed_in = self.client.get(reverse("search"), {"q": "yoga"})

        self.assertEqual(signed_in.status_code, 200)
        self.assertEqual(
            list(signed_in.context["events"]), list(anonymous.context["events"])
        )

    def test_landing_search_form_is_available_to_all_visitors(self):
        """Search is available while the existing landing access stays intact."""
        for signed_in in (False, True):
            with self.subTest(signed_in=signed_in):
                if signed_in:
                    self.client.force_login(self.user)
                response = self.client.get(reverse("landing"))

                self.assertContains(response, 'role="search" method="get"')
                self.assertContains(response, 'action="/search"')
                self.assertContains(response, 'type="search" name="q"')
                self.assertContains(response, '<button type="submit">Search</button>')
                if not signed_in:
                    self.assertContains(response, "Please log in")
                    self.assertNotContains(response, self.yoga.title)

    def test_missing_or_whitespace_queries_show_a_prompt(self):
        """Opening search without keywords never lists every event."""
        for parameters in ({}, {"q": ""}, {"q": " \t "}):
            with self.subTest(parameters=parameters):
                response = self.client.get(reverse("search"), parameters)

                self.assertEqual(response.status_code, 200)
                self.assertContains(response, "Enter a search term")
                self.assertEqual(list(response.context["events"]), [])
                self.assertNotContains(response, self.yoga.title)

    def test_nonmatching_punctuation_and_stopword_queries_are_safe(self):
        """Empty PostgreSQL queries and unmatched words render an empty state."""
        for query in ("astronomy", "!!!", "the and or"):
            with self.subTest(query=query):
                response = self.client.get(reverse("search"), {"q": query})

                self.assertEqual(response.status_code, 200)
                self.assertContains(response, "No upcoming events match your search.")
                self.assertEqual(list(response.context["events"]), [])

    def test_query_is_trimmed_and_unclosed_quotes_are_tolerated(self):
        """User input does not need to be valid raw tsquery syntax."""
        response = self.client.get(reverse("search"), {"q": '  "yoga  '})

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context["query"], '"yoga')
        self.assertContains(response, 'value="&quot;yoga"')
        self.assertEqual(list(response.context["events"]), [self.yoga])

    def test_queries_and_event_content_are_html_escaped(self):
        """Untrusted input remains text in headings, inputs, and cards."""
        query = '<script>alert("x")</script>'
        response = self.client.get(reverse("search"), {"q": query})
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, escape(query))
        self.assertNotContains(response, query)

        title = f"Yoga {query}"
        make_event("escaped", title=title, description="<b>Unsafe markup</b>")
        response = self.client.get(reverse("search"), {"q": "yoga"})
        self.assertContains(response, escape(title))
        self.assertContains(response, "&lt;b&gt;Unsafe markup&lt;/b&gt;")
        self.assertNotContains(response, query)

    def test_cards_link_to_local_detail_urls_and_keep_metadata(self):
        """The entire result card has one local link and NYC-local details."""
        response = self.client.get(reverse("search"), {"q": "yoga"})

        self.assertContains(
            response, f'<a class="event" href="/events/{self.yoga.pk}/">'
        )
        self.assertContains(response, "Jan 15")
        self.assertContains(response, "9:00 AM")
        self.assertContains(response, "10:00 AM")
        self.assertContains(response, "Prospect Park")
        self.assertContains(response, "Fitness | Yoga")
        self.assertContains(response, "Stretch and breathe in the park.")
        self.assertContains(response, 'src="https://example.org/yoga.png"')
        self.assertNotContains(response, f'href="{self.yoga.event_url}"')
        self.assertNotContains(response, f'href="{self.yoga.registration_url}"')
        self.assertRedirects(
            self.client.get(f"/events/{self.yoga.pk}"),
            f"/events/{self.yoga.pk}/",
            status_code=301,
        )

    def test_undated_cards_use_park_names_and_placeholder_images(self):
        """Optional event fields have the same fallbacks as landing cards."""
        make_event("undated", title="Yoga", park_names="Central Park")
        response = self.client.get(reverse("search"), {"q": "yoga"})

        self.assertContains(response, "Date to be announced")
        self.assertContains(response, "Central Park")
        self.assertContains(response, 'src="/static/events/no-image.svg"')
        self.assertContains(response, 'data-placeholder="/static/events/no-image.svg"')

    def test_pagination_preserves_encoded_queries_and_does_not_drop_matches(self):
        """Visitors can reach matches beyond the first page with reserved text."""
        Event.objects.bulk_create(
            [
                Event(
                    guid=f"page-{index}",
                    title="Yoga",
                    location="Central Park",
                    content_hash="p" * 64,
                )
                for index in range(SEARCH_PAGE_SIZE + 1)
            ]
        )
        query = 'yoga & "Central Park"'
        first = self.client.get(reverse("search"), {"q": query})
        second = self.client.get(reverse("search"), {"q": query, "page": 2})

        self.assertEqual(len(first.context["events"]), SEARCH_PAGE_SIZE)
        self.assertEqual(len(second.context["events"]), 1)
        self.assertEqual(first.context["paginator"].count, SEARCH_PAGE_SIZE + 1)
        self.assertContains(first, f'href="?q={quote(query)}&amp;page=2"')
        self.assertContains(second, f'href="?q={quote(query)}&amp;page=1"')
        first_ids = {event.pk for event in first.context["events"]}
        second_ids = {event.pk for event in second.context["events"]}
        self.assertFalse(first_ids & second_ids)
        self.assertEqual(len(first_ids | second_ids), SEARCH_PAGE_SIZE + 1)

    def test_invalid_result_pages_return_404(self):
        """Invalid or out-of-range page numbers do not produce server errors."""
        for page in ("invalid", "0", "999"):
            with self.subTest(page=page):
                response = self.client.get(
                    reverse("search"), {"q": "yoga", "page": page}
                )
                self.assertEqual(response.status_code, 404)
