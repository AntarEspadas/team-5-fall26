"""Reports of content that may need moderation."""

from django.conf import settings
from django.db import models


class Report(models.Model):
    """A report with live target references and a shared resolution lifecycle.

    A null resolved_at means the report remains open. Source deletion clears
    references without deleting the report. Chat content is never copied.
    """

    class Kind(models.TextChoices):
        EVENT = "event", "Event"
        POST = "post", "Comment"
        REPLY = "reply", "Reply"
        USER = "user", "User"
        PROFILE = "profile", "Profile"
        RATING = "rating", "Host rating"

    class Reason(models.TextChoices):
        SPAM_SCAM = "spam_scam", "Spam or scam"
        MISLEADING_INFORMATION = "misleading_information", "Misleading information"
        HARASSMENT_HATE = "harassment_hate", "Harassment or hate"
        INAPPROPRIATE_CONTENT = "inappropriate_content", "Inappropriate content"
        SAFETY_CONCERN = "safety_concern", "Safety concern"
        OTHER = "other", "Other"

    kind = models.CharField(max_length=16, choices=Kind.choices, default=Kind.EVENT)
    reporter = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="reports",
    )
    event = models.ForeignKey(
        "events.Event",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="reports",
    )
    reported_post = models.ForeignKey(
        "events.EventPost",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="reports",
    )
    reported_reply = models.ForeignKey(
        "events.EventReply",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="reports",
    )
    reported_rating = models.ForeignKey(
        "events.HostRating",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="reports",
    )
    reported_user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="received_reports",
    )
    conversation = models.ForeignKey(
        "events.Conversation",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="reports",
    )
    context_message = models.ForeignKey(
        "events.DirectMessage",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="reports",
    )
    event_title = models.CharField(
        max_length=300, editable=False, blank=True, default=""
    )
    reason = models.CharField(max_length=32, choices=Reason.choices)
    details = models.TextField(max_length=5000, blank=True)
    created_at = models.DateTimeField(auto_now_add=True, db_index=True)
    resolved_at = models.DateTimeField(null=True, blank=True)
    resolved_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="resolved_reports",
    )

    class Meta:
        ordering = ["-created_at", "-pk"]
        constraints = [
            models.UniqueConstraint(
                fields=["reporter", "event"],
                condition=models.Q(kind="event", resolved_at__isnull=True),
                name="unique_unresolved_event_report",
            ),
            models.UniqueConstraint(
                fields=["reporter", "reported_post"],
                condition=models.Q(kind="post", resolved_at__isnull=True),
                name="unique_unresolved_post_report",
            ),
            models.UniqueConstraint(
                fields=["reporter", "reported_reply"],
                condition=models.Q(kind="reply", resolved_at__isnull=True),
                name="unique_unresolved_reply_report",
            ),
            models.UniqueConstraint(
                fields=["reporter", "reported_rating"],
                condition=models.Q(kind="rating", resolved_at__isnull=True),
                name="unique_unresolved_rating_report",
            ),
            models.UniqueConstraint(
                fields=["reporter", "reported_user"],
                condition=models.Q(kind="user", resolved_at__isnull=True),
                name="unique_unresolved_user_report",
            ),
            models.UniqueConstraint(
                fields=["reporter", "reported_user"],
                condition=models.Q(kind="profile", resolved_at__isnull=True),
                name="unique_unresolved_profile_report",
            ),
        ]

    def __str__(self):
        target = (
            self.event_title
            if self.kind == self.Kind.EVENT
            else f"{self.get_kind_display()} report #{self.pk}"
        )
        return f"{self.get_reason_display()} — {target}"
