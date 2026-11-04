"""Tests for the sync_events management command."""

from io import StringIO
from unittest.mock import patch

from django.core.management import CommandError, call_command
from django.test import TestCase

from events.services import SyncError

COUNTS = {
    "fetched": 852,
    "created": 3,
    "updated": 2,
    "unchanged": 847,
    "marked_done": 5,
}


class SyncEventsCommandTests(TestCase):
    """The command prints a summary or fails loudly for cron."""

    def test_successful_sync_prints_counts(self):
        """A good run reports every count from the sync service."""
        out = StringIO()
        with patch(
            "events.management.commands.sync_events.sync_events",
            return_value=COUNTS,
        ) as mocked:
            call_command("sync_events", stdout=out)

        mocked.assert_called_once()
        self.assertIn("Event sync complete: 852 fetched", out.getvalue())
        self.assertIn("3 created", out.getvalue())
        self.assertIn("2 updated", out.getvalue())
        self.assertIn("847 unchanged", out.getvalue())
        self.assertIn("5 marked done", out.getvalue())

    def test_sync_failure_becomes_a_command_error(self):
        """SyncError is surfaced as CommandError so cron sees a non-zero exit."""
        with patch(
            "events.management.commands.sync_events.sync_events",
            side_effect=SyncError("feed unreachable"),
        ):
            with self.assertRaisesMessage(CommandError, "feed unreachable"):
                call_command("sync_events", stdout=StringIO())
