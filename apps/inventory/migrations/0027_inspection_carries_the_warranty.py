from django.db import migrations, models


class Migration(migrations.Migration):
    """The vendor warranty typed at inspection is kept on the GRN line, so the
    units the store receives carry it instead of arriving with none."""

    dependencies = [("inventory", "0026_lines_already_stocked_say_so")]

    operations = [
        migrations.AddField(
            model_name="goodsreceiptline",
            name="warranty_months",
            field=models.PositiveSmallIntegerField(blank=True, null=True),
        ),
    ]
