"""Admin listing of questions forwarded by the website assistant."""

from django.contrib import admin

from .inquiries import answer_inquiry
from .models import FAQ, ChatLog, Inquiry


@admin.register(Inquiry)
class InquiryAdmin(admin.ModelAdmin):
    """Let staff review forwarded questions and their answers."""

    list_display = ("__str__", "event", "user", "language", "status", "created_at")
    list_filter = ("status",)
    list_select_related = ("event", "user")
    search_fields = ("question", "question_english", "answer")
    readonly_fields = (
        "user",
        "event",
        "question",
        "question_english",
        "language",
        "status",
        "answer_translated",
        "answered_by",
        "answered_at",
        "delivered_at",
        "created_at",
    )

    def has_add_permission(self, request):
        """Questions only come from the assistant."""
        return False

    def save_model(self, request, obj, form, change):
        """Answering an open question here sends the answer to the asker too."""
        if obj.status == Inquiry.Status.OPEN and obj.answer.strip():
            answer_inquiry(obj, obj.answer.strip(), request.user)
        else:
            super().save_model(request, obj, form, change)


@admin.register(FAQ)
class FAQAdmin(admin.ModelAdmin):
    """Edit the FAQ the assistant answers from; changes apply immediately."""

    list_display = ("question", "topic", "is_active", "position", "updated_at")
    list_editable = ("is_active", "position")
    list_filter = ("topic", "is_active")
    search_fields = ("question", "answer")


@admin.register(ChatLog)
class ChatLogAdmin(admin.ModelAdmin):
    """Read-only anonymous conversation logs, to spot missing FAQ topics."""

    list_display = (
        "__str__",
        "outcome",
        "signed_in",
        "via_voice",
        "response_ms",
        "created_at",
    )
    list_filter = ("outcome", "signed_in", "via_voice")
    search_fields = ("message", "reply")
    date_hierarchy = "created_at"

    def has_add_permission(self, request):
        """Logs are written by the assistant only."""
        return False

    def has_change_permission(self, request, obj=None):
        """Logs can be read and deleted, not edited."""
        return False
