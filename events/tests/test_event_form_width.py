"""The Post an event form uses more of the screen (#184)."""

from django.contrib.auth import get_user_model
from django.contrib.staticfiles import finders
from django.test import TestCase
from django.urls import reverse


def page_rule():
    """Return the CSS block that sizes the event form page."""
    with open(finders.find("events/event_form.css"), encoding="utf-8") as css:
        text = css.read()
    block = text[text.index(".event-form-page {") :]
    return block[: block.index("}")]


class EventFormWidthTests(TestCase):
    """The form box is wider, and the page still loads with its stylesheet."""

    def test_form_page_matches_the_header_width(self):
        """The page uses the site's page width instead of the old 720px."""
        rule = page_rule()

        self.assertIn("max-width: var(--page-width, 1200px);", rule)
        self.assertNotIn("720px", rule)

    def test_post_an_event_page_uses_the_stylesheet(self):
        """The create page renders and loads event_form.css."""
        user = get_user_model().objects.create_user(
            "widehost", "widehost@example.com", "pass12345"
        )
        self.client.force_login(user)

        response = self.client.get(reverse("event_create"))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "events/event_form.css")
        self.assertContains(response, 'class="event-form-page"')
