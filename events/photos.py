"""Photo audiences, upload eligibility, storage, and gallery pagination."""

import logging
from uuid import uuid4

from django.conf import settings
from django.core.exceptions import ImproperlyConfigured, ValidationError
from django.core.files.storage import storages
from django.core.paginator import Paginator
from django.db import transaction
from django.db.models import F, Q
from django.utils import timezone

from accounts.ban_services import get_active_ban, require_unbanned_user

from .models import Event, EventPhoto

logger = logging.getLogger(__name__)
MAX_PHOTOS = 10
MAX_PHOTO_BYTES = 5 * 1024 * 1024
MAX_PHOTO_SIDE = 4096
PHOTO_PAGE_SIZE = 24


def uploadable_events(user):
    """Registration or hosting allows uploads once a noncancelled event starts."""
    if not user.is_authenticated or get_active_ban(user) is not None:
        return Event.objects.none()
    return (
        Event.objects.filter(
            Q(host=user) | Q(registrations__user=user),
            start_time__lte=timezone.now(),
        )
        .exclude(status=Event.Status.CANCELLED)
        .exclude(title__iregex=r"^\s*(CANCELLED|CANCELED)")
        .distinct()
        .order_by("-start_time", "title", "pk")
    )


def photo_storage(backend):
    """Resolve a photo's recorded backend without a fallback to another store."""
    if backend not in EventPhoto.StorageBackend.values:
        raise ImproperlyConfigured("Unknown event photo storage backend.")
    if backend == EventPhoto.StorageBackend.S3 and not settings.EVENT_PHOTO_S3_BUCKET:
        raise ImproperlyConfigured(
            "EVENT_PHOTO_S3_BUCKET is required to read S3 photos."
        )
    return storages[f"event_photos_{backend}"]


def delete_stored_photo(backend, image_key):
    """Best-effort cleanup never makes a committed deletion fail."""
    try:
        photo_storage(backend).delete(image_key)
    except Exception:
        logger.exception("Could not delete event photo from %s: %s", backend, image_key)


def save_photo_batch(*, user, event, photos, visibility):
    """Save validated files together, removing stored objects after any failure."""
    require_unbanned_user(user)
    if not uploadable_events(user).filter(pk=event.pk).exists():
        raise ValidationError("You can no longer upload photos for this event.")
    backend = settings.EVENT_PHOTO_STORAGE
    storage = photo_storage(backend)
    saved_keys = []
    result = []
    try:
        with transaction.atomic():
            for image in photos:
                key = storage.save(f"{event.pk}/{uuid4().hex}.jpg", image)
                saved_keys.append(key)
                result.append(
                    EventPhoto.objects.create(
                        event=event,
                        uploader=user,
                        visibility=visibility,
                        storage_backend=backend,
                        image_key=key,
                    )
                )
    except Exception:
        for key in saved_keys:
            delete_stored_photo(backend, key)
        raise
    return result


def gallery_context(request, *, event=None, uploader=None):
    """Paginate only accessible pictures, with stable event grouping on profiles."""
    photos = EventPhoto.objects.visible_to(request.user).select_related(
        "event", "uploader"
    )
    if event is not None:
        photos = photos.filter(event=event)
    if uploader is not None:
        photos = photos.filter(uploader=uploader).order_by(
            F("event__start_time").desc(nulls_last=True),
            "-event_id",
            "-created_at",
            "-pk",
        )
    page = Paginator(photos, PHOTO_PAGE_SIZE).get_page(request.GET.get("photo_page"))
    # Preserve unrelated page parameters, and attach a fragment to keep navigation
    # at the gallery rather than jumping back to the top of long event pages.
    params = request.GET.copy()
    links = {}
    if page.has_previous():
        params["photo_page"] = page.previous_page_number()
        links["photo_previous_url"] = f"?{params.urlencode()}#photos"
    if page.has_next():
        params["photo_page"] = page.next_page_number()
        links["photo_next_url"] = f"?{params.urlencode()}#photos"
    return {
        "photo_page": page,
        "photo_grouped": uploader is not None,
        "photo_visibility_choices": EventPhoto.Visibility.choices,
        **links,
    }
