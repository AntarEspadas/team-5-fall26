"""Privacy, matching, and page tests for event-interest discovery (#54)."""

from datetime import timedelta

from django.test import SimpleTestCase, TestCase
from django.utils import timezone
from django.urls import reverse

from accounts.forms import AccountSettingsForm
from accounts.models import User, UserBan
from events.models import (
    Event,
    EventInterest,
    Registration,
)
from events.similar_interests import (
    category_query,
    category_tags,
    category_terms,
    find_similar_interest_users,
    shared_interest_label,
)


def make_event(guid, categories, status=Event.Status.UPCOMING, **fields):
    """Create an event with public category labels."""
    return Event.objects.create(
        guid=guid,
        title=fields.pop("title", guid),
        categories=categories,
        status=status,
        content_hash="e" * 64,
        **fields,
    )


def make_user(username, *, sharing=True, similar=True, interests=("sports",)):
    """Create a member with independently controlled visibility settings."""
    return User.objects.create_user(
        username=username,
        email=f"{username}@example.com",
        password="not-a-real-password",
        display_name=username.capitalize(),
        share_profile_with_attendees=sharing,
        discoverable_for_similar_events=similar,
        interests=list(interests),
    )


class CategoryMatchingTests(SimpleTestCase):
    """Only genuine, human-readable event categories match."""

    def test_category_tags_ignore_empty_segments(self):
        self.assertEqual(
            category_tags(" Sports | | Basketball/Netball "),
            ["Sports", "Basketball/Netball"],
        )
        self.assertEqual(category_tags(""), [])

    def test_terms_expand_using_existing_groups(self):
        terms = category_terms(["Basketball/Netball"])
        self.assertIn("Sports", terms)
        self.assertIn("Football", terms)
        self.assertIn("Basketball/Netball", terms)
        self.assertNotIn("Gardening", terms)

    def test_exact_and_related_interest_labels(self):
        source = ["Sports", "Basketball/Netball"]
        self.assertEqual(
            shared_interest_label(source, "Basketball/Netball"),
            "Basketball/Netball",
        )
        self.assertEqual(shared_interest_label(source, "Football"), "Sports")
        self.assertIsNone(shared_interest_label(source, "Gardening"))

    def test_unlisted_categories_can_still_match_exactly(self):
        self.assertEqual(
            shared_interest_label(["Community Chess"], "community chess"),
            "Community Chess",
        )

    def test_category_query_uses_complete_pipe_separated_labels(self):
        self.assertIsNotNone(category_query("event__categories", "Art"))


class SimilarInterestDiscoveryTests(TestCase):
    """Only consenting users with related upcoming events are suggested."""

    @classmethod
    def setUpTestData(cls):
        cls.event = make_event("basketball", "Sports | Basketball/Netball")
        cls.other_sports = make_event("running", "Football")
        cls.same_sport = make_event("basketball-other", "Basketball/Netball")
        cls.other_topic = make_event("gardening", "Gardening")
        cls.viewer = make_user("viewer")
        cls.runner = make_user("runner")
        EventInterest.objects.create(user=cls.runner, event=cls.other_sports)

    def suggestions(self, event=None, viewer=None, limit=6):
        """Fetch suggested profiles and their shared labels."""
        return find_similar_interest_users(
            event or self.event,
            viewer=viewer or self.viewer,
            limit=limit,
        )

    def test_related_event_category_discovers_different_event_attendee(self):
        suggestions = self.suggestions()
        self.assertEqual(len(suggestions), 1)
        self.assertEqual(suggestions[0]["user"], self.runner)
        self.assertEqual(suggestions[0]["shared_interest"], "Sports")

    def test_exact_category_match_uses_specific_interest_label(self):
        person = make_user("ballplayer")
        EventInterest.objects.create(user=person, event=self.same_sport)
        by_user = {item["user"].pk: item for item in self.suggestions()}
        self.assertEqual(
            by_user[person.pk]["shared_interest"],
            "Basketball/Netball",
        )

    def test_unrelated_categories_do_not_match(self):
        person = make_user("gardener")
        EventInterest.objects.create(user=person, event=self.other_topic)
        self.assertNotIn(person, [item["user"] for item in self.suggestions()])

    def test_no_partial_word_category_match(self):
        self.viewer.interests = ["arts"]
        self.viewer.save(update_fields=["interests"])
        art = make_event("art", "Art")
        martial = make_event("martial", "Martial Arts")
        person = make_user("martialmember", interests=("arts",))
        EventInterest.objects.create(user=person, event=martial)
        self.assertEqual(self.suggestions(event=art), [])

    def test_removing_saved_interest_updates_event_and_profile_suggestions(self):
        """Saving My Interests removes a match without changing event attendance."""
        self.client.force_login(self.viewer)
        detail_url = reverse("event_detail", args=[self.event.pk])
        profile_url = reverse("public_profile", args=[self.runner.username])
        EventInterest.objects.create(user=self.viewer, event=self.event)

        self.assertContains(self.client.get(detail_url), self.runner.username)
        self.assertContains(
            self.client.get(profile_url, {"from_event": self.event.pk}),
            "Related event interest:",
        )

        form = AccountSettingsForm(
            {
                "interests": ["nature"],
                "share_profile_with_attendees": "on",
                "discoverable_for_similar_events": "on",
            },
            instance=self.runner,
        )
        self.assertTrue(form.is_valid(), form.errors)
        form.save()
        self.runner.refresh_from_db()
        self.assertEqual(self.runner.interests, ["nature"])

        self.assertEqual(self.suggestions(), [])
        self.assertNotContains(self.client.get(detail_url), self.runner.username)
        self.assertNotContains(
            self.client.get(profile_url, {"from_event": self.event.pk}),
            "Related event interest:",
        )

        self.runner.interests = ["sports"]
        self.runner.save(update_fields=["interests"])
        self.assertEqual([item["user"] for item in self.suggestions()], [self.runner])

    def test_viewer_changing_my_interests_updates_discovery(self):
        """A suggestion also goes away when the browsing user removes the match."""
        self.assertEqual([item["user"] for item in self.suggestions()], [self.runner])
        self.viewer.interests = ["nature"]
        self.viewer.save(update_fields=["interests"])
        self.assertEqual(self.suggestions(), [])

        self.viewer.interests = ["sports"]
        self.viewer.save(update_fields=["interests"])
        self.assertEqual([item["user"] for item in self.suggestions()], [self.runner])

    def test_shared_preference_must_describe_the_relevant_event_categories(self):
        """An unrelated shared checkbox cannot justify a sports recommendation."""
        self.viewer.interests = ["nature"]
        self.viewer.save(update_fields=["interests"])
        self.runner.interests = ["nature"]
        self.runner.save(update_fields=["interests"])
        self.assertEqual(self.suggestions(), [])

    def test_unrelated_other_preference_is_not_a_wildcard(self):
        """The broad Other preference must not expose unrelated profiles."""
        self.viewer.interests = ["other"]
        self.viewer.save(update_fields=["interests"])
        self.runner.interests = ["other"]
        self.runner.save(update_fields=["interests"])
        self.assertEqual(self.suggestions(), [])

    def test_matching_nature_preference_finds_nature_events(self):
        """A separate selected topic can produce a real match."""
        self.viewer.interests = ["nature"]
        self.viewer.save(update_fields=["interests"])
        self.runner.interests = ["nature"]
        self.runner.save(update_fields=["interests"])
        source = make_event("nature-source", "Nature")
        related = make_event("nature-related", "Hiking")
        EventInterest.objects.create(user=self.runner, event=related)
        self.assertEqual(
            [item["user"] for item in self.suggestions(event=source)],
            [self.runner],
        )

    def test_hidden_or_not_separately_opted_in_profiles_are_excluded(self):
        no_profile = make_user("noprofile", sharing=False, similar=True)
        no_similar = make_user("nosimilar", sharing=True, similar=False)
        for user in (no_profile, no_similar):
            EventInterest.objects.create(event=self.other_sports, user=user)
        users = [item["user"] for item in self.suggestions()]
        self.assertEqual(users, [self.runner])

    def test_unchecked_discovery_defaults_to_off(self):
        member = User.objects.create_user(
            username="private", email="private@example.com"
        )
        self.assertFalse(member.discoverable_for_similar_events)
        member.share_profile_with_attendees = True
        member.save()
        EventInterest.objects.create(event=self.other_sports, user=member)
        self.assertNotIn(member, [item["user"] for item in self.suggestions()])

    def test_same_event_attendees_and_registrants_are_not_repeated(self):
        same_interest = make_user("sameinterest")
        same_registration = make_user("sameregistration")
        EventInterest.objects.create(event=self.event, user=same_interest)
        Registration.objects.create(event=self.event, user=same_registration)
        for user in (same_interest, same_registration):
            EventInterest.objects.create(event=self.other_sports, user=user)
        self.assertEqual([i["user"] for i in self.suggestions()], [self.runner])

    def test_current_viewer_is_not_suggested(self):
        EventInterest.objects.create(event=self.other_sports, user=self.viewer)
        self.assertEqual([i["user"] for i in self.suggestions()], [self.runner])

    def test_user_is_only_suggested_once_even_with_multiple_events(self):
        EventInterest.objects.create(event=self.same_sport, user=self.runner)
        self.assertEqual([i["user"] for i in self.suggestions()], [self.runner])

    def test_ended_cancelled_and_inactive_users_are_excluded(self):
        ended = make_event("ended-sports", "Sports", Event.Status.DONE)
        cancelled = make_event("cancelled-sports", "Sports", Event.Status.CANCELLED)
        for event, name in (
            (ended, "endedmember"),
            (cancelled, "cancelledmember"),
        ):
            EventInterest.objects.create(event=event, user=make_user(name))
        inactive = make_user("inactive")
        inactive.is_active = False
        inactive.save()
        EventInterest.objects.create(event=self.other_sports, user=inactive)
        self.assertEqual([i["user"] for i in self.suggestions()], [self.runner])

    def test_empty_categories_closed_events_and_zero_limit_return_nothing(self):
        self.assertEqual(self.suggestions(event=make_event("blank", "")), [])
        self.assertEqual(
            self.suggestions(event=make_event("closed", "Sports", Event.Status.DONE)),
            [],
        )
        self.assertEqual(self.suggestions(limit=0), [])

    def test_result_limit_is_applied(self):
        EventInterest.objects.create(
            event=self.other_sports,
            user=make_user("another"),
        )
        self.assertEqual(len(self.suggestions(limit=1)), 1)

    def test_profile_settings_can_enable_and_disable_suggestions(self):
        member = make_user("settingsmember", sharing=False, similar=False)
        form = AccountSettingsForm(
            {
                "share_profile_with_attendees": "on",
                "discoverable_for_similar_events": "on",
            },
            instance=member,
        )
        self.assertTrue(form.is_valid(), form.errors)
        form.save()
        member.refresh_from_db()
        self.assertTrue(member.share_profile_with_attendees)
        self.assertTrue(member.discoverable_for_similar_events)

        form = AccountSettingsForm({}, instance=member)
        self.assertTrue(form.is_valid(), form.errors)
        form.save()
        member.refresh_from_db()
        self.assertFalse(member.discoverable_for_similar_events)

    def test_event_page_displays_shared_category_and_public_profile_link(self):
        self.client.force_login(self.viewer)
        response = self.client.get(reverse("event_detail", args=[self.event.pk]))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "People with Similar Interests")
        self.assertContains(response, "Related event category: Sports")
        self.assertNotContains(
            response,
            reverse("message_user", args=[self.runner.pk]),
        )
        self.assertContains(
            response,
            reverse("public_profile", args=[self.runner.username]),
        )
        self.assertNotContains(response, self.runner.email)
        self.assertEqual(
            [item["user"] for item in response.context["similar_interest_users"]],
            [self.runner],
        )

    def test_private_profile_is_not_visible_in_event_page(self):
        private = make_user("privateperson", similar=False)
        EventInterest.objects.create(event=self.other_sports, user=private)
        self.client.force_login(self.viewer)
        response = self.client.get(reverse("event_detail", args=[self.event.pk]))
        self.assertNotContains(response, private.username)
        self.assertNotContains(response, private.email)

    def test_anonymous_visitors_do_not_see_similar_profile_discovery(self):
        response = self.client.get(reverse("event_detail", args=[self.event.pk]))
        self.assertNotContains(response, "People with Similar Interests")
        self.assertNotContains(response, self.runner.username)

    def test_event_page_does_not_suggest_on_cancelled_event(self):
        self.event.status = Event.Status.CANCELLED
        self.event.save()
        self.client.force_login(self.viewer)
        response = self.client.get(reverse("event_detail", args=[self.event.pk]))
        self.assertNotContains(response, "People with Similar Interests")

    def test_public_profile_setting_is_visible_in_account_settings(self):
        self.client.force_login(self.viewer)
        response = self.client.get(reverse("account_settings"))
        self.assertContains(response, 'name="discoverable_for_similar_events"')
        self.assertContains(response, "Show my public profile for similar events")

    def test_stale_upcoming_event_is_not_suggested(self):
        """An outdated status is not enough to suggest a past event."""
        self.other_sports.start_time = timezone.now() - timedelta(days=2)
        self.other_sports.save(update_fields=["start_time"])
        self.assertEqual(self.suggestions(), [])

    def test_cancelled_feed_event_is_not_suggested(self):
        """Imported cancellations in the event title remain hidden."""
        self.other_sports.title = "  CANCELED Football practice"
        self.other_sports.save(update_fields=["title"])
        self.assertEqual(self.suggestions(), [])

    def test_stale_or_cancelled_source_event_hides_similar_section(self):
        """Related discovery only appears on events still happening."""
        self.event.title = "CANCELLED: basketball"
        self.event.save(update_fields=["title"])
        self.client.force_login(self.viewer)
        response = self.client.get(reverse("event_detail", args=[self.event.pk]))
        self.assertNotContains(response, "People with Similar Interests")

        self.event.title = "basketball"
        self.event.start_time = timezone.now() - timedelta(days=2)
        self.event.save(update_fields=["title", "start_time"])
        response = self.client.get(reverse("event_detail", args=[self.event.pk]))
        self.assertNotContains(response, "People with Similar Interests")

    def test_profile_link_shows_related_interest_without_exposing_event(self):
        """A recommended profile explains an interest the viewer really follows."""
        EventInterest.objects.create(event=self.event, user=self.viewer)
        self.client.force_login(self.viewer)
        url = reverse("public_profile", args=[self.runner.username])
        response = self.client.get(url, {"from_event": self.event.pk})
        self.assertContains(response, "Related event interest:")
        self.assertContains(response, "Sports")
        self.assertNotContains(response, self.runner.email)
        self.assertNotContains(response, self.other_sports.title)

    def test_profile_referral_respects_opt_out_and_invalid_ids(self):
        """A forged event id or revoked consent does not reveal a match."""
        self.client.force_login(self.viewer)
        url = reverse("public_profile", args=[self.runner.username])
        for ref in ("abc", "-3", "999999999999999999999999", "0"):
            with self.subTest(ref=ref):
                response = self.client.get(url, {"from_event": ref})
                self.assertEqual(response.status_code, 200)
                self.assertNotContains(response, "Related event interest:")

        EventInterest.objects.create(event=self.event, user=self.viewer)
        visible = self.client.get(url, {"from_event": self.event.pk})
        self.assertContains(visible, "Related event interest:")

        self.runner.discoverable_for_similar_events = False
        self.runner.save(update_fields=["discoverable_for_similar_events"])
        response = self.client.get(url, {"from_event": self.event.pk})
        self.assertNotContains(response, "Related event interest:")

    def test_banned_members_not_exposed_and_lifted_bans_restore_discovery(self):
        """Hide actively banned users, but honor expiration and lifting."""
        member_ban = UserBan.objects.create(
            user=self.runner,
            issued_by=self.viewer,
            offense_number=1,
            expires_at=timezone.now() + timedelta(days=5),
        )
        self.assertEqual(self.suggestions(), [])
        self.client.force_login(self.viewer)
        response = self.client.get(reverse("event_detail", args=[self.event.pk]))
        self.assertNotContains(response, self.runner.username)

        member_ban.lifted_at = timezone.now()
        member_ban.save(update_fields=["lifted_at"])
        self.assertEqual([i["user"] for i in self.suggestions()], [self.runner])
