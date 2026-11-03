"""App configuration for the website assistant."""

from django.apps import AppConfig


class ChatbotConfig(AppConfig):
    """Register the website assistant (chat widget) with Django."""

    default_auto_field = "django.db.models.BigAutoField"
    name = "chatbot"
    verbose_name = "Website assistant"
