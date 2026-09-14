from django.core.management.base import BaseCommand

from apps.sites.models import DeviceInstallation
from apps.sites.signals import seed_steps


class Command(BaseCommand):
    help = "Seed the default step checklist for installations that have none."

    def handle(self, *args, **options):
        seeded = 0
        skipped = 0
        for installation in DeviceInstallation.objects.all().iterator():
            created = seed_steps(installation)
            if created:
                seeded += 1
            else:
                skipped += 1
        self.stdout.write(
            self.style.SUCCESS(
                f"Seeded steps for {seeded} installation(s); {skipped} already had steps."
            )
        )
