"""Use message read state instead of duplicate notification entries."""

import django.db.models.deletion
from django.db import migrations, models


def remove_message_notifications(apps, schema_editor):
    Notification = apps.get_model("events", "Notification")
    alias = schema_editor.connection.alias
    Notification.objects.using(alias).filter(message__isnull=False).delete()


class Migration(migrations.Migration):
    dependencies = [("events", "0009_conversation_directmessage")]

    operations = [
        migrations.RunPython(
            remove_message_notifications,
            migrations.RunPython.noop,
        ),
        migrations.RemoveField(
            model_name="notification",
            name="message",
        ),
        migrations.AlterField(
            model_name="notification",
            name="event",
            field=models.ForeignKey(
                on_delete=django.db.models.deletion.CASCADE,
                related_name="notifications",
                to="events.event",
            ),
        ),
        migrations.AddField(
            model_name="directmessage",
            name="read_at",
            field=models.DateTimeField(blank=True, null=True, verbose_name="read at"),
        ),
    ]
