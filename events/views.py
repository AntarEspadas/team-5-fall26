"""Views for the landing page and public event search."""

from urllib.parse import quote, urlencode

from django.contrib.auth import get_user_model
from django.db.models import F
from django.http import QueryDict
from django.urls import reverse
from django.views.generic import DetailView, ListView, TemplateView

from .filters import EventFilterForm, filter_events, filter_params
from .models import ContactRequest, Event, EventInterest, EventPost
from .search import search_events

UPCOMING_EVENT_LIMIT = 20
SEARCH_PAGE_SIZE = 20


class EventDetailView(DetailView):
    """Display event details and its community content."""

    model = Event
    template_name = "events/detail.html"
    context_object_name = "event"

    def get_context_data(self, **kwargs):
        """Add interests, public attendee profiles, and discussions."""
        context = super().get_context_data(**kwargs)
        event = self.object
        user = self.request.user
        is_authenticated = user.is_authenticated

        context["interest_count"] = EventInterest.objects.filter(event=event).count()

        context["user_has_interest"] = (
            EventInterest.objects.filter(event=event, user=user).exists()
            if is_authenticated
            else False
        )

        context["can_interact"] = (
            is_authenticated and event.status == Event.Status.UPCOMING
        )

        User = get_user_model()
        attendee_query = User.objects.filter(
            share_profile_with_attendees=True,
            event_interests__event=event,
        ).distinct()

        if is_authenticated:
            attendee_query = attendee_query.exclude(pk=user.pk)

        attendees = list(attendee_query.order_by("display_name", "username"))

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

        context["attendees"] = [
            {
                "user": attendee,
                "contact_request": request_by_recipient.get(attendee.pk),
            }
            for attendee in attendees
        ]

        context["posts"] = (
            EventPost.objects.filter(event=event)
            .select_related("author")
            .prefetch_related("replies__author")
        )

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
