"""No blank gap between the Description heading and its text (#183)."""

from datetime import timedelta

from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from events.models import Event


class DescriptionGapTests(TestCase):
    """The description box uses pre-line, so stray template newlines show."""

    def make_event(self, description):
        return Event.objects.create(
            guid=f"gap-{len(description)}",
            title="River Walk",
            description=description,
            content_hash="",
            start_time=timezone.now() + timedelta(days=3),
            registration_url="https://example.com/register",
        )

    def page(self, event):
        response = self.client.get(reverse("event_detail", args=[event.pk]))
        self.assertEqual(response.status_code, 200)
        return response.content.decode()

    def test_text_starts_right_after_the_opening_tag(self):
        """No newline or spaces come before the description text."""
        html = self.page(self.make_event("Meet at the dock.\nBring water."))

        self.assertIn(
            '<div class="detail-description">Meet at the dock.\nBring water.</div>',
            html,
        )

    def test_missing_description_message_has_no_gap(self):
        """The fallback text is also flush with the box."""
        html = self.page(self.make_event(""))

        self.assertIn(
            '<div class="detail-description">'
            "No description is available for this event.</div>",
            html,
        )
