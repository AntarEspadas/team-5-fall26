"""Tests for searching events with plain-language prompts."""

from datetime import date, datetime
from unittest.mock import patch
from urllib.parse import parse_qsl, urlsplit
from zoneinfo import ZoneInfo

from django.contrib.auth import get_user_model
from django.test import SimpleTestCase, TestCase, override_settings
from django.urls import reverse

from chatbot.gemini import GeminiError
from events.models import Event
from events.natural_search import (
    GEMINI_SCHEMA,
    Interpretation,
    any_word_query,
    describe_dates,
    describe_search,
    interpret,
    parse_prompt,
)

NYC = ZoneInfo("America/New_York")
# A Wednesday; "this weekend" is January 17-18.
TODAY = date(2099, 1, 14)


def parse(prompt):
    return parse_prompt(prompt, TODAY)


def days(first, last=None):
    """Return the date range January ``first``-``last`` 2099."""
    return date(2099, 1, first), date(2099, 1, last or first)


class PromptRulesTests(SimpleTestCase):
    """Built-in rules turn common English phrasing into filters."""

    def test_a_full_prompt_becomes_keywords_and_filters(self):
        """Generic words like "classes" are dropped so every yoga event counts."""
        meaning = parse("Free yoga classes in Brooklyn this weekend")

        self.assertEqual(meaning.keywords, "yoga")
        self.assertEqual((meaning.date_from, meaning.date_to), days(17, 18))
        self.assertEqual(meaning.boroughs, ["brooklyn"])
        self.assertEqual(meaning.categories, [])
        self.assertEqual(meaning.time, "")

    def test_relative_dates(self):
        cases = {
            "today": days(14),
            "tomorrow": days(15),
            "this weekend": days(17, 18),
            "on the weekend": days(17, 18),
            "next weekend": days(24, 25),
            "this week": days(14, 18),
            "next week": days(19, 25),
            "this month": days(14, 31),
            "next month": (date(2099, 2, 1), date(2099, 2, 28)),
            "saturday": days(17),
            "on Saturday": days(17),
            "next saturday": days(24),
            "wednesday": days(14),
            "in March": (date(2099, 3, 1), date(2099, 3, 31)),
            "in january": days(14, 31),
        }
        for prompt, expected in cases.items():
            with self.subTest(prompt):
                meaning = parse(f"picnic {prompt}")

                self.assertEqual((meaning.date_from, meaning.date_to), expected)
                self.assertEqual(meaning.keywords, "picnic")

    def test_explicit_dates(self):
        cases = {
            "Nov 20": date(2099, 11, 20),
            "November 20th": date(2099, 11, 20),
            "20 november": date(2099, 11, 20),
            "the 3rd of Feb": date(2099, 2, 3),
            "1/20": date(2099, 1, 20),
            "1/20/2100": date(2100, 1, 20),
            "1/20/00": date(2000, 1, 20),
            "2099-02-03": date(2099, 2, 3),
            # Dates already past this year mean next year.
            "Jan 10": date(2100, 1, 10),
        }
        for prompt, expected in cases.items():
            with self.subTest(prompt):
                meaning = parse(f"chess {prompt}")

                self.assertEqual((meaning.date_from, meaning.date_to), (expected,) * 2)
                self.assertEqual(meaning.keywords, "chess")

    def test_impossible_dates_stay_keywords(self):
        meaning = parse("chess 2/30")

        self.assertIsNone(meaning.date_from)
        self.assertEqual(meaning.keywords, "chess 2/30")

    def test_several_dates_cover_the_days_between(self):
        between = parse("concerts between Jan 20 and Jan 25")
        either = parse("saturday or sunday")

        self.assertEqual((between.date_from, between.date_to), days(20, 25))
        self.assertEqual(between.keywords, "concerts")
        self.assertEqual((either.date_from, either.date_to), days(17, 18))
        self.assertEqual(either.keywords, "")

    def test_times_of_day(self):
        cases = {
            "tomorrow morning": "morning",
            "in the afternoon": "afternoon",
            "around noon": "afternoon",
            "saturday evening": "evening",
            "friday night": "evening",
            "after work": "evening",
            "at 7pm": "evening",
            "after 5 PM": "evening",
            "10:30am": "morning",
            "2pm": "afternoon",
            "12am": "morning",
        }
        for prompt, expected in cases.items():
            with self.subTest(prompt):
                meaning = parse(f"tennis {prompt}")

                self.assertEqual(meaning.time, expected)
                self.assertEqual(meaning.keywords, "tennis")

    def test_tonight_means_this_evening(self):
        meaning = parse("movies tonight")

        self.assertEqual((meaning.date_from, meaning.date_to), days(14))
        self.assertEqual(meaning.time, "evening")

    def test_different_times_of_day_cancel_out(self):
        self.assertEqual(parse("yoga morning or evening").time, "")

    def test_boroughs(self):
        meaning = parse("Manhattan, queens, the Bronx or Staten Island")

        self.assertEqual(
            meaning.boroughs, ["manhattan", "queens", "bronx", "staten-island"]
        )
        self.assertEqual(meaning.keywords, "")

    def test_manhattan_beach_is_a_park_not_a_borough(self):
        meaning = parse("Manhattan Beach cleanup")

        self.assertEqual(meaning.boroughs, [])
        self.assertEqual(meaning.keywords, "manhattan beach cleanup")

    def test_audiences_and_broad_kinds_become_categories(self):
        cases = {
            "kid-friendly": "kids",
            "for children": "kids",
            "family": "kids",
            "toddlers": "kids",
            "older adults": "seniors",
            "wheelchair accessible": "seniors",
            "workouts": "fitness",
            "sports": "sports",
            "arts and crafts": "art",
            "music": "music-film",
            "nature": "nature",
            "historic": "history",
            "educational": "education",
        }
        for prompt, expected in cases.items():
            with self.subTest(prompt):
                meaning = parse(prompt)

                self.assertEqual(meaning.categories, [expected])
                self.assertEqual(meaning.keywords, "")

    def test_specific_activities_stay_keywords(self):
        self.assertEqual(parse("martial arts").categories, [])
        self.assertEqual(parse("martial arts").keywords, "martial arts")
        self.assertEqual(parse("yoga concert").categories, [])

    def test_filler_and_punctuation_are_dropped(self):
        meaning = parse("Show me some fun things to do near me in NYC, please!")

        self.assertEqual(meaning, Interpretation())

    def test_search_syntax_is_kept_as_typed(self):
        self.assertEqual(parse("yoga -kids").keywords, "yoga -kids")
        self.assertEqual(parse("yoga -kids").categories, [])
        self.assertEqual(
            parse("“Central Park” or Prospect").keywords,
            '"Central Park" or prospect',
        )
        self.assertEqual(parse('"tai chi').keywords, '"tai chi')

    def test_or_only_stays_between_two_words(self):
        self.assertEqual(parse("or yoga or or dance or").keywords, "yoga or dance")


class InterpretationTests(SimpleTestCase):
    """Interpretations merge into chosen filters and describe themselves."""

    def test_changes_compares_keywords_and_filters(self):
        self.assertFalse(Interpretation(keywords="yoga").changes("Yoga"))
        self.assertTrue(Interpretation(keywords="yoga").changes("yoga!"))
        self.assertTrue(Interpretation(boroughs=["queens"]).changes(""))

    def test_prompt_dates_and_time_replace_chosen_ones_and_places_add_up(self):
        chosen = {
            "date_from": date(2099, 1, 1),
            "date_to": date(2099, 1, 2),
            "time": "morning",
            "borough": ["queens"],
            "category": ["kids"],
        }
        meaning = Interpretation(
            date_from=date(2099, 1, 17),
            time="evening",
            boroughs=["brooklyn", "queens"],
            categories=["kids"],
        )

        self.assertEqual(
            meaning.merged_with(chosen),
            {
                "date_from": date(2099, 1, 17),
                "date_to": None,
                "time": "evening",
                "borough": ["queens", "brooklyn"],
                "category": ["kids"],
            },
        )

    def test_prompt_without_dates_keeps_the_chosen_ones(self):
        chosen = {"date_from": date(2099, 1, 1), "time": "morning"}

        merged = Interpretation(keywords="yoga").merged_with(chosen)

        self.assertEqual(merged["date_from"], date(2099, 1, 1))
        self.assertEqual(merged["time"], "morning")

    def test_any_word_query_only_rewrites_plain_words(self):
        self.assertEqual(any_word_query("yoga dance"), "yoga or dance")
        for keywords in ("yoga", "yoga or dance", "yoga -kids", '"tai chi" park'):
            with self.subTest(keywords):
                self.assertIsNone(any_word_query(keywords))

    def test_date_descriptions(self):
        self.assertEqual(describe_dates(*days(17), TODAY), "Sat, Jan 17")
        self.assertEqual(
            describe_dates(*days(17, 18), TODAY), "Sat, Jan 17 – Sun, Jan 18"
        )
        self.assertEqual(describe_dates(days(17)[0], None, TODAY), "From Sat, Jan 17")
        self.assertEqual(
            describe_dates(None, date(2100, 1, 2), TODAY), "Until Sat, Jan 2, 2100"
        )
        self.assertEqual(describe_dates(None, None, TODAY), "")

    def test_search_description_lists_every_part(self):
        filters = {
            "date_from": date(2099, 1, 17),
            "date_to": date(2099, 1, 18),
            "time": "evening",
            "borough": ["brooklyn"],
            "category": ["kids"],
        }

        self.assertEqual(
            describe_search("yoga", filters, TODAY),
            [
                "“yoga”",
                "Kids & Family",
                "Brooklyn",
                "Sat, Jan 17 – Sun, Jan 18",
                "Evening",
            ],
        )
        self.assertEqual(describe_search("", {}, TODAY), [])


@override_settings(CHATBOT_ENABLED=True)
@patch("chatbot.gemini.generate_json")
class GeminiInterpretationTests(SimpleTestCase):
    """With Gemini configured, it reads longer prompts; rules are the fallback."""

    def test_gemini_reads_longer_prompts(self, generate_json):
        generate_json.return_value = {
            "keywords": "Events: yoga, pilates",
            "boroughs": ["brooklyn", "atlantis", "brooklyn"],
            "categories": ["kids"],
            "date_from": "2099-01-18",
            "date_to": "2099-01-17",
            "time_of_day": "morning",
        }

        meaning = interpret("yoga para niños en Brooklyn este fin de semana", TODAY)

        self.assertEqual(
            meaning,
            Interpretation(
                keywords="yoga pilates",
                date_from=date(2099, 1, 17),
                date_to=date(2099, 1, 18),
                time="morning",
                boroughs=["brooklyn"],
                categories=["kids"],
            ),
        )
        (parts, schema), _ = generate_json.call_args
        self.assertIs(schema, GEMINI_SCHEMA)
        instructions = parts[0]["text"]
        self.assertIn("Today is Wednesday, January 14, 2099", instructions)
        self.assertIn("this weekend is 2099-01-17 to 2099-01-18", instructions)
        self.assertIn(
            '"""yoga para niños en Brooklyn este fin de semana"""', instructions
        )

    def test_unknown_values_from_gemini_are_dropped(self, generate_json):
        generate_json.return_value = {
            "keywords": None,
            "boroughs": "brooklyn",
            "categories": ["kids", "space"],
            "date_from": "soon",
            "date_to": "",
            "time_of_day": "any",
        }

        meaning = interpret("something fun for the kids", TODAY)

        self.assertEqual(meaning, Interpretation(categories=["kids"]))

    def test_categories_that_repeat_a_keyword_are_dropped(self, generate_json):
        """Searching "yoga" already finds Yoga & Pilates listings and more."""
        generate_json.return_value = {
            "keywords": "Yoga",
            "boroughs": [],
            "categories": ["yoga", "kids"],
            "date_from": "",
            "date_to": "",
            "time_of_day": "any",
        }

        meaning = interpret("yoga for my kids please", TODAY)

        self.assertEqual(meaning, Interpretation(keywords="Yoga", categories=["kids"]))

    def test_short_prompts_and_search_syntax_skip_gemini(self, generate_json):
        for prompt in (
            "yoga",
            "Brooklyn yoga",
            '"tai chi" in queens',
            "yoga -kids today",
        ):
            with self.subTest(prompt):
                interpret(prompt, TODAY)

        generate_json.assert_not_called()

    def test_rules_take_over_when_gemini_fails(self, generate_json):
        for failure in (GeminiError("overloaded"), None):
            with self.subTest(failure=failure):
                if failure:
                    generate_json.side_effect = failure
                else:
                    generate_json.side_effect = None
                    generate_json.return_value = ["not", "an", "object"]

                with self.assertLogs("events.natural_search", "WARNING"):
                    meaning = interpret("yoga in Brooklyn this weekend", TODAY)

                self.assertEqual(meaning.keywords, "yoga")
                self.assertEqual(meaning.boroughs, ["brooklyn"])

    @override_settings(CHATBOT_ENABLED=False)
    def test_rules_are_used_without_gemini(self, generate_json):
        meaning = interpret("  yoga   in Brooklyn this weekend ", TODAY)

        generate_json.assert_not_called()
        self.assertEqual(meaning.keywords, "yoga")


def make_event(guid, title, start, **fields):
    fields.setdefault("content_hash", "n" * 64)
    return Event.objects.create(guid=guid, title=title, start_time=start, **fields)


@override_settings(CHATBOT_ENABLED=False)
@patch("events.natural_search.nyc_today", return_value=TODAY)
class NaturalLanguageSearchPageTests(TestCase):
    """The search box reads plain language and shows how it read it."""

    def setUp(self):
        self.brooklyn_yoga = make_event(
            "host-yoga",
            "Sunrise Yoga",
            datetime(2099, 1, 17, 9, tzinfo=NYC),
            borough=Event.Borough.BROOKLYN,
        )
        self.queens_kids = make_event(
            "feed-kids",
            "Puppet Show",
            datetime(2099, 1, 17, 18, tzinfo=NYC),
            park_ids="Q099",
            categories="Best for Kids",
        )
        self.manhattan_concert = make_event(
            "feed-concert",
            "Jazz Concert",
            datetime(2099, 1, 20, 19, tzinfo=NYC),
            park_ids="M010",
            categories="Concerts",
        )

    def ask(self, prompt, **params):
        """Submit the search box the way the page does."""
        params.setdefault("nl", "")
        return self.client.get(reverse("search"), {"q": prompt, **params})

    def redirect_params(self, response):
        self.assertEqual(response.status_code, 302)
        url = urlsplit(response["Location"])
        self.assertEqual(url.path, reverse("search"))
        return parse_qsl(url.query)

    def test_prompt_redirects_to_its_keywords_and_filters(self, today):
        response = self.ask("yoga in Brooklyn this weekend")

        self.assertEqual(
            self.redirect_params(response),
            [
                ("q", "yoga"),
                ("date_from", "2099-01-17"),
                ("date_to", "2099-01-18"),
                ("borough", "brooklyn"),
                ("asked", "yoga in Brooklyn this weekend"),
            ],
        )

    def test_results_page_shows_how_the_search_was_read(self, today):
        response = self.client.get(
            self.ask("yoga in Brooklyn this weekend")["Location"]
        )

        self.assertEqual(list(response.context["events"]), [self.brooklyn_yoga])
        self.assertContains(
            response, "Searching for <q>yoga in Brooklyn this weekend</q> as"
        )
        for term in ("“yoga”", "Brooklyn", "Sat, Jan 17 – Sun, Jan 18"):
            self.assertContains(response, f"<li>{term}</li>")
        self.assertContains(response, 'href="?q=yoga%20in%20Brooklyn%20this%20weekend"')
        # The filter bar shows the filters, so they can be changed.
        self.assertContains(response, 'value="brooklyn" id="filter-borough_1" checked')
        self.assertContains(response, 'name="date_from" value="2099-01-17"')

    def test_filters_alone_can_answer_a_prompt(self, today):
        response = self.client.get(
            self.ask("kid friendly things to do on Saturday evening")["Location"]
        )

        self.assertEqual(list(response.context["events"]), [self.queens_kids])
        self.assertEqual(response.context["query"], "")
        self.assertEqual(
            response.context["understood"], ["Kids & Family", "Sat, Jan 17", "Evening"]
        )

    def test_prompt_adds_to_filters_already_chosen(self, today):
        response = self.ask("concerts in Manhattan", borough="queens", time="evening")

        self.assertEqual(
            self.redirect_params(response),
            [
                ("q", "concerts"),
                ("time", "evening"),
                ("borough", "queens"),
                ("borough", "manhattan"),
                ("asked", "concerts in Manhattan"),
            ],
        )

    def test_any_word_matches_when_all_words_find_nothing(self, today):
        response = self.ask("yoga puppet show this weekend")

        self.assertIn(("q", "yoga or puppet or show"), self.redirect_params(response))

    def test_all_words_are_kept_when_they_match_together(self, today):
        response = self.ask("puppet show this weekend")

        self.assertIn(("q", "puppet show"), self.redirect_params(response))

    def test_open_ended_prompt_lists_everything_coming_up(self, today):
        response = self.ask("what's happening?")

        self.assertEqual(
            self.redirect_params(response),
            [("date_from", "2099-01-14"), ("asked", "what's happening?")],
        )
        page = self.client.get(response["Location"])
        self.assertEqual(len(page.context["events"]), 3)

    def test_plain_keywords_search_without_a_redirect(self, today):
        response = self.ask("yoga")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(list(response.context["events"]), [self.brooklyn_yoga])
        self.assertNotContains(response, "Searching for")

    def test_links_and_unchanged_words_stay_exact_keyword_searches(self, today):
        """Only new words typed into the search box are read as a prompt."""
        prompt = "yoga in Brooklyn this weekend"
        link = self.client.get(reverse("search"), {"q": prompt})
        refiltered = self.ask(prompt, nl=prompt, borough="queens")

        for response in (link, refiltered):
            self.assertEqual(response.status_code, 200)
            self.assertNotContains(response, "Searching for")
        self.assertEqual(list(link.context["events"]), [])

    def test_summary_needs_something_to_describe(self, today):
        response = self.client.get(reverse("search"), {"asked": "anything"})

        self.assertNotContains(response, "Searching for")

    def test_search_boxes_offer_plain_language_for_upcoming_events(self, today):
        landing = self.client.get(reverse("landing"))
        results = self.client.get(reverse("search"), {"q": "yoga"})

        self.assertContains(landing, '<input type="hidden" name="nl" value="">')
        self.assertContains(landing, "Try &ldquo;kids events in Brooklyn this weekend")
        self.assertContains(results, '<input type="hidden" name="nl" value="yoga">')

    def test_archive_search_stays_a_keyword_search(self, today):
        get_user_model().objects.create_user(username="reader", password="pass12345")
        self.client.login(username="reader", password="pass12345")

        response = self.client.get(reverse("archived_events"))

        self.assertNotContains(response, 'name="nl"')
        self.assertContains(response, "Search by activity, park, or location")
