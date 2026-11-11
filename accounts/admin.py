"""Admin site configuration for user accounts."""

from django.contrib import admin
from django.contrib.auth.admin import UserAdmin as DjangoUserAdmin
from django.core import signing
from django.core.exceptions import PermissionDenied
from django.db import transaction
from django.http import HttpResponseBadRequest
from django.shortcuts import get_object_or_404, redirect
from django.template.response import TemplateResponse
from django.urls import path, reverse
from django.utils import timezone
from django.views.decorators.http import require_http_methods

from .ban_services import FIRST_BAN_DURATION, apply_ban, get_active_ban, lift_ban
from .models import User, UserBan

CONFIRMATION_SALT = "accounts.admin.ban_confirmation"
STALE_CONFIRMATION = (
    "This confirmation is out of date. Return to the user page "
    "and review the current ban status."
)


class UserBanInline(admin.TabularInline):
    """Show offense history only through the user admin, without editable forms."""

    model = UserBan
    fk_name = "user"
    extra = 0
    can_delete = False
    fields = readonly_fields = (
        "offense_number",
        "started_at",
        "expires_at",
        "issued_by",
        "lifted_at",
        "lifted_by",
    )
    verbose_name_plural = "Ban history"

    def has_view_permission(self, request, obj=None):
        return request.user.has_perm("accounts.view_user") or request.user.has_perm(
            "accounts.change_user"
        )

    def has_add_permission(self, request, obj=None):
        return False

    def has_change_permission(self, request, obj=None):
        return False

    def has_delete_permission(self, request, obj=None):
        return False


@admin.register(User)
class UserAdmin(DjangoUserAdmin):
    """Django's user admin, with the email address and display name added."""

    fieldsets = (
        (None, {"fields": ("username", "password")}),
        (
            "Personal info",
            {"fields": ("display_name", "first_name", "last_name", "email")},
        ),
        *DjangoUserAdmin.fieldsets[2:],
        ("Moderation", {"fields": ("ban_status",)}),
    )
    add_fieldsets = (
        (
            None,
            {
                "classes": ("wide",),
                "fields": (
                    "username",
                    "email",
                    "display_name",
                    "usable_password",
                    "password1",
                    "password2",
                ),
            },
        ),
    )
    list_display = ("username", "email", "display_name", "is_staff")
    search_fields = ("username", "display_name", "email")
    readonly_fields = ("ban_status",)
    inlines = (UserBanInline,)
    change_form_template = "admin/accounts/user/change_form.html"

    def get_inlines(self, request, obj=None):
        return self.inlines if obj is not None else ()

    @admin.display(description="Current ban status")
    def ban_status(self, obj):
        ban = get_active_ban(obj)
        if ban is None:
            return "Not banned"
        if ban.expires_at is None:
            return "Banned indefinitely"
        return f"Banned until {timezone.localtime(ban.expires_at):%Y-%m-%d %H:%M:%S %Z}"

    def get_urls(self):
        view = self.admin_site.admin_view(
            require_http_methods(["GET", "POST"])(self.ban_confirmation)
        )
        return [
            path(
                "<int:user_id>/ban/", view, {"action": "ban"}, name="accounts_user_ban"
            ),
            path(
                "<int:user_id>/lift-ban/",
                view,
                {"action": "lift"},
                name="accounts_user_lift_ban",
            ),
        ] + super().get_urls()

    def change_view(self, request, object_id, form_url="", extra_context=None):
        target = self.get_object(request, object_id)
        context = dict(extra_context or {})
        if target is not None:
            eligible = not (target.is_staff or target.is_superuser)
            allowed = self.has_change_permission(request, target) and eligible
            ban = get_active_ban(target)
            context.update(
                ban_url=(
                    reverse(
                        f"{self.admin_site.name}:accounts_user_ban", args=[target.pk]
                    )
                    if allowed and ban is None
                    else None
                ),
                lift_ban_url=(
                    reverse(
                        f"{self.admin_site.name}:accounts_user_lift_ban",
                        args=[target.pk],
                    )
                    if allowed and ban is not None
                    else None
                ),
            )
        return super().change_view(request, object_id, form_url, context)

    @transaction.atomic
    def ban_confirmation(self, request, user_id, action):
        """Preview moderation; serialize confirmation checks with lifecycle changes."""
        if not request.user.is_staff or not self.has_change_permission(request):
            raise PermissionDenied
        users = self.get_queryset(request)
        if request.method == "POST":
            users = users.select_for_update()
        target = get_object_or_404(users, pk=user_id)
        if (
            not self.has_change_permission(request, target)
            or target.is_staff
            or target.is_superuser
        ):
            raise PermissionDenied

        active_ban = get_active_ban(target)
        latest_ban = target.bans.first()
        state = {
            "user": target.pk,
            "admin": request.user.pk,
            "action": action,
            "latest_ban": latest_ban.pk if latest_ban else None,
            "active_ban": active_ban.pk if active_ban else None,
        }
        available = active_ban is None if action == "ban" else active_ban is not None
        error = ""
        if request.method == "POST":
            try:
                confirmed = signing.loads(
                    request.POST.get("confirmation_token", ""), salt=CONFIRMATION_SALT
                )
            except signing.BadSignature:
                return HttpResponseBadRequest(
                    "Invalid ban confirmation. Open a new confirmation "
                    "from the user page."
                )
            if confirmed != state or not available:
                error = STALE_CONFIRMATION
            else:
                if action == "ban":
                    ban, changed = apply_ban(request.user, target)
                    message = f"Applied ban for offense {ban.offense_number}."
                else:
                    ban, changed = lift_ban(request.user, active_ban)
                    message = f"Lifted ban for offense {ban.offense_number}."
                if changed:
                    self.log_change(request, target, message)
                    self.message_user(request, message)
                    return redirect(
                        f"{self.admin_site.name}:accounts_user_change", target.pk
                    )
                error = STALE_CONFIRMATION

        request.current_app = self.admin_site.name
        return TemplateResponse(
            request,
            "admin/accounts/user/ban_confirmation.html",
            {
                **self.admin_site.each_context(request),
                "opts": self.model._meta,
                "original": target,
                "title": "Ban user" if action == "ban" else "Lift ban",
                "action": action,
                "active_ban": active_ban,
                "next_offense": latest_ban.offense_number + 1 if latest_ban else 1,
                "duration_hours": int(FIRST_BAN_DURATION.total_seconds() / 3600),
                "confirmation_token": (
                    signing.dumps(state, salt=CONFIRMATION_SALT)
                    if available and not error
                    else None
                ),
                "change_url": reverse(
                    f"{self.admin_site.name}:accounts_user_change", args=[target.pk]
                ),
                "error": error,
            },
            status=409 if error else 200,
        )
