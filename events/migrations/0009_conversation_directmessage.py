"""Add private attendee conversations and message notifications."""

import django.db.models.deletion
from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [("events", "0008_backfill_contact_notifications")]

    operations = [
        migrations.AlterField(
            model_name="notification",
            name="event",
            field=models.ForeignKey(
                blank=True,
                null=True,
                on_delete=django.db.models.deletion.CASCADE,
                related_name="notifications",
                to="events.event",
            ),
        ),
        migrations.CreateModel(
            name="Conversation",
            fields=[
                (
                    "id",
                    models.BigAutoField(
                        auto_created=True,
                        primary_key=True,
                        serialize=False,
                        verbose_name="ID",
                    ),
                ),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("updated_at", models.DateTimeField(auto_now=True)),
                (
                    "user_one",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="conversations_as_one",
                        to="accounts.user",
                    ),
                ),
                (
                    "user_two",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="conversations_as_two",
                        to="accounts.user",
                    ),
                ),
            ],
            options={
                "ordering": ["-updated_at", "-pk"],
                "constraints": [
                    models.UniqueConstraint(
                        fields=("user_one", "user_two"),
                        name="unique_user_pair_conversation",
                    ),
                    models.CheckConstraint(
                        condition=models.Q(
                            user_one__lt=models.F("user_two"),
                        ),
                        name="conversation_users_in_order",
                    ),
                ],
            },
        ),
        migrations.CreateModel(
            name="DirectMessage",
            fields=[
                (
                    "id",
                    models.BigAutoField(
                        auto_created=True,
                        primary_key=True,
                        serialize=False,
                        verbose_name="ID",
                    ),
                ),
                ("body", models.TextField(max_length=5000)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                (
                    "conversation",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="messages",
                        to="events.conversation",
                    ),
                ),
                (
                    "event",
                    models.ForeignKey(
                        blank=True,
                        null=True,
                        on_delete=django.db.models.deletion.SET_NULL,
                        related_name="direct_messages",
                        to="events.event",
                    ),
                ),
                (
                    "sender",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="direct_messages",
                        to="accounts.user",
                    ),
                ),
            ],
            options={"ordering": ["created_at", "pk"]},
        ),
        migrations.AddField(
            model_name="notification",
            name="message",
            field=models.OneToOneField(
                blank=True,
                null=True,
                on_delete=django.db.models.deletion.CASCADE,
                related_name="notification",
                to="events.directmessage",
            ),
        ),
    ]
