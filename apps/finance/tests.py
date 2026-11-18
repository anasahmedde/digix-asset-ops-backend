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
    assert [g["key"] for g in o["inventory"]["groups"]] == ["material", "stock", "rented", "foc_sample", "sample_paid", "office_display"]

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


@pytest.mark.django_db
def test_supplier_invoices_client_pos_auto_draft_filters_and_exports():
    from apps.assets.models import AssetType, Device
    from apps.quotations.models import Quotation, QuotationItem

    today = timezone.localdate()
    admin = _client(User.objects.create_user(username="fin-adm", password="x", role="super_admin"))
    supplier, other = Supplier.objects.create(name="Bill Supplier"), Supplier.objects.create(name="Other Supplier")
    po = PurchaseOrder.objects.create(supplier=supplier, status=PurchaseOrder.Status.ORDERED,
                                      total_amount=Decimal("1000"), order_date=today)
    old = PurchaseOrder.objects.create(supplier=other, status=PurchaseOrder.Status.RECEIVED,
                                       total_amount=Decimal("500"), order_date=today - timedelta(days=90))

    # A supplier's invoice against its PO: their number, recorded as it came.
    bill = {"purchase_order": str(po.id), "amount": "600", "tax_amount": "0",
            "issue_date": str(today), "due_date": str(today + timedelta(days=30))}
    assert admin.post("/api/finance/invoices/", bill, format="json").status_code == 400, "needs their number"
    r = admin.post("/api/finance/invoices/", bill | {"reference": "SUP-INV-77"}, format="json")
    assert r.status_code == 201, r.content
    body = r.json()
    assert (body["invoice_type"], body["status"], body["supplier"], body["po_number"]) == ("payable", "approved", str(supplier.id), po.po_number)
    admin.post("/api/finance/payments/", {"purchase_order": str(old.id), "amount": "500", "payment_date": str(today)}, format="json")

    rows = {x["number"]: x for x in admin.get("/api/finance/payables/").json()["rows"]}
    assert (rows[po.po_number]["invoiced"], rows[po.po_number]["invoices"][0]["reference"]) == (600, "SUP-INV-77")
    assert (rows[po.po_number]["pay_status"], rows[old.po_number]["pay_status"]) == ("unpaid", "paid")

    def numbers(**params):
        data = admin.get("/api/finance/payables/", params).json()
        return sorted(x["number"] for x in data["rows"]), data["totals"]

    assert numbers(status="paid")[0] == [old.po_number]
    assert numbers(party=str(supplier.id))[0] == [po.po_number]
    assert numbers(date_from=str(today - timedelta(days=7)))[0] == [po.po_number]
    found, totals = numbers(q="sup-inv-77")
    assert found == [po.po_number] and totals["PKR"]["total"] == 1000, "the totals follow the filters"
    for fmt, kind in (("xlsx", "spreadsheet"), ("csv", "text/csv"), ("pdf", "application/pdf")):
        f = admin.get("/api/finance/payables/", {"status": "paid", "export": fmt})
        assert f.status_code == 200 and kind in f["Content-Type"], fmt
    assert old.po_number.encode() in admin.get("/api/finance/payables/", {"status": "paid", "export": "csv"}).content
    assert admin.get("/api/finance/inventory/", {"export": "xlsx"}).status_code == 200

    # Accepting a quotation drafts its invoice, items and all.
    client = Client.objects.create(name="PO Client")
    q = Quotation.objects.create(title="Wall", client=client, status=Quotation.Status.SENT)
    QuotationItem.objects.create(quotation=q, description="LED wall", quantity=2, unit_price=Decimal("2500"))
    q.recalc_total()
    assert admin.post(f"/api/quotations/quotations/{q.id}/transition/", {"status": "accepted"}, format="json").status_code == 200
    draft = Invoice.objects.get(quotation=q)
    assert (draft.status, draft.amount, draft.client_id) == ("draft", Decimal("5000"), client.id)
    r = admin.post(f"/api/finance/client-po/{q.id}/", {"client_po_number": "CPO-9", "client_po_amount": "4000"}, format="json")
    assert r.status_code == 200, r.content
    assert (r.json()["po_left"], r.json()["client_po_number"]) == (0, "CPO-9"), "the draft already bills 5000 of a 4000 PO"
    assert admin.get(f"/api/finance/invoices/{draft.id}/print/").status_code == 200

    # The asset's category is set at registration; rented stock is still in hand.
    kind = AssetType.objects.create(name="Fin Screen")
    d = Device.objects.create(asset_type=kind, asset_code="FIN-HELD-1", status=Device.Status.IN_STOCK, held_as="rented")
    assert admin.get(f"/api/assets/devices/{d.id}/").json()["held_as_display"] == "Rented"
    stock = next(g for g in admin.get("/api/finance/inventory/").json()["groups"] if g["key"] == "stock")
    assert "FIN-HELD-1" in [line["asset_code"] for line in stock["lines"]]


@pytest.mark.django_db
def test_bugs_18_nov_discount_due_terms_selected_export_projects_and_placed_assets():
    """Bugs and Fixes 18-Nov: a discounted supplier invoice paid in full
    settles its PO; the PO's terms days for the due date; exporting ticked
    rows; the project column; rented and sample assets out at a site still
    count in inventory, sold ones do not."""
    from apps.assets.models import AssetType, Device
    from apps.setup.models import PaymentTerms
    from apps.teams.models import Project

    today = timezone.localdate()
    admin = _client(User.objects.create_user(username="fin-bug", password="x", role="super_admin"))
    supplier = Supplier.objects.create(name="Discount Supplier")
    terms = PaymentTerms.objects.create(name="Net 30", days=30)
    po = PurchaseOrder.objects.create(supplier=supplier, status=PurchaseOrder.Status.ORDERED, payment_terms=terms,
                                      total_amount=Decimal("1000"), order_date=today - timedelta(days=40))
    other = PurchaseOrder.objects.create(supplier=supplier, status=PurchaseOrder.Status.ORDERED,
                                         total_amount=Decimal("300"), order_date=today)

    bill = {"purchase_order": str(po.id), "reference": "D-1", "amount": "1000", "discount_amount": "100",
            "issue_date": str(today), "due_date": str(today - timedelta(days=1))}
    assert admin.post("/api/finance/invoices/", bill, format="json").status_code == 400, "due before issue"
    too_much = admin.post("/api/finance/invoices/", bill | {"discount_amount": "1001", "due_date": str(today)}, format="json")
    assert too_much.status_code == 400
    r = admin.post("/api/finance/invoices/", bill | {"due_date": str(today + timedelta(days=30))}, format="json")
    assert r.status_code == 201 and Decimal(r.json()["total_amount"]) == Decimal("900"), r.content
    row = next(x for x in admin.get("/api/finance/payables/").json()["rows"] if x["id"] == str(po.id))
    assert (row["discount"], row["balance"], row["terms_days"]) == (100, 900, 30)
    assert admin.post("/api/finance/payments/", {"purchase_order": str(po.id), "amount": "901", "payment_date": str(today)},
                      format="json").status_code == 400
    assert admin.post("/api/finance/payments/", {"purchase_order": str(po.id), "amount": "900", "payment_date": str(today)},
                      format="json").status_code == 201
    row = next(x for x in admin.get("/api/finance/payables/", {"status": ""}).json()["rows"] if x["id"] == str(po.id))
    assert (row["pay_status"], row["balance"]) == ("paid", 0), "paid in full once the discounted invoice is"

    # Export only the ticked rows; an order for no project says what it was for.
    picked = admin.get("/api/finance/payables/", {"status": "", "ids": str(other.id), "export": "csv"}).content.decode("utf-8-sig")
    assert other.po_number in picked and po.po_number not in picked
    assert "Project" in picked.splitlines()[0] and "Discount" in picked.splitlines()[0]

    kind = AssetType.objects.create(name="Bug Screen")
    rental = Project.objects.create(name="Rent Proj", contract_type="rental")
    sold = Project.objects.create(name="Sold Proj", contract_type="sold")
    Device.objects.create(asset_type=kind, asset_code="BUG-RENT", status=Device.Status.ACTIVE, project=rental)
    Device.objects.create(asset_type=kind, asset_code="BUG-FOC", status=Device.Status.INSTALLED, held_as="foc_sample")
    Device.objects.create(asset_type=kind, asset_code="BUG-SOLD", status=Device.Status.ACTIVE, project=sold)
    groups = {g["key"]: [l["asset_code"] for l in g["lines"]] for g in admin.get("/api/finance/inventory/").json()["groups"]}
    assert "BUG-RENT" in groups["rented"] and "BUG-FOC" in groups["foc_sample"]
    assert "BUG-SOLD" not in sum((v for k, v in groups.items() if k != "material"), [])


@pytest.mark.django_db
def test_qa_18_nov_markup_pdfs_bad_amounts_due_dates_moved_payments_and_bad_filters():
    """API QA 18-Nov: markup in text never breaks a PDF; no zero, negative or
    PO-less supplier invoices; a payable is not due before it was ordered; a
    payment moved off an invoice re-syncs it; bad filter ids answer 400."""
    today = timezone.localdate()
    admin = _client(User.objects.create_user(username="fin-qa", password="x", role="super_admin"))
    head = _client(User.objects.create_user(username="fin-qa-gh", password="x", role="group_head"))
    client = Client.objects.create(name="Panel <b> & Co")
    base = {"client": str(client.id), "issue_date": str(today), "due_date": str(today), "description": "Unclosed <b> tag"}
    for bad in ({"amount": "-50"}, {"amount": "0"}, {"amount": "100", "tax_amount": "-150"}):
        assert admin.post("/api/finance/invoices/", base | bad, format="json").status_code == 400, bad
    assert admin.post("/api/finance/invoices/", base | {"amount": "777", "invoice_type": "payable"}, format="json").status_code == 400
    ids = []
    for n in range(2):
        r = admin.post("/api/finance/invoices/", base | {"amount": "100", "notes": "a < b"}, format="json")
        assert r.status_code == 201, r.content
        ids.append(r.json()["id"])
        assert head.post(f"/api/finance/invoices/{ids[-1]}/approve/").status_code == 200
    assert admin.get(f"/api/finance/invoices/{ids[0]}/print/").status_code == 200
    assert admin.get("/api/finance/receivables/", {"export": "pdf"}).status_code == 200

    pay = admin.post("/api/finance/payments/", {"invoice": ids[0], "amount": "60", "payment_date": str(today)}, format="json").json()
    assert admin.patch(f"/api/finance/payments/{pay['id']}/", {"invoice": ids[1]}, format="json").status_code == 200
    left = Invoice.objects.get(pk=ids[0])
    assert (left.status, left.paid_amount) == ("approved", 0), "the invoice it left is re-synced"

    po = PurchaseOrder.objects.create(supplier=Supplier.objects.create(name="Due Sup"), status=PurchaseOrder.Status.ORDERED,
                                      total_amount=Decimal("10"), order_date=today)
    early = admin.post(f"/api/finance/payables/{po.id}/due-date/", {"payment_due_date": str(today - timedelta(days=1))}, format="json")
    assert early.status_code == 400
    assert admin.get("/api/finance/payables/", {"party": "not-a-uuid", "export": "csv"}).status_code == 200
    assert admin.post("/api/finance/inventory/hold/", {"device": "abc", "held_as": "stock"}, format="json").status_code == 404
    for bad in ({"type": "assets", "client": "x"}, {"type": "visits", "date_from": "garbage"}, {"type": "assets", "warranty": "bogus"}):
        assert admin.get("/api/reports/generate/", bad).status_code == 400, bad
