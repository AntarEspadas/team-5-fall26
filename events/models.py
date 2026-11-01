"""Data models for NYC Parks events imported from the city's open data feed."""

from django.contrib.postgres.indexes import GinIndex
from django.db import models
from django.utils import timezone

from .filters import NYC_TIMEZONE, nyc_today
from .search import event_search_vector


class Event(models.Model):
    """One event, either copied from the NYC Parks feed or posted by a user.

    The feed only publishes a rolling two-week window, so this table is the
    durable copy of the data: rows stay in the database after they disappear
    from the feed and are marked done once their end time has passed.

    ``content_hash`` fingerprints the imported content so an hourly sync can
    skip rows that have not really changed, and ``guid`` is the unique
    identifier the feed uses for each event. Events posted on the platform
    have a ``host`` and a ``guid`` starting with ``HOSTED_GUID_PREFIX``, which
    the feed never uses, so the sync leaves them alone.
    """

    HOSTED_GUID_PREFIX = "host-"

    class Status(models.TextChoices):
        """Lifecycle of an event.

        The hourly sync moves upcoming events to done; hosts can cancel their
        own upcoming events. Only upcoming events are listed or accept
        interest, posts, and contact requests.
        """

        UPCOMING = "upcoming", "Upcoming"
        DONE = "done", "Done"
        CANCELLED = "cancelled", "Cancelled"

    class Borough(models.TextChoices):
        """The five NYC boroughs a hosted event can take place in."""

        MANHATTAN = "manhattan", "Manhattan"
        BROOKLYN = "brooklyn", "Brooklyn"
        QUEENS = "queens", "Queens"
        BRONX = "bronx", "The Bronx"
        STATEN_ISLAND = "staten-island", "Staten Island"

    guid = models.CharField("source id", max_length=64, unique=True, db_index=True)
    host = models.ForeignKey(
        "accounts.User",
        on_delete=models.CASCADE,
        null=True,
        blank=True,
        related_name="hosted_events",
        verbose_name="host",
    )
    borough = models.CharField(
        "borough", max_length=20, choices=Borough.choices, blank=True
    )
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
    def has_ended(self):
        """Return whether the event is over, even before the sync notices.

        Uses the sync's rule: an event ends at its end time, or, without one,
        once the day it starts has passed in New York.
        """
        if self.status == self.Status.DONE:
            return True
        if self.end_time:
            return self.end_time < timezone.now()
        if self.start_time:
            return timezone.localtime(self.start_time, NYC_TIMEZONE).date() < (
                nyc_today()
            )
        return False

    @property
    def is_open(self):
        """Return whether the event is upcoming or under way, not cancelled."""
        return self.status == self.Status.UPCOMING and not self.has_ended

    @property
    def is_cancelled(self):
        """Return whether the host or the feed cancelled this event.

        Hosts cancel through the cancelled status. The feed has no such
        status and instead prefixes the title, spelled both "CANCELLED" and
        "CANCELED".
        """
        if self.status == self.Status.CANCELLED:
            return True
        return self.title.lstrip().upper().startswith(("CANCELLED", "CANCELED"))


class EventInterest(models.Model):
    """Record a user's attendance intention for an event."""

    class Status(models.TextChoices):
        INTERESTED = "interested", "Interested"
        WANT_TO_ATTEND = "want_to_attend", "Want to Attend"

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
    status = models.CharField(
        max_length=20,
        choices=Status.choices,
        default=Status.INTERESTED,
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
        return f"{self.user} — {self.get_status_display()} — {self.event}"


class Notification(models.Model):
    """An event update or incoming contact request.

    Event updates become read when displayed. Contact requests stay unread
    until accepted or declined.
    """

    recipient = models.ForeignKey(
        "accounts.User",
        on_delete=models.CASCADE,
        related_name="notifications",
    )
    event = models.ForeignKey(
        Event,
        on_delete=models.CASCADE,
        related_name="notifications",
    )
    contact_request = models.OneToOneField(
        "ContactRequest",
        on_delete=models.CASCADE,
        null=True,
        blank=True,
        related_name="notification",
    )
    subject = models.CharField("subject", max_length=200)
    body = models.TextField("message")
    created_at = models.DateTimeField(auto_now_add=True)
    read_at = models.DateTimeField("read at", null=True, blank=True)

    class Meta:
        ordering = ["-created_at", "-pk"]

    def __str__(self):
        return f"{self.subject} → {self.recipient}"


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


class Conversation(models.Model):
    """A private message thread shared by a pair of users."""

    user_one = models.ForeignKey(
        "accounts.User",
        on_delete=models.CASCADE,
        related_name="conversations_as_one",
    )
    user_two = models.ForeignKey(
        "accounts.User",
        on_delete=models.CASCADE,
        related_name="conversations_as_two",
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-updated_at", "-pk"]
        constraints = [
            models.UniqueConstraint(
                fields=["user_one", "user_two"],
                name="unique_user_pair_conversation",
            ),
            models.CheckConstraint(
                condition=models.Q(user_one__lt=models.F("user_two")),
                name="conversation_users_in_order",
            ),
        ]

    @classmethod
    def get_or_create_for_users(cls, first_user, second_user):
        """Return the single pair thread with participant IDs in order."""
        if first_user.pk == second_user.pk:
            raise ValueError("A conversation requires two different users.")
        user_one, user_two = sorted((first_user, second_user), key=lambda user: user.pk)
        return cls.objects.get_or_create(user_one=user_one, user_two=user_two)

    def other_participant(self, user):
        """Return the other participant, or None when user is not in thread."""
        if user.pk == self.user_one_id:
            return self.user_two
        if user.pk == self.user_two_id:
            return self.user_one
        return None

    def __str__(self):
        return f"Conversation: {self.user_one} ↔ {self.user_two}"


class DirectMessage(models.Model):
    """A message within a pair-level conversation, optionally about an event."""

    conversation = models.ForeignKey(
        Conversation,
        on_delete=models.CASCADE,
        related_name="messages",
    )
    sender = models.ForeignKey(
        "accounts.User",
        on_delete=models.CASCADE,
        related_name="direct_messages",
    )
    event = models.ForeignKey(
        Event,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="direct_messages",
    )
    body = models.TextField(max_length=5000)
    created_at = models.DateTimeField(auto_now_add=True)
    read_at = models.DateTimeField("read at", null=True, blank=True)

    class Meta:
        ordering = ["created_at", "pk"]

    def __str__(self):
        return f"Message from {self.sender} in conversation {self.conversation_id}"


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
