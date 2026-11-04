"""Link incoming request notifications to their source request."""

import django.db.models.deletion
from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [("events", "0006_eventinterest_status")]

    operations = [
        migrations.AddField(
            model_name="notification",
            name="contact_request",
            field=models.OneToOneField(
                blank=True,
                null=True,
                on_delete=django.db.models.deletion.CASCADE,
                related_name="notification",
                to="events.contactrequest",
            ),
        ),
    ]
