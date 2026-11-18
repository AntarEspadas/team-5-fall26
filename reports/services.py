"""Create reports with server-derived targets and independent duplicate rules."""

from django.db import transaction

from .models import Report

TARGET_FIELDS = {
    Report.Kind.EVENT: "event",
    Report.Kind.POST: "reported_post",
    Report.Kind.REPLY: "reported_reply",
    Report.Kind.USER: "reported_user",
    Report.Kind.PROFILE: "reported_user",
    Report.Kind.RATING: "reported_rating",
}


def unresolved_report_lookup(reporter, kind, target):
    """Identify the unresolved report for this specific kind and target."""
    return {
        "reporter": reporter,
        "kind": kind,
        TARGET_FIELDS[kind]: target,
        "resolved_at": None,
    }


@transaction.atomic
def record_report(reporter, kind, target, cleaned_data, *, conversation=None):
    """Keep the first unresolved report, including its original context."""
    defaults = {
        "reason": cleaned_data["reason"],
        "details": cleaned_data["details"],
    }
    if kind == Report.Kind.USER:
        defaults["conversation"] = conversation
        defaults["context_message"] = conversation.messages.order_by(
            "-created_at", "-pk"
        ).first()
    elif kind != Report.Kind.PROFILE:
        if kind == Report.Kind.EVENT:
            event = target
        elif kind == Report.Kind.RATING:
            event = target.event
            defaults["event"] = event
            defaults["reported_user"] = target.rater
        else:
            event = target.event if kind == Report.Kind.POST else target.post.event
            defaults["event"] = event
            defaults["reported_user"] = target.author
        defaults["event_title"] = event.title

    # Each lookup is backed by a conditional unique constraint, so concurrent
    # submissions return the winning record without replacing its details.
    return Report.objects.get_or_create(
        **unresolved_report_lookup(reporter, kind, target), defaults=defaults
    )
