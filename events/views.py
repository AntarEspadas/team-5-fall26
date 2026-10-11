"""Views for the landing page and public event search."""

from urllib.parse import quote, urlencode

from django.db.models import F
from django.http import QueryDict
from django.urls import reverse
from django.views.generic import DetailView, ListView, TemplateView

from .filters import EventFilterForm, filter_events, filter_params
from .models import Event
from .search import search_events

UPCOMING_EVENT_LIMIT = 20
SEARCH_PAGE_SIZE = 20

class EventDetailView(DetailView):
    """Display the full details of a single event."""

    model = Event
    template_name = "events/detail.html"
    context_object_name = "event"


class LandingView(TemplateView):
    """Render the landing page with upcoming events for signed-in visitors.

    Anonymous visitors see the same page with the login prompt, matching the
    behaviour the accounts tests expect, but no event list.
    """

    template_name = "events/landing.html"

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
