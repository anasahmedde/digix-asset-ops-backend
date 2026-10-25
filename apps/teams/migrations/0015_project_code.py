"""Every project gets its own number (PRJ-2026-0001). Projects that exist
already are numbered in the order they were created, then the column becomes
unique and the numbering carries on from there."""
from django.db import migrations, models


def number_existing(apps, schema_editor):
    Project = apps.get_model("teams", "Project")
    Scheme = apps.get_model("setup", "NumberingScheme")
    scheme = Scheme.objects.filter(entity="project").first()
    prefix, sep, with_year, pad = ("PRJ", "-", True, 4) if scheme is None else (
        scheme.prefix, scheme.separator, scheme.include_year, scheme.padding)
    n = scheme.next_number if scheme else 1
    for project in Project.objects.filter(project_code="").order_by("created_at"):
        parts = [prefix, str(project.created_at.year) if with_year else "", str(n).zfill(pad)]
        Project.objects.filter(pk=project.pk).update(project_code=sep.join(p for p in parts if p))
        n += 1
    if scheme is not None:
        scheme.next_number = n
        scheme.save(update_fields=["next_number"])


class Migration(migrations.Migration):
    dependencies = [("teams", "0014_project_activity"), ("setup", "0021_seed_warranty_claim_numbering")]
    operations = [
        # Added plain first: the unique index is created once, by the AlterField below.
        migrations.AddField(
            model_name="project", name="project_code",
            field=models.CharField(blank=True, editable=False, max_length=50),
        ),
        migrations.RunPython(number_existing, migrations.RunPython.noop),
        migrations.AlterField(
            model_name="project", name="project_code",
            field=models.CharField(blank=True, db_index=True, editable=False, max_length=50, unique=True),
        ),
    ]
