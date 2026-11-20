"""Views for the landing page, public event search, and hosted events."""

from urllib.parse import quote, urlencode
from uuid import uuid4

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.contrib.auth.mixins import LoginRequiredMixin
from django.db import transaction
from django.db.models import Case, Exists, F, OuterRef, Q, Sum, Value, When
from django.http import Http404, HttpResponse, QueryDict
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils import timezone
from django.utils.decorators import method_decorator
from django.views.decorators.cache import never_cache
from django.views.decorators.http import (
    require_http_methods,
    require_POST,
    require_safe,
)
from django.views.generic import (
    CreateView,
    DetailView,
    ListView,
    TemplateView,
    UpdateView,
)
from django.utils.http import (
    content_disposition_header,
    url_has_allowed_host_and_scheme,
)

from accounts.ban_services import get_active_ban
from accounts.models import UserBan

from .calendar_export import (
    can_add_to_calendar,
    google_calendar_url,
    has_calendar_file,
    icalendar_text,
    ics_filename,
    webcal_url,
)
from .filters import (
    NYC_TIMEZONE,
    EventFilterForm,
    filter_events,
    filter_params,
)
from .forms import (
    CancelEventForm,
    EventListingForm,
    NotifyAttendeesForm,
    PartySizeForm,
)
from .models import (
    ContactRequest,
    Event,
    EventInterest,
    EventPost,
    FavoriteEvent,
    HostRating,
    Notification,
    Registration,
    SearchHistory,
)
from .notifications import (
    cancel_notice,
    change_notice,
    interested_recipients,
    notify_interested,
    notify_registration_change,
    party,
)
from .ratings import (
    host_rating_summary,
    rating_block_reason,
    rating_closes_at,
    rating_window_open,
)
from .natural_search import MAX_PROMPT_LENGTH, describe_search, natural_search_url
from .recommendations import record_search, recommended_events
from .search import search_events
from .similar_interests import find_similar_interest_users
from .photos import gallery_context, uploadable_events

UPCOMING_EVENT_LIMIT = 20
SEARCH_PAGE_SIZE = 20
NOTIFICATION_PAGE_SIZE = 20
ARCHIVED_EVENT_PREVIEW_LIMIT = 3

ARCHIVED_EVENT_PAGE_SIZE = 20


def annotate_for_viewer(queryset, user):
    """Mark each event the viewer registered for or already rated the host of.

    Event cards use these flags to choose their button: Cancel Registration
    on upcoming events, and Rate on ended ones the viewer attended.
    """
    return queryset.annotate(
        user_is_registered=Exists(
            Registration.objects.filter(event_id=OuterRef("pk"), user=user)
        ),
        user_rated_host=Exists(
            HostRating.objects.filter(event_id=OuterRef("pk"), rater=user)
        ),
    )


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
    """Return e.g. "1 person" or "3 people"."""
    return f"{count} {'person' if count == 1 else 'people'}"


class EventUpdateView(LoginRequiredMixin, UpdateView):
    """Let a host edit their own event until it ends.

    Changing the date, time, or location notifies everyone interested or
    registered.
    """

    form_class = EventListingForm
    template_name = "events/event_form.html"

    def get_object(self, queryset=None):
        """Only the host can edit, and only before the event ends."""
        return get_hosted_upcoming_event(self.request.user, self.kwargs["pk"])

    def form_valid(self, form):
        """Save the changes, notify interested and registered users, and
        confirm on the page."""
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
    """Send the host's message to everyone interested in or registered for
    their event."""
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
        messages.info(
            request, "Nobody is interested in or registered for this event yet."
        )
    return redirect(detail_url)


@login_required
@require_http_methods(["GET", "POST"])
def cancel_event(request, pk):
    """Confirm, then cancel a host's event and tell everyone interested or
    registered."""
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
    """List a user's hosted, registered, and privately saved events."""

    template_name = "events/my_events.html"

    def get_context_data(self, **kwargs):
        """Show the user's hosted, registered and favorite events."""
        context = super().get_context_data(**kwargs)
        user = self.request.user
        events = list(
            user.hosted_events.order_by(
                F("start_time").asc(nulls_last=True),
                "end_time",
                "title",
                "pk",
            )
        )
        context["upcoming_events"] = [event for event in events if event.is_open]
        context["archived_events"] = [
            event for event in reversed(events) if not event.is_open
        ]
        registrations = list(
            Registration.objects.filter(user=user)
            .select_related("event")
            .order_by(
                F("event__start_time").asc(nulls_last=True),
                "event__end_time",
                "event__title",
            )
        )
        stars_by_event = dict(
            HostRating.objects.filter(rater=user).values_list("event_id", "stars")
        )
        can_rate_hosts = get_active_ban(user) is None
        for registration in registrations:
            event = registration.event
            registration.my_stars = stars_by_event.get(event.pk)
            registration.can_rate = (
                can_rate_hosts
                and event.host_id is not None
                and event.host_id != user.pk
                and rating_window_open(event)
            )
        # Attended: registrations for events that are over and weren't
        # cancelled, most recent first. Upcoming ones are on My plans.
        context["attended_registrations"] = [
            registration
            for registration in reversed(registrations)
            if registration.event.has_ended and not registration.event.is_cancelled
        ]
        context["plan_days"] = plan_timeline(context["upcoming_events"], registrations)
        context["favorite_events"] = user.favorite_events.select_related("event")
        context["favorites_tab_active"] = self.request.GET.get("tab") == "favorites"
        return context


def plan_timeline(hosted_events, registrations):
    """Group a user's upcoming plans by day, in date order.

    Plans are the open events the user hosts or registered for, cancelled
    ones left out. Each day is ``(date, plans)``, where a plan is a dict with
    the event, ``hosting``, and the party size when registered. Events
    without a date come last under a ``None`` day.
    """
    plans = {}
    for event in hosted_events:
        if not event.is_cancelled:
            plans[event.pk] = {"event": event, "hosting": True, "party_size": None}
    for registration in registrations:
        event = registration.event
        if not event.is_open or event.is_cancelled:
            continue
        plan = plans.setdefault(
            event.pk, {"event": event, "hosting": False, "party_size": None}
        )
        plan["party_size"] = registration.party_size

    def sort_key(plan):
        event = plan["event"]
        return (event.start_time is None, event.start_time, event.title, event.pk)

    days = []
    for plan in sorted(plans.values(), key=sort_key):
        start = plan["event"].start_time
        day = timezone.localtime(start, NYC_TIMEZONE).date() if start else None
        if not days or days[-1][0] != day:
            days.append((day, []))
        days[-1][1].append(plan)
    return days


@login_required
@require_POST
def set_event_favorite(request, pk):
    """Save or remove only the signed-in user's own favorite."""
    event = get_object_or_404(Event, pk=pk)
    detail_url = reverse("event_detail", args=[pk])
    destination = (
        reverse("my_events") + "?tab=favorites"
        if request.POST.get("source") == "favorites"
        else detail_url
    )
    action = request.POST.get("action")
    if action == "remove":
        FavoriteEvent.objects.filter(user=request.user, event=event).delete()
        messages.success(request, "Removed from your favorites.")
    elif action == "save":
        if not event.is_open or event.is_cancelled:
            messages.error(request, "This event is no longer available to save.")
        else:
            FavoriteEvent.objects.get_or_create(user=request.user, event=event)
            messages.success(request, "Added to your favorites.")
    else:
        messages.error(request, "Choose Save or Remove to update your favorites.")
    return redirect(destination)


class EventManageView(LoginRequiredMixin, DetailView):
    """Let an event's host review attendees and registration activity."""

    model = Event
    template_name = "events/manage_event.html"
    context_object_name = "event"

    def get_queryset(self):
        """Only return events owned by the signed-in user."""
        return Event.objects.filter(host=self.request.user)

    def get_context_data(self, **kwargs):
        """Add the selected management tab and its event-specific data."""
        context = super().get_context_data(**kwargs)
        tab = self.request.GET.get("tab", "attendees")
        if tab not in {"attendees", "notifications"}:
            tab = "attendees"
        context["active_tab"] = tab
        attendees = Registration.objects.filter(event=self.object).select_related(
            "user"
        )
        context["attendees"] = attendees
        context["headcount"] = (
            attendees.aggregate(total=Sum("party_size"))["total"] or 0
        )
        notifications = list(
            self.object.notifications.filter(
                recipient=self.request.user,
                notification_type=Notification.Type.REGISTRATION,
            )
        )
        if tab == "notifications":
            unread_ids = [
                notification.pk
                for notification in notifications
                if notification.read_at is None
            ]
            if unread_ids:
                read_at = timezone.now()
                Notification.objects.filter(pk__in=unread_ids).update(read_at=read_at)
                for notification in notifications:
                    if notification.pk in unread_ids:
                        notification.read_at = read_at
        context["registration_notifications"] = notifications
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


@login_required
@require_POST
def register_event(request, pk):
    """Register the current user for an upcoming event."""
    event = get_object_or_404(Event, pk=pk)
    detail_url = reverse("event_detail", args=[event.pk])

    # Return to the page where the user clicked Register.
    next_url = request.POST.get("next")
    if not url_has_allowed_host_and_scheme(
        next_url,
        allowed_hosts={request.get_host()},
        require_https=request.is_secure(),
    ):
        next_url = detail_url

    # Prevent event hosts from registering for their own events.
    if event.host_id == request.user.pk:
        messages.error(
            request,
            "You can't register for your own event. "
            "Use Manage event to see who is coming.",
        )
        return redirect(next_url)

    # Only allow registration for open, non-cancelled events.
    if not event.is_open or event.is_cancelled:
        messages.error(
            request,
            "This event is no longer taking registrations. "
            "Search for another upcoming event instead.",
        )
        return redirect(next_url)

    form = PartySizeForm(request.POST)
    if not form.is_valid():
        messages.error(request, form.errors["party_size"][0])
        return redirect(next_url)
    party_size = form.cleaned_data["party_size"]
    # One-click Register buttons send no party size; only the party size
    # form can change an existing registration.
    changing_size = "party_size" in request.POST

    with transaction.atomic():
        registration, created = Registration.objects.select_for_update().get_or_create(
            event=event,
            user=request.user,
            defaults={"party_size": party_size},
        )
        previous_size = registration.party_size

        if created:
            notify_registration_change(
                event, request.user, registered=True, party_size=party_size
            )
            if party_size == 1:
                messages.success(request, "Registered for this event.")
            else:
                messages.success(request, f"Registered for {party(party_size)}.")
        elif changing_size and party_size != previous_size:
            registration.party_size = party_size
            registration.save(update_fields=["party_size"])
            notify_registration_change(
                event,
                request.user,
                registered=True,
                party_size=party_size,
                previous_size=previous_size,
            )
            messages.success(request, f"Party size updated to {party_size}.")
        elif changing_size:
            messages.info(request, f"Your party size is already {party_size}.")
        else:
            messages.info(request, "You are already registered for this event.")

    return redirect(next_url)


@login_required
@require_POST
def cancel_registration(request, pk):
    """Cancel the current user's registration for an event."""
    event = get_object_or_404(Event, pk=pk)

    next_url = request.POST.get("next")
    if not url_has_allowed_host_and_scheme(
        next_url,
        allowed_hosts={request.get_host()},
        require_https=request.is_secure(),
    ):
        next_url = reverse("event_detail", args=[event.pk])

    with transaction.atomic():
        registration = (
            Registration.objects.select_for_update()
            .filter(event=event, user=request.user)
            .first()
        )
        if registration:
            registration.delete()
            if event.is_open:
                notify_registration_change(
                    event,
                    request.user,
                    registered=False,
                    party_size=registration.party_size,
                )

    if registration:
        messages.success(
            request,
            "Registration cancelled.",
        )
    else:
        messages.info(
            request,
            "You are not registered for this event.",
        )

    return redirect(next_url)


@require_safe
def event_calendar_file(request, pk):
    """Serve an event as an .ics file for Apple Calendar, Outlook, and others.

    The event page links here with ``webcal://``, so calendar apps subscribe
    and keep refreshing this URL. The file therefore stays available after
    the event ends, and cancelled events are marked cancelled. Only events
    without a start time are not found.
    """
    event = get_object_or_404(Event, pk=pk)
    if not has_calendar_file(event):
        raise Http404("This event has no date to add to a calendar.")
    event_url = request.build_absolute_uri(reverse("event_detail", args=[event.pk]))
    response = HttpResponse(
        icalendar_text(event, event_url),
        content_type="text/calendar; charset=utf-8",
    )
    response["Content-Disposition"] = content_disposition_header(
        False, ics_filename(event)
    )
    return response


@method_decorator(never_cache, name="dispatch")
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
        context["share_event_url"] = self.request.build_absolute_uri(
            reverse("event_detail", args=[event.pk])
        )
        context.update(gallery_context(self.request, event=event))
        context["can_upload_photos"] = (
            uploadable_events(user).filter(pk=event.pk).exists()
        )

        # Get all attendance intention records for this event.
        interests = EventInterest.objects.filter(event=event)

        # Count intentions directly from the database.
        context["interest_count"] = interests.count()
        context["interested_count"] = interests.filter(
            status=EventInterest.Status.INTERESTED
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
        context["can_mark_interest"] = (
            is_authenticated and event.is_open and not event.is_cancelled
        )
        unbanned = get_active_ban(user) is None
        context["can_interact"] = context["can_mark_interest"] and unbanned
        context["is_favorite"] = (
            is_authenticated
            and FavoriteEvent.objects.filter(user=user, event=event).exists()
        )
        context["can_save_favorite"] = (
            is_authenticated and unbanned and event.is_open and not event.is_cancelled
        )
        context["can_manage"] = (
            is_authenticated and unbanned and event.host_id == user.pk and event.is_open
        )
        context["host_cancelled"] = event.is_cancelled

        # Check whether this event accepts registrations on our platform.
        context["event_accepts_registration"] = event.is_open and not event.is_cancelled

        # Offer "Add to calendar" for open events that have a start time.
        context["calendar_available"] = can_add_to_calendar(event)
        if context["calendar_available"]:
            build_url = self.request.build_absolute_uri
            context["google_calendar_url"] = google_calendar_url(
                event, build_url(reverse("event_detail", args=[event.pk]))
            )
            context["webcal_url"] = webcal_url(
                build_url(reverse("event_calendar", args=[event.pk]))
            )

        # Get registrations for this event.
        registrations = Registration.objects.filter(event=event).select_related("user")

        context["registered_count"] = registrations.count()

        # Only show attendees who have chosen to share their profiles.
        context["registered_users"] = registrations.filter(
            user__share_profile_with_attendees=True
        )

        # Check whether the current user has registered.
        current_registration = (
            Registration.objects.filter(
                event=event,
                user=user,
            ).first()
            if is_authenticated
            else None
        )

        context["user_is_registered"] = current_registration is not None
        context["current_registration"] = current_registration
        context["party_size_choices"] = range(1, Registration.MAX_PARTY_SIZE + 1)

        # Only the host sees the headcount; party sizes are on the manage page.
        context["is_host"] = is_authenticated and event.host_id == user.pk
        if context["is_host"]:
            context["headcount"] = (
                registrations.aggregate(total=Sum("party_size"))["total"] or 0
            )

        # Only logged-in users who are not the host can register.
        context["can_register"] = (
            is_authenticated
            and unbanned
            and context["event_accepts_registration"]
            and event.host_id != user.pk
            and current_registration is None
        )

        if context["can_manage"]:
            context["recipient_count"] = interested_recipients(event).count()

        self.add_host_rating_context(context, current_registration)

        # Contact requests are per pair of users, even if they began at
        # another event or the current viewer was the recipient.
        request_by_recipient = {}

        if is_authenticated:
            requests = (
                ContactRequest.objects.filter(
                    Q(sender=user) | Q(recipient=user),
                    status__in=(
                        ContactRequest.Status.PENDING,
                        ContactRequest.Status.ACCEPTED,
                    ),
                )
                .select_related("event")
                .order_by("-created_at", "-pk")
            )

            for contact_request in requests:
                other_user_id = (
                    contact_request.recipient_id
                    if contact_request.sender_id == user.pk
                    else contact_request.sender_id
                )
                request_by_recipient.setdefault(other_user_id, contact_request)

        # Include all users who have chosen to share their profiles.
        # Do not exclude the current user: their avatar should also move
        # between the two groups when they change their intention.
        # Do not offer new contact requests to recipients with active bans.
        active_bans = UserBan.objects.filter(
            user_id=OuterRef("user_id"),
            lifted_at__isnull=True,
        )
        active_bans = active_bans.filter(
            Q(expires_at__isnull=True) | Q(expires_at__gt=timezone.now())
        )
        visible_interests = (
            interests.filter(user__share_profile_with_attendees=True)
            .select_related("user")
            .annotate(recipient_banned=Exists(active_bans))
            .order_by("user__display_name", "user__username")
        )

        attendees = [
            {
                "user": interest.user,
                "status": interest.status,
                "contact_request": request_by_recipient.get(interest.user_id),
                "recipient_banned": interest.recipient_banned,
            }
            for interest in visible_interests
        ]

        # Split the same attendee list into the two intention groups.
        context["interested_users"] = [
            attendee
            for attendee in attendees
            if attendee["status"] == EventInterest.Status.INTERESTED
        ]

        context["attendees"] = attendees

        # Discover only opted-in profiles for active, related events.
        context["can_explore_similar_interests"] = (
            is_authenticated
            and unbanned
            and event.is_open
            and not event.is_cancelled
            and bool(event.categories)
        )
        context["similar_interest_users"] = (
            find_similar_interest_users(event, viewer=user)
            if context["can_explore_similar_interests"]
            else []
        )

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

    def add_host_rating_context(self, context, current_registration):
        """Add the host's rating summary and, for attendees, the rating card.

        The card appears for anyone who registered once the event is over,
        even after the rating window closes, so they can still see their own
        rating; ``rating_block_reason`` decides whether the form is offered.
        """
        event = self.object
        user = self.request.user
        if event.host is None:
            return
        context["host_rating"] = host_rating_summary(event.host)
        show_card = (
            current_registration is not None
            and event.host_id != user.pk
            and event.has_ended
            and not event.is_cancelled
        )
        context["show_rating_card"] = show_card
        if not show_card:
            return
        context["my_rating"] = HostRating.objects.filter(
            event=event, rater=user
        ).first()
        context["rating_closed_reason"] = rating_block_reason(event, user)
        context["star_choices"] = range(HostRating.MAX_STARS, 0, -1)
        context["comment_max_length"] = HostRating.COMMENT_MAX_LENGTH
        closes_at = rating_closes_at(event)
        context["rating_closes_at"] = closes_at


# Team 5 members shown on the About us page.
TEAM_MEMBERS = (
    "Aravind",
    "Ananya Agarwal",
    "Antar Espadas",
    "Divyansh Maurya",
    "Lucy Lu",
    "Rakshit Dongre",
    "Xinrui Li",
)


class AboutView(TemplateView):
    """Explain what the site is, where its events come from, and who built it."""

    template_name = "events/about.html"

    def get_context_data(self, **kwargs):
        """Add the team members listed on the page."""
        context = super().get_context_data(**kwargs)
        context["team"] = TEAM_MEMBERS
        return context


class LandingView(TemplateView):
    """Render the home page.

    Signed-in visitors get their personal sections: recommendations, upcoming
    events with registration badges, and archived events. Signed-out visitors
    get a prompt to log in or sign up.
    """

    template_name = "events/home.html"

    def with_registration(self, events):
        """Mark the events the signed-in visitor registered for or rated."""
        if not self.request.user.is_authenticated:
            return events
        return annotate_for_viewer(events, self.request.user)

    def get_context_data(self, **kwargs):
        """Add the search filters and, when signed in, the personal lists."""
        context = super().get_context_data(**kwargs)
        context["filter_form"] = EventFilterForm()
        upcoming = Event.objects.listed().filter(status=Event.Status.UPCOMING)
        upcoming_order = (
            F("start_time").asc(nulls_last=True),
            "end_time",
            "title",
        )

        context["events"] = []
        context["archived_events"] = []
        context["recommendations"] = []
        context["has_search_history"] = False
        if self.request.user.is_authenticated:
            context["has_search_history"] = SearchHistory.objects.filter(
                user=self.request.user
            ).exists()
            context["recommendations"] = recommended_events(self.request.user)

            # Upcoming events, soonest first.
            context["events"] = self.with_registration(
                upcoming.exclude(
                    pk__in=[event.pk for event in context["recommendations"]]
                )
            ).order_by(*upcoming_order)[:UPCOMING_EVENT_LIMIT]

            # Recently completed events, newest first.
            archived = self.with_registration(
                Event.objects.listed().filter(status=Event.Status.DONE)
            ).order_by(
                F("start_time").desc(nulls_last=True),
                "-end_time",
                "-title",
            )
            context["archived_events"] = archived[:ARCHIVED_EVENT_PREVIEW_LIMIT]

        return context


class EventSearchMixin:
    """Share search, filtering, and pagination behavior across event lists."""

    event_status = None
    default_ordering = ()
    empty_without_filters = False
    clear_filters_url_name = None
    search_action_url_name = None

    def get_queryset(self):
        """Filter events by status, keyword, and validated form fields."""
        self.query = self.request.GET.get("q", "").strip()

        form = EventFilterForm(self.request.GET)
        is_valid = form.is_valid()
        filters = form.cleaned_data
        self.filters = filters

        self.filter_params = filter_params(filters)

        if is_valid:
            self.filter_form = EventFilterForm(QueryDict(urlencode(self.filter_params)))
        else:
            self.filter_form = form

        base_queryset = Event.objects.listed().filter(status=self.event_status)

        events = filter_events(base_queryset, filters)

        if self.request.user.is_authenticated:
            events = annotate_for_viewer(events, self.request.user)

        if self.query:
            return search_events(events, self.query)

        if self.empty_without_filters and not self.filter_params:
            return Event.objects.none()

        return events.order_by(*self.default_ordering)

    def get_context_data(self, **kwargs):
        """Preserve search and filter parameters in pagination and forms."""
        context = super().get_context_data(**kwargs)

        search_params = [("q", self.query)] if self.query else []

        context["query"] = self.query
        context["filter_form"] = self.filter_form
        context["has_filters"] = bool(self.filter_params)
        context["page_query"] = urlencode(
            search_params + self.filter_params,
            quote_via=quote,
        )

        if self.search_action_url_name:
            context["search_action"] = reverse(self.search_action_url_name)

        if self.query:
            context["clear_filters_url"] = "?" + urlencode(
                search_params,
                quote_via=quote,
            )
        else:
            context["clear_filters_url"] = reverse(self.clear_filters_url_name)

        return context


class ArchivedEventsView(
    EventSearchMixin,
    LoginRequiredMixin,
    ListView,
):
    """Display and search completed events in the public archive."""

    model = Event
    template_name = "events/archived_events.html"
    context_object_name = "events"
    paginate_by = ARCHIVED_EVENT_PAGE_SIZE

    event_status = Event.Status.DONE

    default_ordering = (
        F("start_time").desc(nulls_last=True),
        "-end_time",
        "-title",
        "-pk",
    )

    empty_without_filters = False
    clear_filters_url_name = "archived_events"
    search_action_url_name = "archived_events"


class SearchView(EventSearchMixin, ListView):
    """Let any visitor search and filter upcoming events.

    Searches typed in plain language, such as "kids events in Brooklyn this
    weekend", are redirected to the keywords and filters they describe, and
    the results page shows how the search was read.
    """

    template_name = "events/search.html"
    context_object_name = "events"
    paginate_by = SEARCH_PAGE_SIZE

    event_status = Event.Status.UPCOMING

    default_ordering = (
        F("start_time").asc(nulls_last=True),
        "end_time",
        "title",
        "pk",
    )
    empty_without_filters = True
    clear_filters_url_name = "landing"

    def get_queryset(self):
        """Record a signed-in search once, without tracking pagination."""
        events = super().get_queryset()
        if (
            self.request.user.is_authenticated
            and (self.query or self.filter_params)
            and "page" not in self.request.GET
        ):
            record_search(
                self.request.user,
                self.query,
                self.filter_params,
            )
        return events

    def get(self, request, *args, **kwargs):
        """Send a plain-language search to the filters it describes."""
        target = natural_search_url(request)
        if target:
            return redirect(target)
        return super().get(request, *args, **kwargs)

    def get_context_data(self, **kwargs):
        """Explain how a plain-language search was read."""
        context = super().get_context_data(**kwargs)
        asked = " ".join(self.request.GET.get("asked", "").split())
        if asked:
            context["asked"] = asked[:MAX_PROMPT_LENGTH]
            context["understood"] = describe_search(self.query, self.filters)
        return context


@login_required
@require_POST
def clear_search_history(request):
    """Delete the current user's stored event searches."""
    SearchHistory.objects.filter(user=request.user).delete()
    return redirect("landing")
