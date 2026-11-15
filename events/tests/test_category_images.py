"""Tests for the category illustrations shown on events without a photo."""

from datetime import datetime
from zoneinfo import ZoneInfo

from django.contrib.auth import get_user_model
from django.contrib.staticfiles import finders
from django.test import SimpleTestCase, TestCase
from django.urls import reverse

from events.filters import CATEGORY_GROUPS
from events.models import CATEGORY_IMAGE_NAMES, UNILLUSTRATED_GROUPS, Event

NYC = ZoneInfo("America/New_York")


def image_for(categories):
    """Return the illustration an event with these categories would get."""
    return Event(categories=categories).category_image


class CategoryImageTests(SimpleTestCase):
    """Each event picks the illustration of its category group."""

    def test_every_illustration_has_an_image_file(self):
        """No category can point at a missing file."""
        groups = [slug for slug, _, _ in CATEGORY_GROUPS]
        groups = [slug for slug in groups if slug not in UNILLUSTRATED_GROUPS]
        for image in [*groups, *CATEGORY_IMAGE_NAMES.values(), "park"]:
            with self.subTest(image=image):
                self.assertIsNotNone(
                    finders.find(f"events/categories/{image}.webp"), image
                )

    def test_some_categories_have_their_own_illustration(self):
        """Swimming and theater get their own pictures, not their group's."""
        self.assertEqual(
            image_for("Swimming/Aquatics"), "events/categories/swimming.webp"
        )
        self.assertEqual(image_for("Theater"), "events/categories/theater.webp")
        self.assertEqual(image_for("Art"), "events/categories/art.webp")

    def test_audience_groups_defer_to_the_activity(self):
        """A seniors fitness class shows fitness; seniors has no picture."""
        self.assertEqual(
            image_for("Fitness | Shape Up NYC | Seniors | Exercise Classes"),
            "events/categories/fitness.webp",
        )
        self.assertEqual(image_for("Seniors|Art"), "events/categories/art.webp")
        self.assertEqual(
            image_for("Seniors & Accessible"), "events/categories/park.webp"
        )

    def test_feed_category_names_map_to_their_group(self):
        """The feed's own names, like "Best for Kids", use their group's image."""
        self.assertEqual(image_for("Best for Kids"), "events/categories/kids.webp")
        self.assertEqual(image_for("Birding"), "events/categories/nature.webp")
        self.assertEqual(
            image_for("Free Summer Movies"), "events/categories/music-film.webp"
        )

    def test_hosted_event_labels_map_to_their_group(self):
        """Hosted events store group labels, separated by " | "."""
        self.assertEqual(
            image_for("Arts & Crafts | Games"), "events/categories/art.webp"
        )
        self.assertEqual(
            image_for("Nature & Outdoors | Kids & Family"),
            "events/categories/nature.webp",
        )

    def test_first_category_with_a_group_wins(self):
        """Categories outside every group are skipped in favor of later ones."""
        self.assertEqual(image_for("Dance|Art"), "events/categories/dance.webp")
        self.assertEqual(
            image_for("Unlisted Things|Volunteer"),
            "events/categories/volunteer.webp",
        )

    def test_fitness_gives_way_to_a_more_specific_group(self):
        """A yoga class tagged "Fitness" first still gets the yoga picture."""
        self.assertEqual(
            image_for("Fitness|Exercise Classes|Yoga & Pilates Classes"),
            "events/categories/yoga.webp",
        )
        self.assertEqual(
            image_for("Fitness|Unlisted Things"), "events/categories/fitness.webp"
        )

    def test_events_without_a_known_category_get_the_park_scene(self):
        """No categories, or none that fit a group, fall back to a park scene."""
        self.assertEqual(image_for(""), "events/categories/park.webp")
        self.assertEqual(image_for("Unlisted Things"), "events/categories/park.webp")


class CategoryImagePageTests(TestCase):
    """Cards and event pages show the illustration instead of a grey box."""

    @classmethod
    def setUpTestData(cls):
        """Create a member, an event without a photo, and one with a photo."""
        cls.user = get_user_model().objects.create_user(
            username="viewer", email="viewer@example.com", password="pass12345"
        )
        common = {
            "content_hash": "c" * 64,
            "start_time": datetime(2099, 5, 2, 10, 0, tzinfo=NYC),
        }
        cls.plain = Event.objects.create(
            guid="plain", title="Bird walk", categories="Birding", **common
        )
        cls.photo = Event.objects.create(
            guid="photo",
            title="Bird talk",
            categories="Talks",
            image_url="https://example.com/bird.jpg",
            **common,
        )

    def test_cards_show_the_category_illustration(self):
        """A card without a photo shows its category's illustration."""
        self.client.force_login(self.user)
        response = self.client.get(reverse("search"), {"q": "bird"})
        self.assertContains(response, 'src="/static/events/categories/nature.webp"')
        self.assertContains(response, 'src="https://example.com/bird.jpg"')
        self.assertContains(
            response, 'data-placeholder="/static/events/categories/education.webp"'
        )

    def test_event_page_shows_the_category_illustration(self):
        """The event page uses the illustration as a decorative banner."""
        response = self.client.get(reverse("event_detail", args=[self.plain.pk]))
        self.assertContains(response, 'class="detail-image is-illustration"')
        self.assertContains(response, 'src="/static/events/categories/nature.webp"')

    def test_broken_photos_fall_back_to_the_illustration(self):
        """A photo that fails to load is swapped for the category's picture."""
        response = self.client.get(reverse("event_detail", args=[self.photo.pk]))
        self.assertContains(response, 'alt="Image for Bird talk"')
        self.assertContains(
            response, 'data-placeholder="/static/events/categories/education.webp"'
        )
