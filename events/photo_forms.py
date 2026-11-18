"""Validate every image in a batch before writing any photo files."""

from io import BytesIO

from PIL import Image, ImageOps
from django import forms
from django.core.files.base import ContentFile
from django.utils import timezone

from .filters import NYC_TIMEZONE
from .models import Event, EventPhoto
from .photos import MAX_PHOTOS, MAX_PHOTO_BYTES, MAX_PHOTO_SIDE, uploadable_events


class MultipleImageInput(forms.ClearableFileInput):
    allow_multiple_selected = True


class MultipleImageField(forms.ImageField):
    """ImageField's validation must run for every selected file."""

    def clean(self, data, initial=None):
        uploads = data if isinstance(data, (list, tuple)) else [data]
        if not uploads:
            uploads = [None]
        if len(uploads) > MAX_PHOTOS:
            raise forms.ValidationError(
                f"Choose at most {MAX_PHOTOS} pictures at a time."
            )
        return [self.clean_image(upload) for upload in uploads]

    def clean_image(self, upload):
        if upload is not None and upload.size > MAX_PHOTO_BYTES:
            raise forms.ValidationError("Each picture must be no larger than 5 MiB.")
        photo = super().clean(upload)
        if photo.image.format not in {"JPEG", "PNG", "WEBP"}:
            raise forms.ValidationError("Choose JPEG, PNG, or WebP pictures.")
        if max(photo.image.size) > MAX_PHOTO_SIDE:
            raise forms.ValidationError(
                "Pictures must be at most 4096 pixels per side."
            )
        try:
            photo.seek(0)
            with Image.open(photo) as source:
                normalized = ImageOps.exif_transpose(source).convert("RGB")
                normalized.thumbnail((2048, 2048), Image.Resampling.LANCZOS)
                normalized.info.clear()
                output = BytesIO()
                normalized.save(output, format="JPEG", quality=90)
        except (OSError, ValueError) as error:
            raise forms.ValidationError("Choose valid, undamaged pictures.") from error
        return ContentFile(output.getvalue(), name="photo.jpg")


class PhotoEventChoiceField(forms.ModelChoiceField):
    def label_from_instance(self, event):
        day = timezone.localtime(event.start_time, NYC_TIMEZONE).strftime("%b %d, %Y")
        return f"{event.title} — {day}"


class PhotoUploadForm(forms.Form):
    event = PhotoEventChoiceField(queryset=Event.objects.none())
    photos = MultipleImageField(
        max_length=512,
        label="Pictures",
        widget=MultipleImageInput(attrs={"accept": "image/jpeg,image/png,image/webp"}),
        help_text=(
            "Choose 1–10 JPEG, PNG, or WebP pictures. "
            "Up to 5 MiB each and 4096 pixels per side."
        ),
    )
    visibility = forms.ChoiceField(
        choices=EventPhoto.Visibility.choices,
        initial=EventPhoto.Visibility.CONNECTIONS,
        help_text=(
            "Public pictures can be viewed by anyone, including signed-out visitors. "
            "Connections only shares with you and your accepted connections."
        ),
    )

    def __init__(self, *args, user, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["event"].queryset = uploadable_events(user)


class PhotoVisibilityForm(forms.Form):
    visibility = forms.ChoiceField(choices=EventPhoto.Visibility.choices)
