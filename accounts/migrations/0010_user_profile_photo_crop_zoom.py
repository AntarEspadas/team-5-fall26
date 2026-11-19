from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [("accounts", "0009_repair_missing_profile_photo_crop")]

    operations = [
        migrations.AddField(
            model_name="user",
            name="profile_photo_crop_zoom",
            field=models.FloatField(default=1.0, editable=False),
        ),
    ]
