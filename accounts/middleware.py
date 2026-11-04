"""Middleware for sliding session expiry."""

from django.utils.deprecation import MiddlewareMixin


class SlidingSessionMiddleware(MiddlewareMixin):
    """
    Reset session expiry timer to 48 hours on every request from authenticated users.
    Implements sliding idle session: countdown restarts from last user activity.
    """

    def process_request(self, request):
        if request.user.is_authenticated:
            # 48 hours in seconds
            request.session.set_expiry(48 * 60 * 60)
