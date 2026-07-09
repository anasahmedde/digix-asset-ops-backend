from django.db import migrations

# Fault catalogue agreed in the client meeting (2026-07-09). Extensible from
# the Setup screen — these are only the starting set.
ISSUE_TYPES = [
    "Pixel Damage / Off",
    "Module Burnt",
    "Receiving Card Issue",
    "Sending Card Issue",
    "HDMI Cable Issue",
    "Ribbon Cable Issue",
    "Module IC Issue",
    "Media Player Issue",
    "Adapter Issue",
    "Card Issue",
    "Panel Issue",
    "Hardware Issue",
    "Auto Power Off Option Enabled",
    "Auto Start Not Working",
    "Screen Lights Issue",
    "Software Not Working",
    "Internet Connectivity Issue",
    "LAN Cable Not Working",
    "Other",
]


def seed(apps, schema_editor):
    TicketIssueType = apps.get_model("tickets", "TicketIssueType")
    for order, name in enumerate(ISSUE_TYPES, start=1):
        TicketIssueType.objects.get_or_create(name=name, defaults={"sort_order": order})


def unseed(apps, schema_editor):
    TicketIssueType = apps.get_model("tickets", "TicketIssueType")
    TicketIssueType.objects.filter(name__in=ISSUE_TYPES).delete()


class Migration(migrations.Migration):
    dependencies = [("tickets", "0005_ticketissuetype_ticket_assigned_vendor_and_more")]
    operations = [migrations.RunPython(seed, unseed)]
