"""The header's notification bell: the latest notifications in a dropdown."""

from django.contrib.auth.decorators import login_required
from django.db.models import Case, Value, When
from django.http import JsonResponse
from django.template.loader import render_to_string
from django.utils import timezone
from django.views.decorators.http import require_POST

from .models import Notification

BELL_SIZE = 5


@login_required
@require_POST
def notification_bell(request):
    """Return the 5 latest notifications and mark the shown ones read.

    Opening the bell counts as reading what it shows. Pending contact
    requests stay new until they are answered, as on the notifications page,
    and come first so newer event updates cannot push them out of the five.
    Answered requests sort by date so they do not fill the bell for good.
    """
    latest = list(
        request.user.notifications.select_related("event", "contact_request__sender")
        .annotate(
            _pending_request=Case(
                When(
                    contact_request__isnull=False, read_at__isnull=True, then=Value(0)
                ),
                default=Value(1),
            )
        )
        .order_by("_pending_request", "-created_at", "-pk")[:BELL_SIZE]
    )
    to_mark = [
        n.pk for n in latest if n.read_at is None and n.contact_request_id is None
    ]
    if to_mark:
        Notification.objects.filter(pk__in=to_mark).update(read_at=timezone.now())
    html = render_to_string(
        "events/_bell_items.html",
        {"notifications": latest, "newly_read": set(to_mark)},
        request=request,
    )
    unread = request.user.notifications.filter(read_at__isnull=True).count()
    response = JsonResponse({"html": html, "unread_count": unread})
    response["Cache-Control"] = "no-store"
    return response
