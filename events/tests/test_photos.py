"""Upload, audience, storage, lifecycle, and gallery integration tests."""

from datetime import timedelta
from io import BytesIO
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from PIL import Image
from django.conf import settings
from django.core.exceptions import (
    ImproperlyConfigured,
    PermissionDenied,
    ValidationError,
)
from django.core.files.base import ContentFile
from django.core.files.storage import storages
from django.core.files.uploadedfile import SimpleUploadedFile
from django.db import IntegrityError, transaction
from django.test import Client, SimpleTestCase, TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from accounts.models import User, UserBan
from events.models import ContactRequest, Event, EventInterest, EventPhoto, Registration
from events.photo_forms import PhotoUploadForm
from events.photos import (
    delete_stored_photo,
    photo_storage,
    save_photo_batch,
    uploadable_events,
)


def picture(*, image_format="PNG", size=(32, 24), exif=None, suffix=b""):
    output = BytesIO()
    options = {"exif": exif} if exif is not None else {}
    Image.new("RGB", size, "green").save(output, format=image_format, **options)
    return SimpleUploadedFile(
        f"holiday.{image_format.lower()}",
        output.getvalue() + suffix,
        content_type=f"image/{image_format.lower()}",
    )


class PhotoTestCase(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.host = User.objects.create_user("photo-host", "host@example.com")
        cls.member = User.objects.create_user(
            "photographer", "member@example.com", share_profile_with_attendees=True
        )
        cls.friend = User.objects.create_user("friend", "friend@example.com")
        cls.stranger = User.objects.create_user("stranger", "stranger@example.com")
        cls.event = cls.make_event("park-walk", host=cls.host)
        Registration.objects.create(event=cls.event, user=cls.member)

    @classmethod
    def make_event(cls, guid, **fields):
        defaults = {
            "title": guid.replace("-", " ").title(),
            "content_hash": "p" * 64,
            "start_time": timezone.now() - timedelta(hours=2),
            "end_time": timezone.now() - timedelta(hours=1),
        }
        defaults.update(fields)
        return Event.objects.create(guid=guid, **defaults)

    def setUp(self):
        self.directory = self.enterContext(TemporaryDirectory())
        self.enterContext(
            override_settings(
                EVENT_PHOTO_STORAGE="local",
                EVENT_PHOTO_S3_BUCKET="test-photos",
                STORAGES={
                    **settings.STORAGES,
                    "event_photos_local": {
                        "BACKEND": "django.core.files.storage.FileSystemStorage",
                        "OPTIONS": {"location": self.directory},
                    },
                    "event_photos_s3": {
                        "BACKEND": "django.core.files.storage.InMemoryStorage",
                    },
                },
            )
        )
        self.client.force_login(self.member)
        self.upload_url = reverse("event_photo_upload")
        self.event_url = reverse("event_detail", args=[self.event.pk])
        self.profile_url = reverse("public_profile", args=[self.member.username])

    def upload(self, **overrides):
        data = {
            "event": self.event.pk,
            "photos": [picture()],
            "visibility": EventPhoto.Visibility.CONNECTIONS,
            **overrides,
        }
        return self.client.post(self.upload_url, data)

    def saved_photo(self, **fields):
        defaults = {
            "event": self.event,
            "uploader": self.member,
            "visibility": EventPhoto.Visibility.CONNECTIONS,
            "storage_backend": "local",
        }
        defaults.update(fields)
        key = photo_storage(defaults["storage_backend"]).save(
            f"fixture-{EventPhoto.objects.count()}.jpg", picture(image_format="JPEG")
        )
        return EventPhoto.objects.create(image_key=key, **defaults)

    def connect(self, *, sender=None, recipient=None, status="accepted", event=None):
        return ContactRequest.objects.create(
            event=event or self.event,
            sender=sender or self.member,
            recipient=recipient or self.friend,
            status=status,
        )

    def image(self, photo, client=None):
        response = (client or self.client).get(
            reverse("event_photo_image", args=[photo.pk])
        )
        if response.streaming:
            response.image_bytes = b"".join(response.streaming_content)
        return response

    def disk_files(self):
        return [path for path in Path(self.directory).rglob("*") if path.is_file()]


class UploadTests(PhotoTestCase):
    def test_form_preselection_and_private_default(self):
        response = self.client.get(self.upload_url, {"event": self.event.pk})
        self.assertContains(response, 'enctype="multipart/form-data"')
        self.assertContains(response, "multiple required")
        self.assertContains(response, 'name="csrfmiddlewaretoken"')
        self.assertEqual(response.context["form"].initial["event"], str(self.event.pk))
        self.assertEqual(
            response.context["form"].fields["visibility"].initial, "connections"
        )
        self.assertContains(response, self.event.title)
        self.assertContains(response, "signed-out visitors")

    def test_batch_tags_one_event_and_ignores_forged_owner_or_storage(self):
        response = self.upload(
            photos=[picture(), picture(image_format="WEBP")],
            visibility="public",
            uploader=self.stranger.pk,
            storage_backend="s3",
        )
        self.assertRedirects(
            response, self.event_url + "#photos", fetch_redirect_response=False
        )
        photos = list(EventPhoto.objects.all())
        self.assertEqual(len(photos), 2)
        self.assertEqual(len(self.disk_files()), 2)
        for photo in photos:
            self.assertEqual(photo.event_id, self.event.pk)
            self.assertEqual(photo.uploader_id, self.member.pk)
            self.assertEqual(photo.storage_backend, "local")
            self.assertEqual(photo.visibility, "public")
            self.assertNotIn("holiday", photo.image_key)
        self.assertContains(self.client.get(self.event_url), "2 pictures uploaded.")

    def test_host_can_upload_and_finished_events_have_no_deadline(self):
        self.client.force_login(self.host)
        self.event.start_time = timezone.now() - timedelta(days=400)
        self.event.end_time = self.event.start_time + timedelta(hours=1)
        self.event.status = Event.Status.DONE
        self.event.save()
        self.assertEqual(self.upload().status_code, 302)
        self.assertEqual(EventPhoto.objects.get().uploader_id, self.host.pk)

    def test_upload_during_event_and_at_start_boundary(self):
        now = timezone.now()
        Event.objects.filter(pk=self.event.pk).update(
            start_time=now, end_time=now + timedelta(hours=1)
        )
        with patch("events.photos.timezone.now", return_value=now):
            self.assertEqual(self.upload().status_code, 302)

    def test_anonymous_upload_requires_login(self):
        self.client.logout()
        for method in (self.client.get, self.client.post):
            response = method(self.upload_url)
            self.assertEqual(response.status_code, 302)
            self.assertIn(reverse("login"), response.url)
        self.assertFalse(EventPhoto.objects.exists())

    def test_interested_and_unregistered_members_cannot_upload(self):
        self.client.force_login(self.stranger)
        EventInterest.objects.create(event=self.event, user=self.stranger)
        response = self.upload()
        self.assertEqual(response.status_code, 200)
        self.assertFalse(response.context["has_photo_events"])
        self.assertFalse(EventPhoto.objects.exists())
        self.assertEqual(self.disk_files(), [])

    def test_rejects_future_undated_and_both_kinds_of_cancellation(self):
        for changes in (
            {"start_time": timezone.now() + timedelta(hours=1)},
            {"start_time": None},
            {"status": Event.Status.CANCELLED},
            {"title": "  CANCELLED: Park Walk"},
            {"title": "  Canceled: Park Walk"},
        ):
            with self.subTest(changes=changes):
                event = self.make_event(
                    f"invalid-{len(Event.objects.all())}", **changes
                )
                Registration.objects.create(event=event, user=self.member)
                response = self.upload(event=event.pk)
                self.assertIn("event", response.context["form"].errors)
        self.assertFalse(EventPhoto.objects.exists())
        self.assertEqual(self.disk_files(), [])

    def test_rejects_missing_invalid_and_late_invalid_images_before_writing(self):
        cases = [
            [],
            [SimpleUploadedFile("bad.jpg", b"not an image")],
            [picture(), SimpleUploadedFile("bad.jpg", b"not an image")],
            [picture(image_format="GIF")],
            [picture(size=(4097, 1))],
            [picture(suffix=b"x" * (5 * 1024 * 1024))],
            [picture() for _ in range(11)],
        ]
        for uploads in cases:
            with self.subTest(count=len(uploads)):
                response = self.upload(photos=uploads)
                self.assertIn("photos", response.context["form"].errors)
                self.assertFalse(EventPhoto.objects.exists())
                self.assertEqual(self.disk_files(), [])

    def test_accepts_ten_images_and_exact_byte_and_dimension_limits(self):
        upload = picture(size=(4096, 1))
        content = upload.read()
        upload = SimpleUploadedFile(
            "boundary.png", content + b"x" * (5 * 1024 * 1024 - len(content))
        )
        response = self.upload(photos=[upload] + [picture() for _ in range(9)])
        self.assertEqual(response.status_code, 302)
        self.assertEqual(EventPhoto.objects.count(), 10)

    def test_requires_valid_event_and_visibility(self):
        for field, value in (("event", ""), ("event", 999999), ("visibility", "")):
            with self.subTest(field=field, value=value):
                response = self.upload(**{field: value})
                self.assertIn(field, response.context["form"].errors)
        self.assertIn(
            "visibility", self.upload(visibility="everyone").context["form"].errors
        )
        self.assertFalse(EventPhoto.objects.exists())

    def test_reencodes_orientation_resizes_and_removes_metadata_and_trailing_bytes(
        self,
    ):
        exif = Image.Exif()
        exif[274] = 6
        exif[315] = "Private camera owner"
        response = self.upload(
            photos=[
                picture(
                    image_format="JPEG",
                    size=(3000, 1500),
                    exif=exif,
                    suffix=b"untrusted trailing bytes",
                )
            ]
        )
        self.assertEqual(response.status_code, 302)
        stored = self.image(EventPhoto.objects.get()).image_bytes
        self.assertNotIn(b"untrusted trailing bytes", stored)
        self.assertNotIn(b"Private camera owner", stored)
        with Image.open(BytesIO(stored)) as decoded:
            self.assertEqual(decoded.format, "JPEG")
            self.assertEqual(decoded.size, (1024, 2048))
            self.assertEqual(dict(decoded.getexif()), {})

    def test_banned_members_cannot_upload_or_see_upload_controls(self):
        UserBan.objects.create(user=self.member, offense_number=1)
        self.assertEqual(self.upload().status_code, 403)
        response = self.client.get(self.upload_url)
        self.assertNotContains(response, 'enctype="multipart/form-data"')
        for url in (self.event_url, self.profile_url):
            self.assertNotContains(self.client.get(url), "Upload pictures")
        self.assertFalse(EventPhoto.objects.exists())
        self.assertEqual(self.disk_files(), [])

    def test_csrf_is_required(self):
        client = Client(enforce_csrf_checks=True)
        client.force_login(self.member)
        response = client.post(
            self.upload_url,
            {"event": self.event.pk, "photos": [picture()], "visibility": "public"},
        )
        self.assertEqual(response.status_code, 403)
        self.assertFalse(EventPhoto.objects.exists())


class AudienceTests(PhotoTestCase):
    def test_public_picture_and_event_gallery_are_available_while_signed_out(self):
        photo = self.saved_photo(visibility="public")
        self.client.logout()
        response = self.image(photo)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response["Content-Type"], "image/jpeg")
        self.assertIn("private", response["Cache-Control"])
        self.assertIn("no-store", response["Cache-Control"])
        self.assertIn("Cookie", response["Vary"])
        self.assertEqual(response["X-Content-Type-Options"], "nosniff")
        response = self.client.get(self.event_url)
        self.assertContains(response, reverse("event_photo_image", args=[photo.pk]))
        self.assertNotContains(response, photo.image_key)
        head = self.client.head(reverse("event_photo_image", args=[photo.pk]))
        self.assertEqual(head.status_code, 200)
        if head.streaming:
            b"".join(head.streaming_content)

    def test_connections_only_denies_anonymous_strangers_attendees_and_host(self):
        photo = self.saved_photo()
        Registration.objects.create(event=self.event, user=self.stranger)
        for viewer in (None, self.stranger, self.host):
            with self.subTest(viewer=viewer):
                self.client.logout()
                if viewer is not None:
                    self.client.force_login(viewer)
                self.assertEqual(self.image(photo).status_code, 404)
                page = self.client.get(self.event_url)
                self.assertContains(page, "0 pictures")
                self.assertNotContains(
                    page, reverse("event_photo_image", args=[photo.pk])
                )

    def test_accepted_contact_in_either_direction_and_another_event_grants_access(self):
        photo = self.saved_photo()
        other = self.make_event("other-contact-event")
        for reverse_direction in (False, True):
            ContactRequest.objects.all().delete()
            self.connect(
                sender=self.friend if reverse_direction else self.member,
                recipient=self.member if reverse_direction else self.friend,
                event=other,
            )
            self.client.force_login(self.friend)
            self.assertEqual(self.image(photo).status_code, 200)
            for url in (self.event_url, self.profile_url):
                self.assertContains(
                    self.client.get(url), reverse("event_photo_image", args=[photo.pk])
                )

    def test_pending_and_declined_contacts_do_not_grant_access(self):
        photo = self.saved_photo()
        contact = self.connect(status="pending")
        self.client.force_login(self.friend)
        for status in ("pending", "declined"):
            contact.status = status
            contact.save()
            self.assertEqual(self.image(photo).status_code, 404)

    def test_owner_can_view_own_photos_and_revoked_connection_loses_access(self):
        photo = self.saved_photo()
        self.assertEqual(self.image(photo).status_code, 200)
        contact = self.connect()
        self.client.force_login(self.friend)
        self.assertEqual(self.image(photo).status_code, 200)
        contact.delete()
        self.assertEqual(self.image(photo).status_code, 404)
        self.assertNotContains(
            self.client.get(self.profile_url),
            reverse("event_photo_image", args=[photo.pk]),
        )

    def test_profile_access_rules_do_not_change_public_event_photo_access(self):
        photo = self.saved_photo(visibility="public")
        self.member.share_profile_with_attendees = False
        self.member.save()
        self.client.force_login(self.friend)
        self.assertTemplateUsed(
            self.client.get(self.profile_url), "accounts/profile_unavailable.html"
        )
        self.assertEqual(self.image(photo).status_code, 200)
        self.client.logout()
        self.assertEqual(self.client.get(self.profile_url).status_code, 302)
        self.assertEqual(self.image(photo).status_code, 200)

    def test_upload_eligibility_changes_do_not_remove_existing_pictures_or_controls(
        self,
    ):
        photo = self.saved_photo()
        Registration.objects.filter(event=self.event, user=self.member).delete()
        self.event.status = Event.Status.CANCELLED
        self.event.save()
        self.assertEqual(self.image(photo).status_code, 200)
        response = self.client.get(self.event_url)
        self.assertContains(response, reverse("event_photo_delete", args=[photo.pk]))
        self.assertNotContains(response, "Upload pictures")

    def test_missing_and_unavailable_files_have_safe_responses(self):
        photo = self.saved_photo()
        storage = photo_storage("local")
        storage.delete(photo.image_key)
        self.assertEqual(self.image(photo).status_code, 404)
        with patch.object(storage, "open", side_effect=OSError("unavailable")):
            with self.assertLogs("events.photo_views", level="ERROR"):
                response = self.image(photo)
        self.assertEqual(response.status_code, 503)
        self.assertContains(response, "temporarily unavailable", status_code=503)
        self.assertIn("no-store", response["Cache-Control"])


class ManagementAndGalleryTests(PhotoTestCase):
    def test_owner_can_change_visibility_without_moving_the_file(self):
        photo = self.saved_photo()
        endpoint = reverse("event_photo_visibility", args=[photo.pk])
        original_key = photo.image_key
        response = self.client.post(endpoint, {"visibility": "public"})
        self.assertRedirects(
            response, self.event_url + "#photos", fetch_redirect_response=False
        )
        self.assertEqual(self.image(photo, Client()).status_code, 200)
        self.client.post(endpoint, {"visibility": "connections"})
        photo.refresh_from_db()
        self.assertEqual(photo.visibility, "connections")
        self.assertEqual(photo.image_key, original_key)
        self.assertEqual(self.image(photo, Client()).status_code, 404)
        self.assertEqual(self.client.get(endpoint).status_code, 405)
        self.client.post(endpoint, {"visibility": "invalid"})
        photo.refresh_from_db()
        self.assertEqual(photo.visibility, "connections")

    def test_other_users_and_host_cannot_manage_even_public_photos(self):
        photo = self.saved_photo(visibility="public")
        for viewer in (self.stranger, self.host):
            self.client.force_login(viewer)
            self.assertEqual(
                self.client.post(
                    reverse("event_photo_visibility", args=[photo.pk]),
                    {"visibility": "connections"},
                ).status_code,
                404,
            )
            endpoint = reverse("event_photo_delete", args=[photo.pk])
            self.assertEqual(self.client.get(endpoint).status_code, 404)
            self.assertEqual(self.client.post(endpoint).status_code, 404)
        photo.refresh_from_db()
        self.assertEqual(photo.visibility, "public")

    def test_deletion_requires_confirmation_and_removes_file_after_commit(self):
        photo = self.saved_photo()
        endpoint = reverse("event_photo_delete", args=[photo.pk])
        self.assertContains(self.client.get(endpoint), "Delete this picture?")
        self.assertTrue(EventPhoto.objects.filter(pk=photo.pk).exists())
        self.assertEqual(len(self.disk_files()), 1)
        with self.captureOnCommitCallbacks(execute=True):
            response = self.client.post(endpoint)
        self.assertRedirects(
            response, self.event_url + "#photos", fetch_redirect_response=False
        )
        self.assertFalse(EventPhoto.objects.exists())
        self.assertEqual(self.disk_files(), [])
        self.assertEqual(self.image(photo).status_code, 404)

    def test_cascading_deletions_clean_files_and_rollbacks_keep_them(self):
        for target in ("event", "uploader"):
            with self.subTest(target=target):
                photo = self.saved_photo()
                identifier = photo.pk
                with self.assertRaises(RuntimeError):
                    with transaction.atomic():
                        photo.delete()
                        raise RuntimeError("rollback")
                self.assertTrue(EventPhoto.objects.filter(pk=identifier).exists())
                self.assertEqual(len(self.disk_files()), 1)
                with self.captureOnCommitCallbacks(execute=True):
                    getattr(EventPhoto.objects.get(pk=identifier), target).delete()
                self.assertEqual(self.disk_files(), [])
                if target == "event":
                    self.event = self.make_event("replacement-event")

    def test_bans_block_management_but_allow_reading_own_photos(self):
        photo = self.saved_photo()
        UserBan.objects.create(user=self.member, offense_number=1)
        for endpoint, data in (
            ("event_photo_visibility", {"visibility": "public"}),
            ("event_photo_delete", {}),
        ):
            self.assertEqual(
                self.client.post(reverse(endpoint, args=[photo.pk]), data).status_code,
                403,
            )
        self.assertEqual(self.image(photo).status_code, 200)
        for url in (self.event_url, self.profile_url):
            self.assertNotContains(
                self.client.get(url), reverse("event_photo_delete", args=[photo.pk])
            )
        self.assertNotContains(
            self.client.get(reverse("event_photo_delete", args=[photo.pk])),
            "Keep picture",
        )

    def test_profile_groups_newest_events_and_then_newest_photos(self):
        older = self.make_event(
            "older-event", start_time=timezone.now() - timedelta(days=3)
        )
        old_photo = self.saved_photo(event=older)
        first = self.saved_photo()
        latest = self.saved_photo()
        hidden = self.make_event("hidden-event", title="Secret event attendance")
        self.saved_photo(event=hidden)
        self.client.force_login(self.stranger)
        EventPhoto.objects.filter(pk__in=[old_photo.pk, first.pk, latest.pk]).update(
            visibility="public"
        )
        response = self.client.get(self.profile_url)
        self.assertEqual(
            [photo.pk for photo in response.context["photo_page"]],
            [latest.pk, first.pk, old_photo.pk],
        )
        self.assertNotContains(response, "Secret event attendance")
        self.assertContains(response, "grouped by event")
        self.assertContains(response, "3 pictures")
        self.assertIn("private", response["Cache-Control"])

    def test_pagination_is_stable_preserves_parameters_and_filters_counts(self):
        for index in range(26):
            EventPhoto.objects.create(
                event=self.event,
                uploader=self.member,
                visibility="public" if index < 25 else "connections",
                storage_backend="local",
                image_key=f"not-loaded-{index}.jpg",
            )
        self.client.logout()
        response = self.client.get(self.event_url, {"from_event": "123"})
        self.assertEqual(len(response.context["photo_page"]), 24)
        self.assertContains(response, "25 pictures")
        self.assertContains(response, "?from_event=123&amp;photo_page=2#photos")
        second = self.client.get(self.event_url, {"photo_page": 2})
        self.assertEqual(len(second.context["photo_page"]), 1)
        self.assertTrue(second.context["photo_previous_url"])
        first_ids = {photo.pk for photo in response.context["photo_page"]}
        self.assertNotIn(second.context["photo_page"][0].pk, first_ids)


class StorageTests(PhotoTestCase):
    def test_setting_switches_new_uploads_and_preserves_each_existing_backend(self):
        self.upload()
        local = EventPhoto.objects.get()
        with override_settings(EVENT_PHOTO_STORAGE="s3"):
            self.upload()
            remote = EventPhoto.objects.exclude(pk=local.pk).get()
            self.assertEqual(remote.storage_backend, "s3")
            self.assertTrue(photo_storage("s3").exists(remote.image_key))
            self.assertEqual(self.image(local).status_code, 200)
        self.assertEqual(self.image(remote).status_code, 200)
        self.assertEqual(self.image(local).status_code, 200)
        with self.captureOnCommitCallbacks(execute=True):
            remote.delete()
        self.assertFalse(photo_storage("s3").exists(remote.image_key))
        self.assertEqual(len(self.disk_files()), 1)

    def test_storage_failure_rolls_back_entire_batch_and_cleans_written_files(self):
        storage = photo_storage("local")
        original_save = storage.save
        calls = []

        def fail_second(*args, **kwargs):
            calls.append(args[0])
            if len(calls) == 2:
                raise OSError("disk unavailable")
            return original_save(*args, **kwargs)

        with patch.object(storage, "save", side_effect=fail_second):
            with self.assertLogs("events.photo_views", level="ERROR"):
                response = self.upload(photos=[picture(), picture()])
        self.assertEqual(response.status_code, 503)
        self.assertContains(response, "could not be saved", status_code=503)
        self.assertFalse(EventPhoto.objects.exists())
        self.assertEqual(self.disk_files(), [])

    def test_database_failure_also_cleans_stored_objects(self):
        with patch.object(
            EventPhoto.objects, "create", side_effect=IntegrityError("DB")
        ):
            with self.assertLogs("events.photo_views", level="ERROR"):
                response = self.upload()
        self.assertEqual(response.status_code, 503)
        self.assertFalse(EventPhoto.objects.exists())
        self.assertEqual(self.disk_files(), [])

    def test_cleanup_failures_are_logged_without_breaking_committed_deletion(self):
        photo = self.saved_photo()
        with patch.object(
            photo_storage("local"), "delete", side_effect=OSError("disk")
        ):
            with self.assertLogs("events.photos", level="ERROR"):
                with self.captureOnCommitCallbacks(execute=True):
                    photo.delete()
        self.assertFalse(EventPhoto.objects.exists())

    def test_missing_s3_configuration_and_unknown_backends_fail_without_fallback(self):
        with override_settings(EVENT_PHOTO_S3_BUCKET=""):
            with self.assertRaises(ImproperlyConfigured):
                photo_storage("s3")
        with self.assertRaises(ImproperlyConfigured):
            photo_storage("unknown")
        self.assertEqual(self.disk_files(), [])

    def test_service_rechecks_eligibility_and_bans_before_writing(self):
        Registration.objects.filter(event=self.event, user=self.member).delete()
        with self.assertRaises(ValidationError):
            save_photo_batch(
                user=self.member,
                event=self.event,
                photos=[ContentFile(b"validated image", name="photo.jpg")],
                visibility="public",
            )
        UserBan.objects.create(user=self.member, offense_number=1)
        self.assertFalse(uploadable_events(self.member).exists())
        with self.assertRaises(PermissionDenied):
            save_photo_batch(
                user=self.member, event=self.event, photos=[], visibility="public"
            )
        self.assertEqual(self.disk_files(), [])

    def test_event_becoming_ineligible_after_form_validation_shows_error(self):
        with patch(
            "events.photo_views.save_photo_batch",
            side_effect=ValidationError(
                "You can no longer upload photos for this event."
            ),
        ):
            response = self.upload()
        self.assertContains(response, "You can no longer upload photos")
        self.assertEqual(self.disk_files(), [])

    def test_damaged_image_decode_is_a_form_error(self):
        with patch(
            "events.photo_forms.ImageOps.exif_transpose", side_effect=OSError("decode")
        ):
            response = self.upload()
        self.assertIn("photos", response.context["form"].errors)
        self.assertEqual(self.disk_files(), [])

    def test_single_file_data_is_validated_as_a_batch(self):
        form = PhotoUploadForm(
            data={"event": self.event.pk, "visibility": "public"},
            files={"photos": picture()},
            user=self.member,
        )
        self.assertTrue(form.is_valid(), form.errors)
        self.assertEqual(len(form.cleaned_data["photos"]), 1)

    def test_unknown_cleanup_backend_is_logged(self):
        with self.assertLogs("events.photos", level="ERROR"):
            delete_stored_photo("missing", "photo.jpg")


class StorageConfigurationTests(SimpleTestCase):
    def test_default_media_backends_stay_separate_from_private_photo_stores(self):
        self.assertEqual(
            settings.STORAGES["default"]["BACKEND"],
            "django.core.files.storage.FileSystemStorage",
        )
        self.assertEqual(
            settings.STORAGES["staticfiles"]["BACKEND"],
            "django.contrib.staticfiles.storage.StaticFilesStorage",
        )
        local_root = Path(
            settings.STORAGES["event_photos_local"]["OPTIONS"]["location"]
        )
        self.assertFalse(local_root.is_relative_to(Path(settings.MEDIA_ROOT)))
        options = settings.STORAGES["event_photos_s3"]["OPTIONS"]
        self.assertIsNone(options["default_acl"])
        self.assertTrue(options["querystring_auth"])
        self.assertIsNone(options["custom_domain"])

    def test_storage_api_can_use_s3_without_network_during_configuration(self):
        from storages.backends.s3 import S3Storage

        config = settings.STORAGES["event_photos_s3"]
        storage = storages.create_storage(config)
        self.assertIsInstance(storage, S3Storage)
        self.assertFalse(storage.file_overwrite)
        self.assertIsNone(storage.default_acl)
        self.assertTrue(storage.querystring_auth)

    def test_s3_upload_delete_and_missing_file_with_stubbed_aws(self):
        from boto3.s3.transfer import TransferConfig
        from botocore.stub import Stubber

        storage = storages.create_storage(
            {
                "BACKEND": "storages.backends.s3.S3Storage",
                "OPTIONS": {
                    **settings.STORAGES["event_photos_s3"]["OPTIONS"],
                    "bucket_name": "test-photos",
                    "region_name": "us-east-1",
                    "access_key": "test-access",
                    "secret_key": "test-secret",
                    "transfer_config": TransferConfig(use_threads=False),
                },
            }
        )
        client = storage.connection.meta.client
        location = {"Bucket": "test-photos", "Key": "event_photos/picture.jpg"}
        with Stubber(client) as aws:
            aws.add_client_error(
                "head_object",
                service_error_code="404",
                http_status_code=404,
                expected_params=location,
            )
            aws.add_response("put_object", {})
            aws.add_response("delete_object", {}, expected_params=location)
            aws.add_client_error(
                "head_object",
                service_error_code="404",
                http_status_code=404,
                expected_params={
                    "Bucket": "test-photos",
                    "Key": "event_photos/missing.jpg",
                },
            )
            with patch.object(client, "put_object", wraps=client.put_object) as upload:
                key = storage.save("picture.jpg", picture(image_format="JPEG"))
            self.assertEqual(key, "picture.jpg")
            parameters = upload.call_args.kwargs
            self.assertEqual(parameters["Key"], location["Key"])
            self.assertEqual(parameters["ContentType"], "image/jpeg")
            self.assertNotIn("ACL", parameters)
            storage.delete(key)
            with self.assertRaises(FileNotFoundError):
                storage.open("missing.jpg")
            aws.assert_no_pending_responses()
