"""Admin site configuration for user accounts."""

from django.contrib import admin
from django.contrib.auth.admin import UserAdmin as DjangoUserAdmin

from .models import User


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
