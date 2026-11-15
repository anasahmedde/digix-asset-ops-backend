from decimal import Decimal

from django.db import migrations
from django.db.models import Sum


def itemise(apps, schema_editor):
    """Invoices used to carry a typed "paid" figure. Payments are now the
    record, so whatever was typed beyond the payments becomes one payment."""
    Invoice = apps.get_model("finance", "Invoice")
    Payment = apps.get_model("finance", "Payment")
    for inv in Invoice.objects.filter(paid_amount__gt=0):
        listed = Payment.objects.filter(invoice=inv).aggregate(s=Sum("amount"))["s"] or Decimal("0")
        missing = inv.paid_amount - listed
        if missing > 0:
            Payment.objects.create(
                invoice=inv, amount=missing, payment_date=inv.issue_date, kind="payment",
                notes="Paid before payments were listed one by one",
            )


class Migration(migrations.Migration):
    dependencies = [("finance", "0005_payables_and_receivables")]
    operations = [migrations.RunPython(itemise, migrations.RunPython.noop)]
