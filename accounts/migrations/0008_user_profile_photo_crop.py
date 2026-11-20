from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [("accounts", "0007_user_discoverable_for_similar_events")]

    operations = [
        migrations.AddField(
            model_name="user",
            name="profile_photo_crop",
            field=models.ImageField(
                blank=True,
                upload_to="profile_photos/crops/",
                verbose_name="profile photo crop",
            ),
        ),
        migrations.AddField(
            model_name="user",
            name="profile_photo_crop_x",
            field=models.FloatField(default=0.5, editable=False),
        ),
        migrations.AddField(
            model_name="user",
            name="profile_photo_crop_y",
            field=models.FloatField(default=0.5, editable=False),
        ),
        migrations.AddField(
            model_name="user",
            name="profile_photo_crop_zoom",
            field=models.FloatField(default=1.0, editable=False),
        ),
    ]
