"""Management command that runs the hourly events sync from cron."""

from django.core.management.base import BaseCommand, CommandError

from events.services import SyncError, sync_events


class Command(BaseCommand):
    """Fetch the NYC Parks events feed and sync it into the database."""

    help = (
        "Fetch NYC Parks events from the open data feed, upsert changes, "
        "and mark events done once their end time has passed."
    )

    def handle(self, *args, **options):
        """Run one sync and print a summary, or fail loudly for cron."""
        try:
            counts = sync_events()
        except SyncError as error:
            raise CommandError(str(error)) from error
        self.stdout.write(
            self.style.SUCCESS(
                "Event sync complete: {fetched} fetched, {created} created, "
                "{updated} updated, {unchanged} unchanged, "
                "{marked_done} marked done.".format(**counts)
            )
        )
