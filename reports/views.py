"""Views for reporting events."""

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.views.decorators.http import require_http_methods

from events.models import Event

from .forms import EventReportForm
from .models import Report

ALREADY_REPORTED_MESSAGE = "You have already reported this event."


@login_required
@require_http_methods(["GET", "POST"])
def report_event(request, pk):
    """Record one unresolved report per user and event, in any event state."""
    event = get_object_or_404(Event, pk=pk)
    detail_url = reverse("event_detail", args=[event.pk])
    if Report.objects.filter(
        reporter=request.user, event=event, resolved_at__isnull=True
    ).exists():
        messages.info(request, ALREADY_REPORTED_MESSAGE)
        return redirect(detail_url)

    form = EventReportForm(request.POST if request.method == "POST" else None)
    if form.is_bound and form.is_valid():
        # The conditional unique constraint makes get_or_create race-safe:
        # simultaneous submissions keep the first report and its original text.
        _, created = Report.objects.get_or_create(
            reporter=request.user,
            event=event,
            resolved_at=None,
            defaults={
                "event_title": event.title,
                "reason": form.cleaned_data["reason"],
                "details": form.cleaned_data["details"],
            },
        )
        if created:
            messages.success(request, "Your report has been submitted.")
        else:
            messages.info(request, ALREADY_REPORTED_MESSAGE)
        return redirect(detail_url)

    return render(request, "reports/report_event.html", {"event": event, "form": form})
