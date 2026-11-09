"""Middleware for session expiry and banned-user submission restrictions."""

from django.utils.deprecation import MiddlewareMixin

from .ban_responses import banned_response
from .ban_services import get_active_ban


class BanMiddleware(MiddlewareMixin):
    """Deny member submissions by default while a ban is active.

    Resolve exceptions by the full route name, including any namespace.
    New write endpoints are restricted without needing individual guards.
    """

    safe_methods = frozenset({"GET", "HEAD", "OPTIONS", "TRACE"})
    authentication_routes = frozenset(
        {
            "login",
            "logout",
            "password_reset",
            "password_reset_confirm",
            "account_login",
            "account_logout",
            "account_reauthenticate",
            "account_reset_password",
            "account_reset_password_from_key",
            "google_login",
            "google_callback",
            "google_login_by_token",
        }
    )

    def process_view(self, request, view_func, view_args, view_kwargs):
        if request.method in self.safe_methods or not request.user.is_authenticated:
            return None

        route = request.resolver_match.view_name
        if request.method == "POST" and route in self.authentication_routes:
            return None
        if get_active_ban(request.user) is None:
            return None

        # This handler checks the ban itself and permits only Interested.
        if request.method == "POST" and route == "toggle_event_interest":
            return None
        return banned_response(request)


class SlidingSessionMiddleware(MiddlewareMixin):
    """
    Reset session expiry timer to 48 hours on every request from authenticated users.
    Implements sliding idle session: countdown restarts from last user activity.
    """

    def process_request(self, request):
        if request.user.is_authenticated:
            # 48 hours in seconds
            request.session.set_expiry(48 * 60 * 60)
