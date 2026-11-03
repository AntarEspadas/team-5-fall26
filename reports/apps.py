"""App configuration for reports."""

from django.apps import AppConfig


class ReportsConfig(AppConfig):
    """Register reports independently of the content being reported."""

    default_auto_field = "django.db.models.BigAutoField"
    name = "reports"
