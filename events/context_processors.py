"""Template context shared by every page."""

from django.db.models import Q

from .models import DirectMessage


def notifications(request):
    """Expose unread notification and direct-message counts.

    The counts are callables, so their queries only run on pages that show them.
    """
    if not request.user.is_authenticated:
        return {}
    unread = request.user.notifications.filter(read_at__isnull=True)
    unread_messages = DirectMessage.objects.filter(
        Q(conversation__user_one=request.user) | Q(conversation__user_two=request.user),
        read_at__isnull=True,
    ).exclude(sender=request.user)
    return {
        "unread_notification_count": unread.count,
        "unread_message_count": unread_messages.count,
    }
