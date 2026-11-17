"""URL patterns for hosted events and event community actions."""

from django.urls import path

from reports.views import report_event, report_post, report_reply

from . import community_views
from .views import (
    EventCreateView,
    EventManageView,
    EventUpdateView,
    MyEventsView,
    cancel_event,
    cancel_registration,
    clear_search_history,
    event_calendar_file,
    notify_attendees,
    register_event,
    ArchivedEventsView,
)

urlpatterns = [
    path(
        "search-history/clear/",
        clear_search_history,
        name="clear_search_history",
    ),
    path("new/", EventCreateView.as_view(), name="event_create"),
    path("mine/", MyEventsView.as_view(), name="my_events"),
    path("<int:pk>/manage/", EventManageView.as_view(), name="event_manage"),
    path("<int:pk>/edit/", EventUpdateView.as_view(), name="event_edit"),
    path("<int:pk>/notify/", notify_attendees, name="event_notify"),
    path("<int:pk>/cancel/", cancel_event, name="event_cancel"),
    path("<int:pk>/register/", register_event, name="event_register"),
    path("<int:pk>/calendar.ics", event_calendar_file, name="event_calendar"),
    path(
        "<int:pk>/cancel-registration/",
        cancel_registration,
        name="event_cancel_registration",
    ),
    path("archived/", ArchivedEventsView.as_view(), name="archived_events"),
    path("<int:pk>/report/", report_event, name="event_report"),
    path("posts/<int:post_id>/report/", report_post, name="event_post_report"),
    path("replies/<int:reply_id>/report/", report_reply, name="event_reply_report"),
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
