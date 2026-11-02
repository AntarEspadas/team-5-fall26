"""Questions the website assistant passed on to an event manager."""

from django.db import models


class Inquiry(models.Model):
    """A visitor's question the assistant could not answer from site data.

    Questions about a hosted event go to its host; everything else goes to
    staff. Once answered, the answer is shown in the asker's chat window and,
    for signed-in askers, sent as an email and (for event questions) an in-app
    notification. ``answer_translated`` holds the answer in the language the
    question was asked in, when that isn't English.
    """

    class Status(models.TextChoices):
        OPEN = "open", "Open"
        ANSWERED = "answered", "Answered"

    user = models.ForeignKey(
        "accounts.User",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="assistant_inquiries",
        verbose_name="asked by",
    )
    event = models.ForeignKey(
        "events.Event",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="assistant_inquiries",
    )
    question = models.TextField("question", max_length=2000)
    question_english = models.TextField("question in English", blank=True)
    language = models.CharField("language", max_length=40, blank=True)
    status = models.CharField(
        "status",
        max_length=10,
        choices=Status.choices,
        default=Status.OPEN,
        db_index=True,
    )
    answer = models.TextField("answer", max_length=4000, blank=True)
    answer_translated = models.TextField("translated answer", blank=True)
    answered_by = models.ForeignKey(
        "accounts.User",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="answered_assistant_inquiries",
    )
    answered_at = models.DateTimeField("answered at", null=True, blank=True)
    delivered_at = models.DateTimeField("shown in chat at", null=True, blank=True)
    created_at = models.DateTimeField("asked at", auto_now_add=True)

    class Meta:
        ordering = ["-created_at", "-pk"]
        verbose_name = "assistant inquiry"
        verbose_name_plural = "assistant inquiries"

    def __str__(self):
        return self.display_question[:80]

    @property
    def display_question(self):
        """Return the question in English when a translation is available."""
        return self.question_english or self.question

    @property
    def is_english(self):
        """Return whether the question was asked in English (or unknown)."""
        return self.language.strip().lower() in ("", "en", "english")

    @property
    def answer_for_asker(self):
        """Return the answer in the asker's language when it was translated."""
        return self.answer_translated or self.answer
