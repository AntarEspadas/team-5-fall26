"""Shared submission flow for events, public comments, chat users, and profiles."""

from django.contrib import messages
from django.contrib.auth import get_user_model
from django.contrib.auth.decorators import login_required
from django.http import Http404
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.views.decorators.http import require_http_methods

from events.conversation_services import get_accessible_conversation
from events.models import Event, EventPost, EventReply, HostRating

from .forms import ReportForm
from .models import Report
from .services import record_report, unresolved_report_lookup

TARGET_LABELS = {
    Report.Kind.EVENT: "event",
    Report.Kind.POST: "comment",
    Report.Kind.REPLY: "reply",
    Report.Kind.USER: "user",
    Report.Kind.PROFILE: "profile",
    Report.Kind.RATING: "rating",
}


@login_required
@require_http_methods(["GET", "POST"])
def report_event(request, pk):
    """Record one unresolved report per user and event, in any event state."""
    event = get_object_or_404(Event, pk=pk)
    return _report(
        request,
        kind=Report.Kind.EVENT,
        target=event,
        subject=event.title,
        back_url=reverse("event_detail", args=[event.pk]),
        back_label="event",
    )


@login_required
@require_http_methods(["GET", "POST"])
def report_post(request, post_id):
    """Report a public comment and associate its author with the report."""
    post = get_object_or_404(
        EventPost.objects.select_related("event", "author"), pk=post_id
    )
    return _report_comment(request, Report.Kind.POST, post, post.event)


@login_required
@require_http_methods(["GET", "POST"])
def report_reply(request, reply_id):
    """Report a public reply independently of its parent comment."""
    reply = get_object_or_404(
        EventReply.objects.select_related("post__event", "author"), pk=reply_id
    )
    return _report_comment(request, Report.Kind.REPLY, reply, reply.post.event)


@login_required
@require_http_methods(["GET", "POST"])
def report_rating(request, rating_id):
    """Report a host rating's comment without revealing who wrote it."""
    rating = get_object_or_404(
        HostRating.objects.select_related("event", "host").exclude(comment=""),
        pk=rating_id,
    )
    if rating.rater_id == request.user.pk:
        raise Http404("Rating not found.")
    return _report(
        request,
        kind=Report.Kind.RATING,
        target=rating,
        subject=f"A rating of {rating.host.get_display_name()}",
        back_url=reverse("public_profile", args=[rating.host.username]),
        back_label="profile",
    )


def _report_comment(request, kind, comment, event):
    if comment.author_id == request.user.pk:
        raise Http404("Comment not found.")
    return _report(
        request,
        kind=kind,
        target=comment,
        subject=comment.author.get_display_name(),
        back_url=reverse("event_detail", args=[event.pk]),
        back_label="event",
        comment=comment,
    )


@login_required
@require_http_methods(["GET", "POST"])
def report_conversation_user(request, conversation_id):
    """Report the other participant with the latest message as context."""
    thread = get_accessible_conversation(request.user, conversation_id)
    other_user = thread.other_participant(request.user)
    return _report(
        request,
        kind=Report.Kind.USER,
        target=other_user,
        subject=other_user.get_display_name(),
        back_url=reverse("conversation", args=[thread.pk]),
        back_label="conversation",
        conversation=thread,
    )


@login_required
@require_http_methods(["GET", "POST"])
def report_profile(request, username):
    """Report another member's shared profile, independently of chat reports."""
    profile_user = get_object_or_404(
        get_user_model()
        .objects.filter(share_profile_with_attendees=True)
        .exclude(pk=request.user.pk),
        username=username,
    )
    return _report(
        request,
        kind=Report.Kind.PROFILE,
        target=profile_user,
        subject=f"{profile_user.get_display_name()} (@{profile_user.username})",
        back_url=reverse("public_profile", args=[profile_user.username]),
        back_label="profile",
    )


def _report(
    request,
    *,
    kind,
    target,
    subject,
    back_url,
    back_label,
    comment=None,
    conversation=None,
):
    """Validate the common fields and redirect to the target's source page."""
    label = TARGET_LABELS[kind]
    already_reported = f"You have already reported this {label}."
    if Report.objects.filter(
        **unresolved_report_lookup(request.user, kind, target)
    ).exists():
        messages.info(request, already_reported)
        return redirect(back_url)

    form = ReportForm(request.POST if request.method == "POST" else None)
    if form.is_bound and form.is_valid():
        _, created = record_report(
            request.user, kind, target, form.cleaned_data, conversation=conversation
        )
        if created:
            messages.success(request, "Report sent. Our moderators will review it.")
        else:
            messages.info(request, already_reported)
        return redirect(back_url)

    return render(
        request,
        "reports/report_form.html",
        {
            "form": form,
            "target_label": label,
            "subject": subject,
            "back_url": back_url,
            "back_label": back_label,
            "comment": comment,
            "is_conversation_report": conversation is not None,
        },
    )
