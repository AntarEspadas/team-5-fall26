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


class FAQ(models.Model):
    """A question and answer the assistant uses for support topics.

    Staff edit these in the Django admin, so policies such as refunds can be
    updated without a code change. Every active entry is given to the
    assistant with each conversation.
    """

    class Topic(models.TextChoices):
        BOOKING = "booking", "Booking & attending"
        CANCELLATIONS = "cancellations", "Cancellations & refunds"
        PAYMENTS = "payments", "Payments & prices"
        ACCOUNT = "account", "Account & login"
        EVENTS = "events", "Events & venues"
        GENERAL = "general", "General"

    topic = models.CharField(
        "topic", max_length=20, choices=Topic.choices, default=Topic.GENERAL
    )
    question = models.CharField("question", max_length=300)
    answer = models.TextField("answer", max_length=2000)
    is_active = models.BooleanField(
        "active", default=True, help_text="Only active entries reach the assistant."
    )
    position = models.PositiveIntegerField(
        "position", default=0, help_text="Lower numbers come first."
    )
    updated_at = models.DateTimeField("updated at", auto_now=True)

    class Meta:
        ordering = ["topic", "position", "pk"]
        verbose_name = "FAQ entry"
        verbose_name_plural = "FAQ entries"

    def __str__(self):
        return self.question


class ChatLog(models.Model):
    """An anonymous record of one exchange, used to improve the FAQ.

    Logs never store who asked. Email addresses and long numbers such as
    phone or card numbers are masked before saving.
    """

    class Outcome(models.TextChoices):
        ANSWERED = "answered", "Answered"
        UNANSWERED = "unanswered", "Couldn't answer"
        # Older conversations, from when unanswered questions were forwarded.
        ESCALATED = "escalated", "Sent to event manager"
        DECLINED = "declined", "Declined (safety)"
        ERROR = "error", "Gemini unavailable"

    created_at = models.DateTimeField("time", auto_now_add=True, db_index=True)
    signed_in = models.BooleanField("signed in", default=False)
    via_voice = models.BooleanField("voice", default=False)
    page = models.CharField("page", max_length=200, blank=True)
    message = models.TextField("message")
    reply = models.TextField("reply", blank=True)
    outcome = models.CharField(
        "outcome", max_length=10, choices=Outcome.choices, db_index=True
    )
    response_ms = models.PositiveIntegerField("response time (ms)", default=0)

    class Meta:
        ordering = ["-created_at", "-pk"]
        verbose_name = "chat log"
        verbose_name_plural = "chat logs"

    def __str__(self):
        return self.message[:80]
