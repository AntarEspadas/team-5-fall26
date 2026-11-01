"""Views for the landing page, public event search, and hosted events."""

from urllib.parse import quote, urlencode
from uuid import uuid4

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.contrib.auth.mixins import LoginRequiredMixin
from django.db import transaction
from django.db.models import Case, F, Value, When
from django.http import Http404, QueryDict
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils import timezone
from django.views.decorators.http import require_http_methods, require_POST
from django.views.generic import (
    CreateView,
    DetailView,
    ListView,
    TemplateView,
    UpdateView,
)

from .filters import EventFilterForm, filter_events, filter_params
from .forms import CancelEventForm, EventListingForm, NotifyAttendeesForm
from .models import ContactRequest, Event, EventInterest, EventPost, Notification
from .notifications import (
    cancel_notice,
    change_notice,
    interested_recipients,
    notify_interested,
)
from .search import search_events

UPCOMING_EVENT_LIMIT = 20
SEARCH_PAGE_SIZE = 20
NOTIFICATION_PAGE_SIZE = 20


class EventCreateView(LoginRequiredMixin, CreateView):
    """Let a signed-in user post an event that everyone can discover."""

    model = Event
    form_class = EventListingForm
    template_name = "events/event_form.html"

    def form_valid(self, form):
        """Make the current user the host and give the event its own ID."""
        form.instance.host = self.request.user
        form.instance.guid = Event.HOSTED_GUID_PREFIX + uuid4().hex
        response = super().form_valid(form)
        messages.success(self.request, "Your event is posted.")
        return response

    def get_success_url(self):
        """Show the new event's detail page."""
        return reverse("event_detail", args=[self.object.pk])


def get_hosted_upcoming_event(user, pk):
    """Return an event the user hosts that has not ended or been cancelled.

    Other users' events, NYC Parks feed events, and events that have ended
    or been cancelled are reported as not found.
    """
    event = get_object_or_404(Event, pk=pk, host=user)
    if not event.is_open:
        raise Http404("This event has ended or was cancelled.")
    return event


def people(count):
    """Return e.g. "1 interested person" or "3 interested people"."""
    return f"{count} interested {'person' if count == 1 else 'people'}"


class EventUpdateView(LoginRequiredMixin, UpdateView):
    """Let a host edit their own event until it ends.

    Changing the date, time, or location notifies everyone interested.
    """

    form_class = EventListingForm
    template_name = "events/event_form.html"

    def get_object(self, queryset=None):
        """Only the host can edit, and only before the event ends."""
        return get_hosted_upcoming_event(self.request.user, self.kwargs["pk"])

    def form_valid(self, form):
        """Save the changes, notify interested users, and confirm on the page."""
        notice = change_notice(form.instance, form.initial, form.changed_data)
        response = super().form_valid(form)
        confirmation = "Event updated."
        if notice:
            event_url = self.request.build_absolute_uri(self.get_success_url())
            count = notify_interested(self.object, *notice, event_url=event_url)
            if count:
                confirmation += f" We let {people(count)} know about the change."
        messages.success(self.request, confirmation)
        return response

    def get_success_url(self):
        """Show the updated event's detail page."""
        return reverse("event_detail", args=[self.object.pk])


@login_required
@require_POST
def notify_attendees(request, pk):
    """Send the host's message to everyone interested in their event."""
    event = get_hosted_upcoming_event(request.user, pk)
    detail_url = reverse("event_detail", args=[event.pk])
    form = NotifyAttendeesForm(request.POST)
    if not form.is_valid():
        messages.error(request, "Write a subject and a message before sending.")
        return redirect(detail_url)
    count = notify_interested(
        event,
        form.cleaned_data["subject"],
        form.cleaned_data["message"],
        event_url=request.build_absolute_uri(detail_url),
    )
    if count:
        messages.success(request, f"Your message was sent to {people(count)}.")
    else:
        messages.info(request, "Nobody is interested in this event yet.")
    return redirect(detail_url)


@login_required
@require_http_methods(["GET", "POST"])
def cancel_event(request, pk):
    """Confirm, then cancel a host's event and tell everyone interested."""
    event = get_hosted_upcoming_event(request.user, pk)
    form = CancelEventForm(request.POST if request.method == "POST" else None)
    if form.is_bound and form.is_valid():
        detail_url = reverse("event_detail", args=[event.pk])
        with transaction.atomic():
            # Only one of two simultaneous submissions can make the change,
            # so attendees are never told twice.
            cancelled = Event.objects.filter(
                pk=event.pk, status=Event.Status.UPCOMING
            ).update(status=Event.Status.CANCELLED, updated_at=timezone.now())
            if not cancelled:
                raise Http404("This event was already cancelled.")
            event.status = Event.Status.CANCELLED
            count = notify_interested(
                event,
                *cancel_notice(event, form.cleaned_data["message"]),
                event_url=request.build_absolute_uri(detail_url),
            )
        confirmation = "Event cancelled."
        if count:
            confirmation += f" We let {people(count)} know."
        messages.success(request, confirmation)
        return redirect(detail_url)
    return render(
        request,
        "events/event_cancel.html",
        {
            "event": event,
            "form": form,
            "recipient_count": interested_recipients(event).count(),
        },
    )


class MyEventsView(LoginRequiredMixin, TemplateView):
    """List the events a user posted: upcoming ones, then archived ones."""

    template_name = "events/my_events.html"

    def get_context_data(self, **kwargs):
        """Split hosted events into upcoming (soonest first) and archived.

        Archived events are cancelled or over, most recent first.
        """
        context = super().get_context_data(**kwargs)
        events = list(
            self.request.user.hosted_events.order_by(
                F("start_time").asc(nulls_last=True), "end_time", "title", "pk"
            )
        )
        context["upcoming_events"] = [event for event in events if event.is_open]
        context["archived_events"] = [
            event for event in reversed(events) if not event.is_open
        ]
        return context


class NotificationListView(LoginRequiredMixin, ListView):
    """List contact requests first, newest first within each group.

    Displayed event updates become read. Pending contact requests stay new
    until the recipient resolves them.
    """

    template_name = "events/notifications.html"
    context_object_name = "notifications"
    paginate_by = NOTIFICATION_PAGE_SIZE

    def get_queryset(self):
        """Order both groups before pagination and load request context."""
        return (
            self.request.user.notifications.select_related(
                "event",
                "contact_request__sender",
            )
            .annotate(
                _request_priority=Case(
                    When(contact_request__isnull=False, then=Value(0)),
                    default=Value(1),
                )
            )
            .order_by("_request_priority", "-created_at", "-pk")
        )

    def get_context_data(self, **kwargs):
        """Mark only displayed event updates read."""
        context = super().get_context_data(**kwargs)
        shown = context["notifications"]
        unread = [
            n.pk for n in shown if n.read_at is None and n.contact_request_id is None
        ]
        Notification.objects.filter(pk__in=unread).update(read_at=timezone.now())
        return context


class EventDetailView(DetailView):
    """Display event details and its community content."""

    model = Event
    template_name = "events/detail.html"
    context_object_name = "event"

    def get_context_data(self, **kwargs):
        """Add attendance intentions, attendee profiles, and discussions."""
        context = super().get_context_data(**kwargs)

        event = self.object
        user = self.request.user
        is_authenticated = user.is_authenticated

        # Get all attendance intention records for this event.
        interests = EventInterest.objects.filter(event=event)

        # Count intentions directly from the database.
        context["interest_count"] = interests.count()
        context["interested_count"] = interests.filter(
            status=EventInterest.Status.INTERESTED
        ).count()
        context["want_to_attend_count"] = interests.filter(
            status=EventInterest.Status.WANT_TO_ATTEND
        ).count()

        # Get the current user's selected intention.
        current_interest = (
            interests.filter(user=user).first() if is_authenticated else None
        )

        context["user_has_interest"] = current_interest is not None
        context["current_interest_status"] = (
            current_interest.status if current_interest else ""
        )

        # Control whether users can interact with the event.
        context["can_interact"] = (
            is_authenticated and event.status == Event.Status.UPCOMING
        )
        context["can_manage"] = (
            is_authenticated and event.host_id == user.pk and event.is_open
        )
        context["host_cancelled"] = event.status == Event.Status.CANCELLED

        if context["can_manage"]:
            context["recipient_count"] = interested_recipients(event).count()

        # Find contact requests sent by the current user.
        request_by_recipient = {}

        if is_authenticated:
            requests = (
                ContactRequest.objects.filter(
                    event=event,
                    sender=user,
                )
                .exclude(status=ContactRequest.Status.DECLINED)
                .order_by("-created_at")
            )

            for contact_request in requests:
                request_by_recipient.setdefault(
                    contact_request.recipient_id,
                    contact_request,
                )

        # Include all users who have chosen to share their profiles.
        # Do not exclude the current user: their avatar should also move
        # between the two groups when they change their intention.
        visible_interests = (
            interests.filter(user__share_profile_with_attendees=True)
            .select_related("user")
            .order_by("user__display_name", "user__username")
        )

        attendees = [
            {
                "user": interest.user,
                "status": interest.status,
                "contact_request": request_by_recipient.get(interest.user_id),
            }
            for interest in visible_interests
        ]

        # Split the same attendee list into the two intention groups.
        context["interested_users"] = [
            attendee
            for attendee in attendees
            if attendee["status"] == EventInterest.Status.INTERESTED
        ]

        context["want_to_attend_users"] = [
            attendee
            for attendee in attendees
            if attendee["status"] == EventInterest.Status.WANT_TO_ATTEND
        ]

        context["attendees"] = attendees

        # Event discussion posts and replies.
        context["posts"] = (
            EventPost.objects.filter(event=event)
            .select_related("author")
            .prefetch_related("replies__author")
        )

        # Contact requests received by the current user.
        context["received_contact_requests"] = (
            ContactRequest.objects.filter(
                event=event,
                recipient=user,
                status=ContactRequest.Status.PENDING,
            ).select_related("sender")
            if is_authenticated
            else ContactRequest.objects.none()
        )

        return context


class LandingView(TemplateView):
    """Render the landing page with upcoming events for signed-in visitors.

    Anonymous visitors see the same page with the login prompt, matching the
    behaviour the accounts tests expect, but no event list.
    """

    template_name = "events/home.html"

    def get_context_data(self, **kwargs):
        """Add upcoming events, soonest first, and an empty filter bar."""
        context = super().get_context_data(**kwargs)
        context["filter_form"] = EventFilterForm()
        context["events"] = []
        if self.request.user.is_authenticated:
            context["events"] = Event.objects.filter(
                status=Event.Status.UPCOMING
            ).order_by(F("start_time").asc(nulls_last=True), "end_time", "title")[
                :UPCOMING_EVENT_LIMIT
            ]
        return context


class SearchView(ListView):
    """Let any visitor search and filter upcoming events.

    A search term ranks results by relevance; filters alone list matching
    events soonest first. Without either, the page asks for input instead of
    listing every event.
    """

    template_name = "events/search.html"
    context_object_name = "events"
    paginate_by = SEARCH_PAGE_SIZE

    def get_queryset(self):
        """Return upcoming events matching the search term and every filter."""
        self.query = self.request.GET.get("q", "").strip()
        form = EventFilterForm(self.request.GET)
        form.is_valid()
        filters = form.cleaned_data
        self.filter_params = filter_params(filters)
        if form.errors:
            self.filter_form = form
        else:
            self.filter_form = EventFilterForm(QueryDict(urlencode(self.filter_params)))
        events = filter_events(
            Event.objects.filter(status=Event.Status.UPCOMING), filters
        )
        if self.query:
            return search_events(events, self.query)
        if not self.filter_params:
            return Event.objects.none()
        return events.order_by(
            F("start_time").asc(nulls_last=True), "end_time", "title", "pk"
        )

    def get_context_data(self, **kwargs):
        """Keep the search term and filters for the form and pagination links."""
        context = super().get_context_data(**kwargs)
        search_params = [("q", self.query)] if self.query else []
        context["query"] = self.query
        context["filter_form"] = self.filter_form
        context["has_filters"] = bool(self.filter_params)
        context["page_query"] = urlencode(
            search_params + self.filter_params, quote_via=quote
        )
        if self.query:
            context["clear_filters_url"] = "?" + urlencode(
                search_params, quote_via=quote
            )
        else:
            context["clear_filters_url"] = reverse("landing")
        return context
