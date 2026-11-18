"""Grouped moderation queue, using the existing report lifecycle."""

from urllib.parse import urlencode

from django.contrib import admin, messages
from django.core.exceptions import PermissionDenied
from django.core.paginator import Paginator
from django.db.models import (
    BigIntegerField,
    Case,
    Count,
    Exists,
    F,
    Max,
    OuterRef,
    Q,
    Value,
    When,
)
from django.http import HttpResponseRedirect
from django.shortcuts import get_object_or_404
from django.template.response import TemplateResponse
from django.urls import path, reverse
from django.utils import timezone
from django.views.decorators.http import require_GET, require_POST

from .models import Report
from .services import TARGET_FIELDS

STATUS_OPTIONS = (("open", "Unresolved"), ("resolved", "Resolved"), ("all", "All"))
SORT_OPTIONS = (
    ("newest", "Newest activity"),
    ("oldest", "Oldest activity"),
    ("most", "Most reports"),
    ("fewest", "Fewest reports"),
)
SORT_ORDER = {
    "newest": "-latest",
    "oldest": "latest",
    "most": "-report_count",
    "fewest": "report_count",
}


def open_chat_reports():
    """Only a report for this conversation can authorize private review."""
    return Report.objects.filter(
        kind=Report.Kind.USER, conversation__isnull=False, resolved_at__isnull=True
    )


def feed_options(request):
    """Accept only known filters, keeping action redirects local."""
    status = request.GET.get("status", "open")
    sort = request.GET.get("sort", "newest")
    return (
        status if status in dict(STATUS_OPTIONS) else "open",
        sort if sort in SORT_ORDER else "newest",
    )


def target_filter(report):
    """Missing targets are singletons; context fields never define identity."""
    field = TARGET_FIELDS[report.kind]
    target_id = getattr(report, f"{field}_id")
    if target_id is None:
        return Q(pk=report.pk)
    return Q(kind=report.kind, **{f"{field}_id": target_id})


def target_summary(report, request, site):
    """Build live descriptions and links without accessing chat content."""
    target = getattr(report, TARGET_FIELDS[report.kind])
    result = {"summary": "", "admin_url": None, "public_url": None}
    if target is None:
        result["summary"] = "Deleted target"
        if report.event_title:
            result["summary"] += f" — {report.event_title}"
        return result
    if report.kind == Report.Kind.EVENT:
        result["summary"] = target.title
        result["public_url"] = reverse("event_detail", args=[target.pk])
    elif report.kind in (Report.Kind.POST, Report.Kind.REPLY):
        event = target.event if report.kind == Report.Kind.POST else target.post.event
        result["summary"] = (
            f"{target.author.get_display_name()} on {event.title}: {target.body[:240]}"
        )
        anchor = "comment" if report.kind == Report.Kind.POST else "reply"
        result["public_url"] = (
            reverse("event_detail", args=[event.pk]) + f"#{anchor}-{target.pk}"
        )
    elif report.kind == Report.Kind.RATING:
        result["summary"] = (
            f"{target.stars}/5 for {target.host.get_display_name()} "
            f"on {target.event.title}: {target.comment[:240]}"
        )
        result["public_url"] = (
            reverse("public_profile", args=[target.host.username])
            + f"#rating-{target.pk}"
        )
    else:
        result["summary"] = f"{target.get_display_name()} (@{target.username})"
        result["public_url"] = reverse("public_profile", args=[target.username])
    model_admin = site._registry.get(type(target))
    if model_admin and model_admin.has_view_or_change_permission(request, target):
        opts = target._meta
        result["admin_url"] = reverse(
            f"{site.name}:{opts.app_label}_{opts.model_name}_change", args=[target.pk]
        )
    return result


@admin.register(Report)
class ReportAdmin(admin.ModelAdmin):
    """Reports are submitted on the site and resolved only through the queue."""

    def has_add_permission(self, request):
        return False

    def has_delete_permission(self, request, obj=None):
        return False

    def get_urls(self):
        return [
            path(
                "<int:report_id>/conversation/",
                self.admin_site.admin_view(require_GET(self.conversation_view)),
                name="reports_report_conversation",
            ),
            path(
                "<int:report_id>/resolve/",
                self.admin_site.admin_view(require_POST(self.resolve_report)),
                name="reports_report_resolve",
            ),
            path(
                "<int:report_id>/resolve-all/",
                self.admin_site.admin_view(require_POST(self.resolve_all)),
                name="reports_report_resolve_all",
            ),
        ] + super().get_urls()

    def change_view(self, request, object_id, form_url="", extra_context=None):
        # The report model is never editable through a generic admin form.
        if not self.has_view_permission(request):
            raise PermissionDenied
        return HttpResponseRedirect(self.feed_url(request))

    def feed_url(self, request):
        status, sort = feed_options(request)
        params = {"status": status, "sort": sort, "page": request.GET.get("page", "1")}
        return reverse(f"{self.admin_site.name}:reports_report_changelist") + (
            "?" + urlencode(params)
        )

    def resolve(self, request, report_id, *, all_reports):
        if not self.has_change_permission(request):
            raise PermissionDenied
        report = get_object_or_404(Report, pk=report_id)
        predicate = target_filter(report) if all_reports else Q(pk=report.pk)
        count = Report.objects.filter(predicate, resolved_at__isnull=True).update(
            resolved_at=timezone.now(), resolved_by=request.user
        )
        self.message_user(request, f"Resolved {count} report(s).", messages.SUCCESS)
        return HttpResponseRedirect(self.feed_url(request))

    def resolve_report(self, request, report_id):
        return self.resolve(request, report_id, all_reports=False)

    def resolve_all(self, request, report_id):
        return self.resolve(request, report_id, all_reports=True)

    def conversation_view(self, request, report_id):
        """Review live messages without invoking participant-side read receipts."""
        if not self.has_view_permission(request):
            raise PermissionDenied
        report = get_object_or_404(
            Report.objects.select_related(
                "conversation__user_one", "conversation__user_two"
            ).annotate(
                can_review_conversation=Exists(
                    open_chat_reports().filter(
                        conversation_id=OuterRef("conversation_id")
                    )
                )
            ),
            pk=report_id,
            kind=Report.Kind.USER,
            conversation__isnull=False,
            can_review_conversation=True,
        )
        thread = report.conversation
        thread_messages = thread.messages.select_related("sender").order_by(
            "created_at", "pk"
        )
        context_message = thread.messages.filter(pk=report.context_message_id).first()
        paginator = Paginator(thread_messages, 50)
        context_page = None
        if context_message is not None:
            preceding = thread.messages.filter(
                Q(created_at__lt=context_message.created_at)
                | Q(created_at=context_message.created_at, pk__lt=context_message.pk)
            ).count()
            context_page = preceding // paginator.per_page + 1
        status, sort = feed_options(request)
        feed_query = urlencode(
            {"status": status, "sort": sort, "page": request.GET.get("page", "1")}
        )
        if "message_page" not in request.GET:
            initial_page = context_page or paginator.num_pages
            anchor = f"#message-{context_message.pk}" if context_message else ""
            return HttpResponseRedirect(
                f"{request.path}?{feed_query}&message_page={initial_page}{anchor}"
            )
        page = paginator.get_page(request.GET.get("message_page"))
        request.current_app = self.admin_site.name
        return TemplateResponse(
            request,
            "admin/reports/report/conversation.html",
            {
                **self.admin_site.each_context(request),
                "title": "Conversation review",
                "opts": self.model._meta,
                "report": report,
                "conversation": thread,
                "message_page": page,
                "context_message": context_message,
                "context_page": context_page,
                "feed_url": self.feed_url(request),
                "feed_query": feed_query,
            },
        )

    def changelist_view(self, request, extra_context=None):
        if not self.has_view_permission(request):
            raise PermissionDenied
        status, sort = feed_options(request)
        match = {
            "open": Q(resolved_at__isnull=True),
            "resolved": Q(resolved_at__isnull=False),
            "all": Q(),
        }[status]
        # Null references cannot safely be grouped together after target deletion.
        targets = Report.objects.annotate(
            target_id=Case(
                *[
                    When(kind=kind, then=F(f"{field}_id"))
                    for kind, field in TARGET_FIELDS.items()
                ],
                output_field=BigIntegerField(),
            )
        ).annotate(
            orphan_id=Case(
                When(target_id__isnull=True, then=F("pk")),
                default=Value(None),
                output_field=BigIntegerField(),
            )
        )
        grouped = (
            targets.order_by()
            .values("kind", "target_id", "orphan_id")
            .annotate(
                report_count=Count("pk", filter=match),
                open_count=Count("pk", filter=Q(resolved_at__isnull=True)),
                latest=Max("created_at", filter=match),
            )
            .filter(report_count__gt=0)
            .order_by(SORT_ORDER[sort], "kind", "target_id", "orphan_id")
        )
        page = Paginator(grouped, 25).get_page(request.GET.get("page"))
        groups = list(page.object_list)
        predicates = Q(pk__in=[])
        by_key = {}
        for group in groups:
            key = (group["kind"], group["target_id"], group["orphan_id"])
            by_key[key] = group
            group["reports"] = []
            if group["orphan_id"] is not None:
                predicates |= Q(pk=group["orphan_id"])
            else:
                predicates |= Q(
                    kind=group["kind"],
                    **{f"{TARGET_FIELDS[group['kind']]}_id": group["target_id"]},
                )
        reports = (
            targets.filter(predicates)
            .filter(match)
            .annotate(
                can_review_conversation=Case(
                    When(
                        kind=Report.Kind.USER,
                        then=Exists(
                            open_chat_reports().filter(
                                conversation_id=OuterRef("conversation_id")
                            )
                        ),
                    ),
                    default=Value(False),
                )
            )
            .select_related(
                "reporter",
                "resolved_by",
                "event",
                "reported_user",
                "reported_post__author",
                "reported_post__event",
                "reported_reply__author",
                "reported_reply__post__event",
                "reported_rating__host",
                "reported_rating__event",
            )
        )
        for report in reports:
            key = (report.kind, report.target_id, report.orphan_id)
            group = by_key.get(key)
            if group is not None:
                group["reports"].append(report)
        # Reports may be resolved or removed between aggregation and retrieval.
        groups = [group for group in groups if group["reports"]]
        for group in groups:
            representative = group["reports"][0]
            group.update(target_summary(representative, request, self.admin_site))
            group["label"] = representative.get_kind_display()
            group["representative"] = representative
        context = {
            **self.admin_site.each_context(request),
            "title": "Reports",
            "opts": self.model._meta,
            "groups": groups,
            "page_obj": page,
            "status": status,
            "sort": sort,
            "status_options": STATUS_OPTIONS,
            "sort_options": SORT_OPTIONS,
            "filter_query": urlencode({"status": status, "sort": sort}),
            "can_resolve": self.has_change_permission(request),
            **(extra_context or {}),
        }
        return TemplateResponse(request, "admin/reports/report/feed.html", context)
