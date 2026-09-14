from django.db import migrations

# Client-requested defaults: unassigned >24h -> Group Head (+ Operations
# notified); response-SLA and due-date breaches follow the same ladder.
DEFAULTS = [
    {"trigger": "assignment_sla", "hours": 24, "escalate_to_role": "group_head", "also_notify_role": "ops_manager"},
    {"trigger": "response_sla", "hours": None, "escalate_to_role": "group_head", "also_notify_role": "ops_manager"},
    {"trigger": "due_date", "hours": None, "escalate_to_role": "group_head", "also_notify_role": "ops_manager"},
]


def seed(apps, schema_editor):
    EscalationPolicy = apps.get_model("setup", "EscalationPolicy")
    for row in DEFAULTS:
        EscalationPolicy.objects.get_or_create(trigger=row["trigger"], defaults=row)


def unseed(apps, schema_editor):
    EscalationPolicy = apps.get_model("setup", "EscalationPolicy")
    EscalationPolicy.objects.filter(trigger__in=[r["trigger"] for r in DEFAULTS]).delete()


class Migration(migrations.Migration):
    dependencies = [
        ("setup", "0003_escalationpolicy"),
    ]

    operations = [
        migrations.RunPython(seed, unseed),
    ]
