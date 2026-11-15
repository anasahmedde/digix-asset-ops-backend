from datetime import timedelta
from decimal import Decimal

import pytest
from django.utils import timezone
from rest_framework.test import APIClient

from apps.accounts.models import User
from apps.clients.models import Client
from apps.finance.models import Invoice
from apps.procurement.models import PurchaseOrder
from apps.suppliers.models import Supplier


def _client(user):
    c = APIClient()
    c.force_authenticate(user)
    return c


@pytest.mark.django_db
def test_payables_receivables_and_net_worth_on_the_financial_dashboard():
    today = timezone.localdate()
    finance = _client(User.objects.create_user(username="fin-1", password="x", role="finance"))
    head = _client(User.objects.create_user(username="fin-gh", password="x", role="group_head"))
    tech = _client(User.objects.create_user(username="fin-tech", password="x", role="technician"))

    # Payable: an approved order paid in an advance and two payments.
    supplier = Supplier.objects.create(name="Fin Supplier")
    po = PurchaseOrder.objects.create(
        supplier=supplier, status=PurchaseOrder.Status.APPROVED, total_amount=Decimal("1000"),
        order_date=today - timedelta(days=40), payment_due_date=today - timedelta(days=10),
    )
    PurchaseOrder.objects.create(supplier=supplier, status=PurchaseOrder.Status.DRAFT, total_amount=Decimal("999"))
    for kind, amount, ago in (("advance", "200", 30), ("payment", "300", 20), ("payment", "100", 5)):
        r = finance.post("/api/finance/payments/", {
            "purchase_order": str(po.id), "kind": kind, "amount": amount,
            "payment_date": str(today - timedelta(days=ago)),
        }, format="json")
        assert r.status_code == 201, r.content
    over = finance.post("/api/finance/payments/", {
        "purchase_order": str(po.id), "amount": "401", "payment_date": str(today),
    }, format="json")
    assert over.status_code == 400, "never more than is owed"

    rows = finance.get("/api/finance/payables/").json()["rows"]
    assert [r["number"] for r in rows] == [po.po_number], "a draft order is not owed"
    row = rows[0]
    assert (row["advance"], row["first"], row["second"], row["third"]) == (200, 300, 100, None)
    assert row["balance"] == 400 and row["overdue_days"] == 10 and row["aging"] == "1_30"

    # Receivable: only once approved, and only by whoever may approve.
    client = Client.objects.create(name="Fin Client")
    r = finance.post("/api/finance/invoices/", {
        "invoice_type": "receivable", "client": str(client.id), "amount": "1500", "tax_amount": "0",
        "issue_date": str(today - timedelta(days=5)), "due_date": str(today + timedelta(days=25)),
    }, format="json")
    assert r.status_code == 201, r.content
    inv = r.json()
    assert finance.get("/api/finance/receivables/").json()["rows"] == [], "a draft is not owed to us"
    assert finance.post(f"/api/finance/invoices/{inv['id']}/approve/").status_code == 403
    assert head.post(f"/api/finance/invoices/{inv['id']}/approve/").status_code == 200
    r = finance.post("/api/finance/payments/", {
        "invoice": inv["id"], "kind": "advance", "amount": "500", "payment_date": str(today),
    }, format="json")
    assert r.status_code == 201, r.content
    assert Invoice.objects.get(pk=inv["id"]).status == Invoice.Status.PARTIALLY_PAID
    assert finance.patch(f"/api/finance/invoices/{inv['id']}/", {"amount": "9"}, format="json").status_code == 400

    # Net worth: what we are owed less what we owe.
    o = finance.get("/api/finance/overview/").json()
    assert o["receivable"]["PKR"]["balance"] == 1000 and o["payable"]["PKR"]["balance"] == 400
    assert o["net_worth"]["PKR"] == 600
    assert [g["key"] for g in o["inventory"]["groups"]] == ["material", "stock", "office_display", "foc_sample"]

    # It is for management: the field does not read it.
    assert tech.get("/api/finance/overview/").status_code == 403


@pytest.mark.django_db
def test_a_quotation_is_invoiced_in_parts_and_each_part_is_a_receivable_once_approved():
    from apps.quotations.models import Quotation

    today = timezone.localdate()
    finance = _client(User.objects.create_user(username="fin-q", password="x", role="finance"))
    head = _client(User.objects.create_user(username="fin-q-gh", password="x", role="group_head"))
    client = Client.objects.create(name="Quoted Client")
    other = Client.objects.create(name="Somebody Else")
    q = Quotation.objects.create(title="Screens", client=client, currency="AED", total_amount=Decimal("10000"))

    def raise_invoice(amount, **extra):
        return finance.post("/api/finance/invoices/", {
            "quotation": str(q.id), "description": "Advance 30%", "amount": amount, "tax_amount": "0",
            "issue_date": str(today), "due_date": str(today + timedelta(days=15)), **extra,
        }, format="json")

    assert raise_invoice("3000").status_code == 400, "only an accepted quotation is invoiced"
    q.status = Quotation.Status.ACCEPTED
    q.save()
    r = raise_invoice("3000", client=str(other.id), currency="PKR")
    assert r.status_code == 201, r.content
    inv = r.json()
    assert (inv["client"], inv["currency"], inv["quote_number"]) == (str(client.id), "AED", q.quote_number), "billed to the quotation's client"
    assert raise_invoice("7000.01").status_code == 400, "never more than the quotation"
    assert raise_invoice("7000").status_code == 201

    # Each part counts once approved, against the quotation it bills.
    assert head.post(f"/api/finance/invoices/{inv['id']}/approve/").status_code == 200
    data = finance.get("/api/finance/receivables/").json()
    assert [(r["number"], r["quote_number"], r["balance"]) for r in data["rows"]] == [(inv["invoice_number"], q.quote_number, 3000)]
    assert data["uninvoiced"] == {}, "all of it is invoiced, drafts included"

    seen = finance.get(f"/api/quotations/quotations/{q.id}/").json()["billing"]
    assert (seen["invoiced"], seen["to_invoice"]) == (10000, 0)
    pdf = finance.get(f"/api/finance/invoices/{inv['id']}/print/")
    assert pdf.status_code == 200 and pdf["Content-Type"] == "application/pdf"
