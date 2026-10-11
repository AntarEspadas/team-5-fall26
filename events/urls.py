"""URL patterns for event community actions."""

from django.urls import path

from . import community_views

urlpatterns = [
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
