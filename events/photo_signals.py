"""Clean up private photo files after explicit and cascading deletions commit."""

from functools import partial

from django.db import transaction
from django.db.models.signals import post_delete
from django.dispatch import receiver

from .models import EventPhoto
from .photos import delete_stored_photo


@receiver(post_delete, sender=EventPhoto)
def cleanup_photo_file(sender, instance, using, **kwargs):
    transaction.on_commit(
        partial(delete_stored_photo, instance.storage_backend, instance.image_key),
        using=using,
    )
