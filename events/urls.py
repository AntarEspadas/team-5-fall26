"""URL patterns for hosted events and event community actions."""

from django.urls import path

from reports.views import report_event, report_post, report_rating, report_reply

from . import community_views
from . import photo_views
from .rating_views import rate_host
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
    remove_attendee,
    set_event_favorite,
    ArchivedEventsView,
)

urlpatterns = [
    path("photos/upload/", photo_views.upload_photos, name="event_photo_upload"),
    path(
        "photos/<int:photo_id>/image/",
        photo_views.photo_image,
        name="event_photo_image",
    ),
    path(
        "photos/<int:photo_id>/visibility/",
        photo_views.photo_visibility,
        name="event_photo_visibility",
    ),
    path(
        "photos/<int:photo_id>/delete/",
        photo_views.delete_photo,
        name="event_photo_delete",
    ),
    path(
        "search-history/clear/",
        clear_search_history,
        name="clear_search_history",
    ),
    path("new/", EventCreateView.as_view(), name="event_create"),
    path("mine/", MyEventsView.as_view(), name="my_events"),
    path("<int:pk>/favorite/", set_event_favorite, name="event_favorite"),
    path("<int:pk>/manage/", EventManageView.as_view(), name="event_manage"),
    path("<int:pk>/edit/", EventUpdateView.as_view(), name="event_edit"),
    path("<int:pk>/notify/", notify_attendees, name="event_notify"),
    path("<int:pk>/cancel/", cancel_event, name="event_cancel"),
    path("<int:pk>/register/", register_event, name="event_register"),
    path(
        "<int:pk>/attendees/<int:registration_id>/remove/",
        remove_attendee,
        name="event_remove_attendee",
    ),
    path("<int:pk>/rate-host/", rate_host, name="rate_host"),
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
    path("ratings/<int:rating_id>/report/", report_rating, name="host_rating_report"),
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
