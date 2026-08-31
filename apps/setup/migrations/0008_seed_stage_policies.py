"""Wave 4: multi-stage escalation ladders.

Existing rows became (scope=ticket, stage=1) via the field defaults in 0007.
Here we seed the stage-2 ticket ladders (fire 24h after the stage-1 anchor
offset) and the installation due-date ladder (stage 1 the day after the due
date, stage 2 24h later).
"""

from django.db import migrations


def seed(apps, schema_editor):
    EscalationPolicy = apps.get_model("setup", "EscalationPolicy")

    # Ticket stage-2 rows: 24h after the stage-1 offset, same recipients.
    for trigger in ("response_sla", "due_date"):
        stage1 = EscalationPolicy.objects.filter(
            scope="ticket", trigger=trigger, stage=1
        ).first()
        base_hours = (stage1.hours or 0) if stage1 else 0
        EscalationPolicy.objects.get_or_create(
            scope="ticket", trigger=trigger, stage=2,
            defaults={
                "hours": base_hours + 24,
                "escalate_to_role": "group_head",
                "also_notify_role": "ops_manager",
            },
        )

    # Installation due-date ladder.
    EscalationPolicy.objects.get_or_create(
        scope="installation", trigger="due_date", stage=1,
        defaults={
            "hours": 0,
            "escalate_to_role": "ops_manager",
            "also_notify_role": "",
        },
    )
    EscalationPolicy.objects.get_or_create(
        scope="installation", trigger="due_date", stage=2,
        defaults={
            "hours": 24,
            "escalate_to_role": "group_head",
            "also_notify_role": "ops_manager",
        },
    )


def unseed(apps, schema_editor):
    EscalationPolicy = apps.get_model("setup", "EscalationPolicy")
    EscalationPolicy.objects.filter(scope="ticket", stage=2).delete()
    EscalationPolicy.objects.filter(scope="installation").delete()


def grandfather_existing_breaches(apps, schema_editor):
    """Suppress the first-run escalation storm.

    Tickets the legacy single-stage engine already escalated get their ladder
    keys pre-filled (both stages), and installations already overdue at deploy
    time are stamped as fully escalated — only breaches that happen AFTER this
    deploy notify people. Without this, the first beat tick would email the
    Group Head about every historical breach at once.
    """
    from django.db.models import Q
    from django.utils import timezone

    now = timezone.now().isoformat()
    Ticket = apps.get_model("tickets", "Ticket")
    DeviceInstallation = apps.get_model("sites", "DeviceInstallation")

    legacy_map = {
        "escalated": "response_sla",
        "assignment_escalated": "assignment_sla",
        "due_date_escalated": "due_date",
    }
    legacy_any = Q(escalated=True) | Q(assignment_escalated=True) | Q(due_date_escalated=True)
    for ticket in Ticket.objects.filter(legacy_any).iterator():
        state = dict(ticket.escalation_state or {})
        changed = False
        for flag, trigger in legacy_map.items():
            if getattr(ticket, flag, False):
                for stage in (1, 2):
                    key = f"{trigger}:{stage}"
                    if key not in state:
                        state[key] = now
                        changed = True
        if changed:
            ticket.escalation_state = state
            ticket.save(update_fields=["escalation_state"])

    today = timezone.localdate()
    for installation in DeviceInstallation.objects.filter(
        completed_at__isnull=True, due_date__isnull=False, due_date__lt=today
    ).iterator():
        state = dict(installation.escalation_state or {})
        changed = False
        for stage in (1, 2):
            key = f"due_date:{stage}"
            if key not in state:
                state[key] = now
                changed = True
        if changed:
            installation.escalation_state = state
            installation.save(update_fields=["escalation_state"])


class Migration(migrations.Migration):
    dependencies = [
        ("setup", "0007_alter_escalationpolicy_options_and_more"),
        ("tickets", "0015_ticket_escalation_state"),
        ("sites", "0008_deviceinstallation_escalation_state"),
    ]

    operations = [
        migrations.RunPython(seed, unseed),
        migrations.RunPython(grandfather_existing_breaches, migrations.RunPython.noop),
    ]
