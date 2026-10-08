"""The three ways an order gets paid, ready to pick on day one.

Payment terms were a catalogue somebody had to fill in before a work order
could say anything about money, and a purchase order could not say it at
all. These are the three the business actually uses. They are ordinary
rows, so the list stays the client's to extend from Setup.
"""

from django.db import migrations

TERMS = [
    {
        "name": "100% on delivery",
        "code": "ON_DELIVERY",
        "days": 0,
        "description": "The whole amount falls due when the goods or the work are delivered.",
    },
    {
        "name": "100% in advance",
        "code": "ADVANCE",
        "days": 0,
        "description": "Paid in full before the supplier starts.",
    },
    {
        "name": "50% advance, 50% on delivery",
        "code": "MILESTONE_50",
        "days": 0,
        "description": "Half up front to begin, the balance on delivery.",
    },
]


def seed(apps, schema_editor):
    PaymentTerms = apps.get_model("setup", "PaymentTerms")
    for term in TERMS:
        # Named terms a client has already set up are left exactly as they are.
        PaymentTerms.objects.get_or_create(name=term["name"], defaults=term)


def unseed(apps, schema_editor):
    """Undo: drop only the three this added, and only if nothing uses them."""
    PaymentTerms = apps.get_model("setup", "PaymentTerms")
    for term in TERMS:
        row = PaymentTerms.objects.filter(name=term["name"], code=term["code"]).first()
        if row is None:
            continue
        if row.work_orders.exists() or row.purchase_orders.exists():
            continue
        row.delete()


class Migration(migrations.Migration):

    dependencies = [
        ("setup", "0016_seed_request_numbering"),
        ("procurement", "0013_purchaseorder_payment_terms"),
    ]

    operations = [
        migrations.RunPython(seed, unseed),
    ]
