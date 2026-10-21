"""Template context shared by every page."""


def notifications(request):
    """Expose the signed-in user's unread notification count.

    The count is a callable, so the query only runs on pages that show it.
    """
    if not request.user.is_authenticated:
        return {}
    unread = request.user.notifications.filter(read_at__isnull=True)
    return {"unread_notification_count": unread.count}
