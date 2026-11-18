"""Test private favorites, My Events, and favorite write permissions."""

from datetime import timedelta

from django.db import IntegrityError, transaction
from django.test import Client, TestCase
from django.urls import reverse
from django.utils import timezone

from accounts.models import User, UserBan
from events.models import (
    Event,
    EventInterest,
    FavoriteEvent,
    Notification,
    Registration,
)


class FavoriteEventTests(TestCase):
    """A favorite is private, persistent, and separate from attendance."""

    @classmethod
    def setUpTestData(cls):
        """Create two accounts and two distinct events."""
        cls.member = User.objects.create_user("favoriteuser", "fav@example.com")
        cls.peer = User.objects.create_user("favoritepeer", "peer@example.com")
        cls.event = Event.objects.create(
            guid="favorite-walk",
            title="Morning walking group",
            start_time=timezone.now() + timedelta(days=5),
            host=cls.peer,
        )
        cls.other = Event.objects.create(
            guid="favorite-art",
            title="Afternoon art class",
            start_time=timezone.now() + timedelta(days=6),
        )

    def setUp(self):
        """Sign in as the member and prepare the favorite page URLs."""
        self.client.force_login(self.member)
        self.action_url = reverse("event_favorite", args=[self.event.pk])
        self.detail_url = reverse("event_detail", args=[self.event.pk])
        self.list_url = reverse("my_events") + "?tab=favorites"

    def save_favorite(self):
        """Save the first event as the signed-in member."""
        return self.client.post(self.action_url, {"action": "save"})

    def test_save_is_private_and_does_not_register_or_notify(self):
        """Saving twice leaves one favorite and no attendance or notification."""
        self.assertContains(self.client.get(self.detail_url), "Save to favorites")
        self.assertRedirects(self.save_favorite(), self.detail_url)
        self.assertRedirects(self.save_favorite(), self.detail_url)
        favorite = FavoriteEvent.objects.get(user=self.member, event=self.event)
        self.assertIn(" saved ", str(favorite))
        self.assertEqual(FavoriteEvent.objects.count(), 1)
        self.assertFalse(EventInterest.objects.exists())
        self.assertFalse(Registration.objects.exists())
        self.assertFalse(Notification.objects.exists())
        self.assertContains(self.client.get(self.detail_url), "Remove from favorites")

    def test_favorites_tab_only_shows_current_users_favorites(self):
        """Favorites from a second account must not appear in My Events."""
        self.save_favorite()
        FavoriteEvent.objects.create(user=self.peer, event=self.other)
        response = self.client.get(self.list_url)
        self.assertContains(response, 'id="favorites-tab"')
        self.assertContains(response, "Favorite Events")
        self.assertContains(response, self.event.title)
        self.assertNotContains(response, self.other.title)
        self.assertTrue(response.context["favorites_tab_active"])
        self.assertEqual(
            [favorite.event for favorite in response.context["favorite_events"]],
            [self.event],
        )
        self.client.force_login(self.peer)
        response = self.client.get(self.list_url)
        self.assertContains(response, self.other.title)
        detail = self.client.get(self.detail_url)
        self.assertFalse(detail.context["is_favorite"])

    def test_remove_only_affects_current_user_and_returns_to_tab(self):
        """Repeated removals are safe, including from the Favorites tab."""
        self.save_favorite()
        FavoriteEvent.objects.create(user=self.peer, event=self.event)
        for _ in range(2):
            response = self.client.post(
                self.action_url,
                {"action": "remove", "source": "favorites", "user": self.peer.pk},
            )
            self.assertRedirects(response, self.list_url)
        self.assertFalse(FavoriteEvent.objects.filter(user=self.member).exists())
        self.assertTrue(FavoriteEvent.objects.filter(user=self.peer).exists())
        self.assertContains(
            self.client.get(self.list_url), "You have not saved any favorites yet."
        )

    def test_cancelled_and_ended_favorites_remain_removable(self):
        """Keep saved events visible and labeled after their status changes."""
        self.save_favorite()
        self.event.status = Event.Status.CANCELLED
        self.event.save(update_fields=["status"])
        self.assertContains(self.client.get(self.list_url), "Event cancelled")
        self.assertContains(self.client.get(self.detail_url), "Remove from favorites")
        self.client.post(self.action_url, {"action": "remove"})
        self.save_favorite()
        self.assertFalse(FavoriteEvent.objects.exists())

        FavoriteEvent.objects.create(user=self.member, event=self.event)
        self.event.status = Event.Status.DONE
        self.event.save(update_fields=["status"])
        self.assertContains(self.client.get(self.list_url), "Event ended")
        self.client.post(self.action_url, {"action": "remove"})
        self.assertFalse(FavoriteEvent.objects.exists())

    def test_expired_and_feed_cancelled_events_cannot_be_newly_saved(self):
        """A stale upcoming status must not allow a new unavailable favorite."""
        self.event.start_time = timezone.now() - timedelta(days=2)
        self.event.save(update_fields=["start_time"])
        self.save_favorite()
        self.assertFalse(FavoriteEvent.objects.exists())
        self.event.title = "CANCELED Walking group"
        self.event.start_time = timezone.now() + timedelta(days=2)
        self.event.save(update_fields=["start_time", "title"])
        self.save_favorite()
        self.assertFalse(FavoriteEvent.objects.exists())

    def test_login_post_and_valid_action_are_required(self):
        """Guests and GET requests cannot change favorites."""
        self.assertEqual(self.client.get(self.action_url).status_code, 405)
        self.client.post(self.action_url, {"action": "unknown"})
        self.assertFalse(FavoriteEvent.objects.exists())
        self.client.logout()
        self.assertContains(self.client.get(self.detail_url), "Log in to save")
        self.assertEqual(self.save_favorite().status_code, 302)
        self.assertEqual(self.client.get(self.list_url).status_code, 302)
        self.assertFalse(FavoriteEvent.objects.exists())

    def test_post_requires_csrf_token(self):
        """A forged browser submission is rejected."""
        client = Client(enforce_csrf_checks=True)
        client.force_login(self.member)
        response = client.post(self.action_url, {"action": "save"})
        self.assertEqual(response.status_code, 403)
        self.assertFalse(FavoriteEvent.objects.exists())

    def test_ban_keeps_favorites_readable_but_blocks_writes(self):
        """The existing ban policy applies to the new feature."""
        self.save_favorite()
        UserBan.objects.create(
            user=self.member,
            issued_by=self.peer,
            offense_number=1,
            expires_at=timezone.now() + timedelta(days=2),
        )
        page = self.client.get(self.list_url)
        self.assertContains(page, self.event.title)
        self.assertNotContains(page, "Remove from favorites")
        self.assertNotContains(
            self.client.get(self.detail_url), "Remove from favorites"
        )
        response = self.client.post(self.action_url, {"action": "remove"})
        self.assertEqual(response.status_code, 403)
        self.assertTrue(FavoriteEvent.objects.filter(user=self.member).exists())

    def test_database_uniqueness_and_cascade(self):
        """Prevent duplicates at the database level and clean up deleted events."""
        self.save_favorite()
        with self.assertRaises(IntegrityError), transaction.atomic():
            FavoriteEvent.objects.create(user=self.member, event=self.event)
        self.event.delete()
        self.assertFalse(FavoriteEvent.objects.exists())
