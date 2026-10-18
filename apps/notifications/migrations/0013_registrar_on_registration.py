"""Who registered each asset, for assets registered before it was recorded.

The registration line was written without a person. The notice the
registrar's line received ("Asset registered: DGX-... - by <name>") names
them; where one was sent, the name goes on the registration.
"""
from django.db import migrations


def name_the_registrar(apps, schema_editor):
    Event = apps.get_model("assets", "DeviceLifecycleEvent")
    AuditLog = apps.get_model("accounts", "AuditLog")
    Notification = apps.get_model("notifications", "Notification")
    User = apps.get_model("accounts", "User")

    people = {}
    for u in User.objects.all():
        full = f"{u.first_name} {u.last_name}".strip()
        for key in {full, u.username}:
            if key:
                people.setdefault(key, set()).add(u.pk)

    for ev in Event.objects.filter(description="Registered", performed_by__isnull=True):
        notice = Notification.objects.filter(ref=f"fyi-device:{ev.device_id}").order_by("created_at").first()
        if notice is None or " by " not in (notice.message or ""):
            continue
        name = notice.message.rsplit(" by ", 1)[1].strip()
        match = people.get(name, set())
        if len(match) != 1:
            continue
        uid = next(iter(match))
        Event.objects.filter(pk=ev.pk).update(performed_by_id=uid)
        AuditLog.objects.filter(
            resource_type="device", resource_id=str(ev.device_id), action="create", user__isnull=True,
        ).update(user_id=uid)


class Migration(migrations.Migration):

    dependencies = [
        ("notifications", "0012_note_replies"),
        ("assets", "0032_alter_device_status"),
        ("accounts", "0015_menu_order"),
    ]

    operations = [migrations.RunPython(name_the_registrar, migrations.RunPython.noop)]
