"""Allow members to opt into discovery through related events."""

from django.db import migrations, models


class Migration(migrations.Migration):
    """Store a separate opt-in for being suggested on other event pages."""

    dependencies = [
        ("accounts", "0006_user_ban"),
    ]

    operations = [
        migrations.AddField(
            model_name="user",
            name="discoverable_for_similar_events",
            field=models.BooleanField(
                default=False,
                help_text=(
                    "Allow your public profile to appear to people browsing other "
                    "events with similar activities. Requires profile sharing to be on."
                ),
                verbose_name="appear in similar-event suggestions",
            ),
        ),
    ]
