"""The reason work went back becomes the reviewer's own words.

The old field held one of six codes. A code cannot say what was actually
wrong, so the field is free text now — and the codes already stored are
rewritten as the labels they were always displayed as, because a screen
that suddenly reads "parts_awaited" has lost information, not gained it.
"""
from django.db import migrations, models

WAS = {
    "parts_awaited": "Parts awaited",
    "vendor_support": "Vendor support needed",
    "site_access": "Client/site access issue",
    "needs_replacement": "Needs full replacement",
    "not_satisfactory": "Work not satisfactory",
    "other": "Other",
}


def spell_them_out(apps, schema_editor):
    Visit = apps.get_model("maintenance", "MaintenanceVisit")
    for code, label in WAS.items():
        Visit.objects.filter(review_reason=code).update(review_reason=label)


def put_the_codes_back(apps, schema_editor):
    """Anything written since is truncated to fit the old 20-char column."""
    Visit = apps.get_model("maintenance", "MaintenanceVisit")
    for code, label in WAS.items():
        Visit.objects.filter(review_reason=label).update(review_reason=code)
    # A free-text reason has no code to go back to, so it becomes "other" —
    # the escape hatch the old list carried for exactly this.
    Visit.objects.exclude(review_reason="").exclude(
        review_reason__in=list(WAS)
    ).update(review_reason="other")


class Migration(migrations.Migration):

    dependencies = [
        ("maintenance", "0019_corrective_jobs_become_visits"),
    ]

    operations = [
        migrations.AlterField(
            model_name="maintenancevisit",
            name="review_reason",
            field=models.TextField(blank=True, default=""),
        ),
        migrations.RunPython(spell_them_out, put_the_codes_back),
    ]
