"""Admin configuration for imported and hosted events."""

from django.contrib import admin

from .models import Event


@admin.register(Event)
class EventAdmin(admin.ModelAdmin):
    """Show events in a readable, searchable list, with hosts if any."""

    list_display = ("title", "start_time", "end_time", "status", "host")
    list_filter = ("status", "borough")
    list_select_related = ("host",)
    search_fields = ("title", "location", "park_names")
    ordering = ("start_time",)
