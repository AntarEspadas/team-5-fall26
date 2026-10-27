"""Bring pre-existing pending requests into the notification list."""

from django.db import migrations


def backfill_contact_notifications(apps, schema_editor):
    request_model = apps.get_model("events", "ContactRequest")
    notification_model = apps.get_model("events", "Notification")
    alias = schema_editor.connection.alias
    requests = (
        request_model.objects.using(alias)
        .filter(status="pending")
        .select_related("sender")
    )
    for contact_request in requests.iterator():
        sender = contact_request.sender
        name = sender.display_name or sender.username
        notification, created = notification_model.objects.using(alias).get_or_create(
            contact_request_id=contact_request.pk,
            defaults={
                "recipient_id": contact_request.recipient_id,
                "event_id": contact_request.event_id,
                "subject": f"Contact request from {name}",
                "body": contact_request.message,
            },
        )
        if created:
            # auto_now_add overrides values supplied during creation.
            notification_model.objects.using(alias).filter(pk=notification.pk).update(
                created_at=contact_request.created_at
            )


def remove_contact_notifications(apps, schema_editor):
    notification_model = apps.get_model("events", "Notification")
    notification_model.objects.using(schema_editor.connection.alias).filter(
        contact_request__isnull=False
    ).delete()


class Migration(migrations.Migration):
    dependencies = [("events", "0007_notification_contact_request")]

    operations = [
        migrations.RunPython(
            backfill_contact_notifications, remove_contact_notifications
        ),
    ]
