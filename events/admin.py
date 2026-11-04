"""Admin configuration for imported and hosted events."""

from django.contrib import admin

from .models import Event, Notification


@admin.register(Notification)
class NotificationAdmin(admin.ModelAdmin):
    """List sent notifications so staff can check who was told what."""

    list_display = (
        "subject",
        "recipient",
        "event",
        "contact_request",
        "created_at",
        "read_at",
    )
    list_select_related = (
        "recipient",
        "event",
        "contact_request__sender",
        "contact_request__recipient",
    )


@admin.register(Event)
class EventAdmin(admin.ModelAdmin):
    """Show events in a readable, searchable list, with hosts if any."""

    list_display = ("title", "start_time", "end_time", "status", "host")
    list_filter = ("status", "borough")
    list_select_related = ("host",)
    search_fields = ("title", "location", "park_names")
    ordering = ("start_time",)
