"""Admin listing of questions forwarded by the website assistant."""

from django.contrib import admin

from .inquiries import answer_inquiry
from .models import Inquiry


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
