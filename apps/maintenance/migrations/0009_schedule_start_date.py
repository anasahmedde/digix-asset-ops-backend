"""A schedule records the day its rounds begin.

Existing schedules only ever recorded the next one due, so that date is the
best available answer for when they started — it is the earliest day any of
them is known to have been arranged for.
"""
from django.db import migrations, models
from django.db.models import F


def the_next_round_is_the_best_guess_at_the_first(apps, schema_editor):
    MaintenanceSchedule = apps.get_model("maintenance", "MaintenanceSchedule")
    MaintenanceSchedule.objects.filter(start_date__isnull=True).update(
        start_date=F("next_due"),
    )


def forget_it_again(apps, schema_editor):
    MaintenanceSchedule = apps.get_model("maintenance", "MaintenanceSchedule")
    MaintenanceSchedule.objects.update(start_date=None)


class Migration(migrations.Migration):

    dependencies = [("maintenance", "0008_two_kinds_of_maintenance")]

    operations = [
        migrations.AddField(
            model_name="maintenanceschedule",
            name="start_date",
            field=models.DateField(blank=True, null=True),
        ),
        migrations.RunPython(
            the_next_round_is_the_best_guess_at_the_first, forget_it_again,
        ),
    ]
