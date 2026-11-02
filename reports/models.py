"""Reports of content that may need moderation."""

from django.conf import settings
from django.db import models


class Report(models.Model):
    """An event report, retaining its history after content or account deletion.

    A null resolved_at means the report remains open. Other report targets
    can share the reason, details, and resolution fields in the future.
    """

    class Reason(models.TextChoices):
        SPAM_SCAM = "spam_scam", "Spam or scam"
        MISLEADING_INFORMATION = "misleading_information", "Misleading information"
        HARASSMENT_HATE = "harassment_hate", "Harassment or hate"
        INAPPROPRIATE_CONTENT = "inappropriate_content", "Inappropriate content"
        SAFETY_CONCERN = "safety_concern", "Safety concern"
        OTHER = "other", "Other"

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
    event_title = models.CharField(max_length=300, editable=False)
    reason = models.CharField(max_length=32, choices=Reason.choices)
    details = models.TextField(max_length=5000, blank=True)
    created_at = models.DateTimeField(auto_now_add=True, db_index=True)
    resolved_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["-created_at", "-pk"]
        constraints = [
            models.UniqueConstraint(
                fields=["reporter", "event"],
                condition=models.Q(resolved_at__isnull=True),
                name="unique_unresolved_event_report",
            ),
        ]

    def __str__(self):
        return f"{self.get_reason_display()} — {self.event_title}"
