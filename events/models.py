"""Data models for NYC Parks events imported from the city's open data feed."""

from django.contrib.postgres.indexes import GinIndex
from django.db import models

from .search import event_search_vector


class Event(models.Model):
    """One NYC Parks event, copied from the open data feed.

    The feed only publishes a rolling two-week window, so this table is the
    durable copy of the data: rows stay in the database after they disappear
    from the feed and are marked done once their end time has passed.

    ``content_hash`` fingerprints the imported content so an hourly sync can
    skip rows that have not really changed, and ``guid`` is the unique
    identifier the feed uses for each event.
    """

    class Status(models.TextChoices):
        """Lifecycle of an event as tracked by the hourly sync."""

        UPCOMING = "upcoming", "Upcoming"
        DONE = "done", "Done"

    guid = models.CharField("source id", max_length=64, unique=True, db_index=True)
    title = models.CharField("title", max_length=300)
    description = models.TextField("description", blank=True)
    start_time = models.DateTimeField("start time", null=True, blank=True)
    end_time = models.DateTimeField("end time", null=True, blank=True)
    location = models.CharField("location", max_length=300, blank=True)
    park_names = models.CharField("park names", max_length=300, blank=True)
    park_ids = models.CharField("park ids", max_length=100, blank=True)
    categories = models.CharField("categories", max_length=500, blank=True)
    latitude = models.FloatField("latitude", null=True, blank=True)
    longitude = models.FloatField("longitude", null=True, blank=True)
    event_url = models.URLField("event URL", max_length=500, blank=True)
    registration_url = models.URLField("registration URL", max_length=500, blank=True)
    image_url = models.URLField("image URL", max_length=500, blank=True)
    instructor = models.CharField("instructor", max_length=100, blank=True)
    contact_phone = models.CharField("contact phone", max_length=50, blank=True)
    status = models.CharField(
        "status",
        max_length=10,
        choices=Status.choices,
        default=Status.UPCOMING,
        db_index=True,
    )
    content_hash = models.CharField("content hash", max_length=64)
    source_version = models.CharField("source version", max_length=64, blank=True)
    first_seen_at = models.DateTimeField("first seen at", auto_now_add=True)
    last_synced_at = models.DateTimeField("last synced at", null=True, blank=True)
    updated_at = models.DateTimeField("updated at", auto_now=True)

    class Meta:
        """List events chronologically by default."""

        ordering = ["start_time", "end_time", "title"]
        indexes = [GinIndex(event_search_vector(), name="event_search_gin")]

    def __str__(self):
        """Return the event title for admin listings and debug output."""
        return self.title

    @property
    def primary_category(self):
        """Return the first category the feed listed, e.g. "Fitness".

        The sync stores every category in one pipe-separated string, while a
        card tag only has room for the first name.
        """
        return self.categories.split("|")[0].strip()

    @property
    def is_cancelled(self):
        """Return whether the feed marks this event as cancelled.

        ``status`` only records the sync lifecycle (upcoming or done), so the
        feed's own title prefix is the cancellation signal we have. The feed
        spells that prefix both "CANCELLED" and "CANCELED".
        """
        return self.title.lstrip().upper().startswith(("CANCELLED", "CANCELED"))


class EventInterest(models.Model):
    """Record that a user is interested in an event."""

    event = models.ForeignKey(
        Event,
        on_delete=models.CASCADE,
        related_name="interests",
    )
    user = models.ForeignKey(
        "accounts.User",
        on_delete=models.CASCADE,
        related_name="event_interests",
    )
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-created_at"]
        constraints = [
            models.UniqueConstraint(
                fields=["event", "user"],
                name="unique_event_interest",
            ),
        ]

    def __str__(self):
        return f"{self.user} interested in {self.event}"


class ContactRequest(models.Model):
    """A request to contact another user through a shared event."""

    class Status(models.TextChoices):
        PENDING = "pending", "Pending"
        ACCEPTED = "accepted", "Accepted"
        DECLINED = "declined", "Declined"

    event = models.ForeignKey(
        Event,
        on_delete=models.CASCADE,
        related_name="contact_requests",
    )
    sender = models.ForeignKey(
        "accounts.User",
        on_delete=models.CASCADE,
        related_name="sent_contact_requests",
    )
    recipient = models.ForeignKey(
        "accounts.User",
        on_delete=models.CASCADE,
        related_name="received_contact_requests",
    )
    message = models.CharField(max_length=500, blank=True)
    status = models.CharField(
        max_length=10,
        choices=Status.choices,
        default=Status.PENDING,
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-created_at"]
        constraints = [
            models.CheckConstraint(
                condition=~models.Q(sender=models.F("recipient")),
                name="contact_request_not_self",
            ),
        ]

    def __str__(self):
        return f"{self.sender} → {self.recipient} ({self.status})"


class EventPost(models.Model):
    """A discussion post associated with an event."""

    event = models.ForeignKey(
        Event,
        on_delete=models.CASCADE,
        related_name="discussion_posts",
    )
    author = models.ForeignKey(
        "accounts.User",
        on_delete=models.CASCADE,
        related_name="event_posts",
    )
    body = models.TextField(max_length=5000)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-created_at"]

    def __str__(self):
        return f"Post by {self.author} on {self.event}"


class EventReply(models.Model):
    """A reply to a discussion post."""

    post = models.ForeignKey(
        EventPost,
        on_delete=models.CASCADE,
        related_name="replies",
    )
    author = models.ForeignKey(
        "accounts.User",
        on_delete=models.CASCADE,
        related_name="event_replies",
    )
    body = models.TextField(max_length=3000)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["created_at"]

    def __str__(self):
        return f"Reply by {self.author} on post {self.post_id}"
