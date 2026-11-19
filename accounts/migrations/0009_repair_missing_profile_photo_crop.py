"""Repair databases that applied the crop migrations before the column was added."""

from django.db import migrations


def add_missing_crop_column(apps, schema_editor):
    """Bring the database in line with the existing migration state."""
    user = apps.get_model("accounts", "User")
    field = user._meta.get_field("profile_photo_crop")
    connection = schema_editor.connection

    with connection.cursor() as cursor:
        columns = connection.introspection.get_table_description(
            cursor, user._meta.db_table
        )

    if field.column not in {column.name for column in columns}:
        schema_editor.add_field(user, field)


class Migration(migrations.Migration):
    dependencies = [("accounts", "0008_alter_user_profile_photo_crop")]

    operations = [
        migrations.RunPython(add_missing_crop_column, migrations.RunPython.noop),
    ]
