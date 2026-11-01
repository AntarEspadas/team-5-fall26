"""URL patterns for hosted events and event community actions."""

from django.urls import path

from . import community_views
from .views import (
    EventCreateView,
    EventUpdateView,
    MyEventsView,
    cancel_event,
    notify_attendees,
)

urlpatterns = [
    path("new/", EventCreateView.as_view(), name="event_create"),
    path("mine/", MyEventsView.as_view(), name="my_events"),
    path("<int:pk>/edit/", EventUpdateView.as_view(), name="event_edit"),
    path("<int:pk>/notify/", notify_attendees, name="event_notify"),
    path("<int:pk>/cancel/", cancel_event, name="event_cancel"),
    path(
        "<int:event_id>/interest/",
        community_views.toggle_event_interest,
        name="toggle_event_interest",
    ),
    path(
        "<int:event_id>/posts/",
        community_views.create_event_post,
        name="create_event_post",
    ),
    path(
        "posts/<int:post_id>/replies/",
        community_views.create_event_reply,
        name="create_event_reply",
    ),
    path(
        "<int:event_id>/contact/<int:user_id>/",
        community_views.send_contact_request,
        name="send_contact_request",
    ),
    path(
        "contact-requests/<int:request_id>/<str:action>/",
        community_views.respond_to_contact_request,
        name="respond_to_contact_request",
    ),
]
