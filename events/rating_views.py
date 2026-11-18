"""Let attendees rate the host of an event they went to."""

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.db import transaction
from django.shortcuts import get_object_or_404, redirect
from django.urls import reverse
from django.utils.http import url_has_allowed_host_and_scheme
from django.views.decorators.http import require_POST

from .forms import HostRatingForm
from .models import Event, HostRating
from .ratings import rating_block_reason


def safe_next_url(request, fallback):
    """Return the posted ``next`` URL when it stays on this site."""
    next_url = request.POST.get("next")
    if url_has_allowed_host_and_scheme(
        next_url,
        allowed_hosts={request.get_host()},
        require_https=request.is_secure(),
    ):
        return next_url
    return fallback


@login_required
@require_POST
def rate_host(request, pk):
    """Save or update the signed-in attendee's rating of an event's host."""
    event = get_object_or_404(Event.objects.select_related("host"), pk=pk)
    next_url = safe_next_url(
        request, reverse("event_detail", args=[event.pk]) + "#rate-host"
    )

    reason = rating_block_reason(event, request.user)
    if reason:
        messages.error(request, reason)
        return redirect(next_url)

    form = HostRatingForm(request.POST)
    if not form.is_valid():
        first_error = next(iter(form.errors.values()))[0]
        messages.error(request, first_error)
        return redirect(next_url)

    with transaction.atomic():
        _, created = HostRating.objects.update_or_create(
            event=event,
            rater=request.user,
            defaults={
                "host": event.host,
                "stars": form.cleaned_data["stars"],
                "comment": form.cleaned_data["comment"],
            },
        )

    if created:
        messages.success(request, "Thanks, your rating was saved.")
    else:
        messages.success(request, "Your rating was updated.")
    return redirect(next_url)
