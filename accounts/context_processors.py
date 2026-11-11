"""Expose the viewer's current submission restrictions to community templates."""

from .ban_responses import BAN_MESSAGE
from .ban_services import get_active_ban


def ban_status(request):
    return {
        "viewer_banned": get_active_ban(request.user) is not None,
        "ban_message": BAN_MESSAGE,
    }
