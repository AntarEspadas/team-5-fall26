"""Admin configuration for imported and hosted events."""

from django.contrib import admin

from .models import (
    Event,
    EventPost,
    EventReply,
    HostRating,
    Notification,
    Registration,
)


@admin.register(Registration)
class RegistrationAdmin(admin.ModelAdmin):
    """Show who registered for which event and how many they are bringing."""

    list_display = ("event", "user", "party_size", "created_at")
    list_select_related = ("event", "user")
    search_fields = ("event__title", "user__username")


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


@admin.register(HostRating)
class HostRatingAdmin(admin.ModelAdmin):
    """Let moderators review and remove host ratings flagged in reports."""

    list_display = ("__str__", "host", "stars", "updated_at")
    list_select_related = ("event", "host", "rater")
    search_fields = ("comment", "event__title", "host__username")


@admin.register(EventPost)
class EventPostAdmin(admin.ModelAdmin):
    """Manage public discussion comments flagged in the moderation queue."""

    list_display = ("__str__", "event", "author", "created_at")
    list_select_related = ("event", "author")
    search_fields = ("body", "event__title", "author__username")


@admin.register(EventReply)
class EventReplyAdmin(admin.ModelAdmin):
    """Manage replies independently of their parent comments."""

    list_display = ("__str__", "post", "author", "created_at")
    list_select_related = ("post", "author")
    search_fields = ("body", "post__event__title", "author__username")
