"""Consistent HTML and JSON responses for submissions blocked by a ban."""

from django.http import JsonResponse
from django.shortcuts import render

BAN_MESSAGE = (
    "Your account is currently banned. You can browse the site and mark events "
    "as Interested, but other submissions are disabled."
)


def banned_response(request):
    """Explain the restriction without redirecting or processing the submission."""
    if (
        request.content_type == "application/json"
        or request.headers.get("X-Requested-With") == "XMLHttpRequest"
        or request.get_preferred_type(["text/html", "application/json"])
        == "application/json"
    ):
        return JsonResponse({"error": BAN_MESSAGE, "code": "user_banned"}, status=403)
    return render(
        request, "accounts/banned.html", {"ban_message": BAN_MESSAGE}, status=403
    )
