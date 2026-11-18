"""Upload pictures, serve authorized images, and manage the owner's pictures."""

import logging

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.exceptions import PermissionDenied, ValidationError
from django.http import FileResponse, Http404
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.views.decorators.cache import never_cache
from django.views.decorators.http import (
    require_http_methods,
    require_POST,
    require_safe,
)
from django.views.decorators.vary import vary_on_cookie

from .models import EventPhoto
from .photo_forms import PhotoUploadForm, PhotoVisibilityForm
from .photos import photo_storage, save_photo_batch

logger = logging.getLogger(__name__)


def photo_event_url(photo):
    return reverse("event_detail", args=[photo.event_id]) + "#photos"


@login_required
@never_cache
@require_http_methods(["GET", "POST"])
def upload_photos(request):
    form = PhotoUploadForm(
        request.POST if request.method == "POST" else None,
        request.FILES if request.method == "POST" else None,
        user=request.user,
        initial={"event": request.GET.get("event")},
    )
    status = 200
    if request.method == "POST" and form.is_valid():
        try:
            pictures = save_photo_batch(
                user=request.user,
                event=form.cleaned_data["event"],
                photos=form.cleaned_data["photos"],
                visibility=form.cleaned_data["visibility"],
            )
        except ValidationError as error:
            form.add_error(None, error)
        except PermissionDenied:
            raise
        except Exception:
            logger.exception("Could not save event photo batch")
            form.add_error(
                None,
                "Your pictures could not be saved. Please select them again and retry.",
            )
            status = 503
        else:
            count = len(pictures)
            messages.success(
                request, f"{count} picture{'s' if count != 1 else ''} uploaded."
            )
            return redirect(photo_event_url(pictures[0]))
    return render(
        request,
        "events/photo_upload.html",
        {"form": form, "has_photo_events": form.fields["event"].queryset.exists()},
        status=status,
    )


@never_cache
@vary_on_cookie
@require_safe
def photo_image(request, photo_id):
    photo = get_object_or_404(EventPhoto.objects.visible_to(request.user), pk=photo_id)
    try:
        image = photo_storage(photo.storage_backend).open(photo.image_key, "rb")
    except FileNotFoundError as error:
        raise Http404("Picture not found.") from error
    except Exception:
        logger.exception("Could not open event photo %s", photo.pk)
        return render(request, "events/photo_unavailable.html", status=503)
    response = FileResponse(
        image, content_type="image/jpeg", filename="event-photo.jpg"
    )
    response["X-Content-Type-Options"] = "nosniff"
    return response


@login_required
@require_POST
def photo_visibility(request, photo_id):
    photo = get_object_or_404(EventPhoto, pk=photo_id, uploader=request.user)
    form = PhotoVisibilityForm(request.POST)
    if form.is_valid():
        photo.visibility = form.cleaned_data["visibility"]
        photo.save(update_fields=["visibility"])
        messages.success(request, "Picture visibility updated.")
    else:
        messages.error(request, "Choose Public or Connections only.")
    return redirect(photo_event_url(photo))


@login_required
@never_cache
@require_http_methods(["GET", "POST"])
def delete_photo(request, photo_id):
    photo = get_object_or_404(EventPhoto, pk=photo_id, uploader=request.user)
    if request.method == "POST":
        destination = photo_event_url(photo)
        photo.delete()
        messages.success(request, "Picture deleted.")
        return redirect(destination)
    return render(request, "events/photo_delete.html", {"photo": photo})
