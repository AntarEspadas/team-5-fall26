"""Template context for the chat widget shown on every page."""

from django.conf import settings


def chatbot(request):
    """Tell templates whether to show the assistant's chat widget."""
    return {
        "chatbot_enabled": settings.CHATBOT_ENABLED,
        "chatbot_support_email": settings.CHATBOT_SUPPORT_EMAIL,
    }
