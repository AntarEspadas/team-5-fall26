"""App configuration for the events app."""

from django.apps import AppConfig


class EventsConfig(AppConfig):
    """Register the events app with Django."""

    default_auto_field = "django.db.models.BigAutoField"
    name = "events"
