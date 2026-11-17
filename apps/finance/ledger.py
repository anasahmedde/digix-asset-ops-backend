"""The Financial module's arithmetic: what the company owes, what it is owed,
and what it holds. Kept in one place so the dashboard and the lists under it
can never disagree."""
from collections import defaultdict
from datetime import timedelta
from decimal import Decimal

from django.utils import timezone

from apps.assets.costs import money

from .models import Invoice, Payment

ZERO = Decimal("0.00")

# How late an unpaid balance is, in the buckets an aging report uses.
AGING = (
    ("current", "Not yet due"),
    ("1_30", "1-30 days"),
    ("31_60", "31-60 days"),
    ("61_90", "61-90 days"),
    ("90_plus", "Over 90 days"),
)


def aging_bucket(days: int) -> str:
    if days <= 0:
        return "current"
    if days <= 30:
        return "1_30"
    if days <= 60:
        return "31_60"
    if days <= 90:
        return "61_90"
    return "90_plus"


def split_payments(payments) -> dict:
    """The advance, then the 1st, 2nd and 3rd payments after it, in the order
    they were made. A 4th and later are added to the 3rd and counted, so
    the columns still add up to what was paid."""
    pays = sorted(payments, key=lambda p: (p.payment_date, p.created_at))
    advance = sum((p.amount for p in pays if p.kind == Payment.Kind.ADVANCE), ZERO)
    rest = [p.amount for p in pays if p.kind != Payment.Kind.ADVANCE]
    return {
        "advance": money(advance),
        "first": money(rest[0]) if len(rest) > 0 else None,
        "second": money(rest[1]) if len(rest) > 1 else None,
        "third": money(sum(rest[2:], ZERO)) if len(rest) > 2 else None,
        "later_payments": max(0, len(rest) - 3),
        "paid": money(advance + sum(rest, ZERO)),
        "payments": [
            {
                "id": str(p.id), "kind": p.kind, "amount": money(p.amount), "payment_date": p.payment_date,
                "method": p.method, "method_display": p.get_method_display(), "reference": p.reference,
                "notes": p.notes,
            }
            for p in pays
        ],
    }


PAY_STATUS = {"unpaid": "Unpaid", "partially_paid": "Partially Paid", "paid": "Paid"}


def _row(total, payments, due, today) -> dict:
    row = split_payments(payments)
    balance = max(money(total) - row["paid"], ZERO)
    overdue = (today - due).days if due and balance > 0 and today > due else 0
    paid_state = "paid" if balance <= 0 else "partially_paid" if row["paid"] > 0 else "unpaid"
    row.update(
        total=money(total), balance=balance, due_date=due, overdue_days=overdue,
        aging=aging_bucket(overdue) if balance > 0 else None,
        pay_status=paid_state, pay_status_display=PAY_STATUS[paid_state],
    )
    return row


def _invoice_line(inv) -> dict:
    return {
        "id": str(inv.id), "number": inv.invoice_number, "reference": inv.reference,
        "amount": money(inv.amount), "total": money(inv.total_amount), "issue_date": inv.issue_date,
        "due_date": inv.due_date, "status": inv.status, "status_display": inv.get_status_display(),
    }


def po_due_date(po):
    """When the supplier is to be paid, and whether somebody set it.

    Unset, it is read off the order: the order date plus the payment terms'
    days, or the expected delivery when the terms give no days."""
    if po.payment_due_date:
        return po.payment_due_date, True
    base = po.order_date or timezone.localtime(po.created_at).date()
    days = po.payment_terms.days if po.payment_terms_id else 0
    if days:
        return base + timedelta(days=days), False
    return po.expected_delivery or base, False


def payable_orders():
    """Orders the company is committed to pay: signed off, not cancelled."""
    from apps.procurement.models import PurchaseOrder as PO

    return (
        PO.objects.filter(status__in=(PO.Status.APPROVED, PO.Status.ORDERED, PO.Status.PARTIALLY_RECEIVED, PO.Status.RECEIVED))
        .select_related("supplier", "payment_terms")
        .prefetch_related("payments", "invoices", "items__bom_line__project")
    )


def po_projects(po) -> list[dict]:
    """The projects an order buys for: those whose requirements its lines cover."""
    seen = {}
    for item in po.items.all():
        project = item.bom_line.project if item.bom_line_id else None
        if project is not None:
            seen[project.pk] = {"id": str(project.pk), "name": project.name}
    return list(seen.values())


def payables(today=None) -> list[dict]:
    today = today or timezone.localdate()
    rows = []
    for po in payable_orders():
        due, due_set = po_due_date(po)
        row = _row(po.total_amount, po.payments.all(), due, today)
        # The supplier's invoices against it, beside what was ordered.
        bills = [i for i in po.invoices.all() if i.status != Invoice.Status.CANCELLED]
        row.update(
            id=str(po.id), number=po.po_number, party=po.supplier.name, party_id=str(po.supplier_id),
            currency=po.currency, status=po.status, status_display=po.get_status_display(),
            date=po.order_date or timezone.localtime(po.created_at).date(),
            due_date_set=due_set,
            terms=po.payment_terms.name if po.payment_terms_id else po.payment_terms_note,
            projects=po_projects(po),
            invoiced=money(sum((i.total_amount for i in bills), ZERO)),
            invoices=[_invoice_line(i) for i in sorted(bills, key=lambda i: i.issue_date)],
        )
        rows.append(row)
    return rows


def receivable_invoices():
    """Client invoices signed off and not cancelled: money owed to us."""
    return (
        Invoice.objects.filter(invoice_type=Invoice.InvoiceType.RECEIVABLE)
        .exclude(status__in=(Invoice.Status.DRAFT, Invoice.Status.CANCELLED))
        .select_related("client")
        .prefetch_related("payments")
    )


def quotation_billing(quotation, exclude=None) -> dict:
    """How much of an accepted quotation has been invoiced, received, and is
    left to invoice. Cancelled invoices bill nothing; tax is on top."""
    invoices = [
        i for i in quotation.invoices.all()
        if i.status != Invoice.Status.CANCELLED and (exclude is None or i.pk != exclude.pk)
    ]
    invoiced = money(sum((i.amount for i in invoices), ZERO))
    received = money(sum((p.amount for i in invoices for p in i.payments.all()), ZERO))
    total = money(quotation.total_amount)
    po = quotation.client_po_amount
    return {
        "quoted": total, "invoiced": invoiced, "received": received,
        "to_invoice": max(total - invoiced, ZERO), "invoices": len(invoices),
        # The client's PO, and how much of it is not invoiced yet.
        "client_po_number": quotation.client_po_number, "client_po_date": quotation.client_po_date,
        "client_po_amount": money(po) if po is not None else None,
        "po_left": max(money(po) - invoiced, ZERO) if po is not None else None,
    }


def draft_full_invoice(quotation, user=None):
    """On acceptance: the invoice for the whole quotation, drafted from it.

    Client, currency and value come from the quotation and the PDF lists its
    items. It stays a draft until approved, so it can be cut down to an
    advance first, or deleted to bill in parts instead."""
    if quotation.total_amount <= 0 or quotation.invoices.exists():
        return None
    today = timezone.localdate()
    return Invoice.objects.create(
        invoice_type=Invoice.InvoiceType.RECEIVABLE, quotation=quotation, client=quotation.client,
        currency=quotation.currency, description="Full quotation value", amount=quotation.total_amount,
        tax_amount=ZERO, total_amount=quotation.total_amount, issue_date=today, due_date=today + timedelta(days=30),
        created_by=user,
    )


def uninvoiced() -> dict:
    """Per currency: what accepted quotations are worth that has not been
    invoiced yet. It becomes a receivable once invoiced and approved."""
    from apps.quotations.models import Quotation

    out = defaultdict(lambda: {"amount": ZERO, "quotations": 0})
    for q in Quotation.objects.filter(status=Quotation.Status.ACCEPTED).prefetch_related("invoices__payments"):
        left = quotation_billing(q)["to_invoice"]
        if left > 0:
            out[q.currency]["amount"] += left
            out[q.currency]["quotations"] += 1
    return dict(out)


def receivables(today=None) -> list[dict]:
    today = today or timezone.localdate()
    rows = []
    for inv in receivable_invoices().select_related("quotation").prefetch_related("quotation__spawned_projects"):
        row = _row(inv.total_amount, inv.payments.all(), inv.due_date, today)
        q = inv.quotation
        row.update(
            id=str(inv.id), number=inv.invoice_number, party=inv.client.name if inv.client_id else "",
            party_id=str(inv.client_id) if inv.client_id else None,
            currency=inv.currency, status=inv.status, status_display=inv.get_status_display(),
            date=inv.issue_date, due_date_set=True, terms="", description=inv.description,
            quotation=str(inv.quotation_id) if q else None,
            quote_number=q.quote_number if q else None,
            client_po_number=q.client_po_number if q else "",
            projects=[{"id": str(p.pk), "name": p.name} for p in q.spawned_projects.all()] if q else [],
        )
        rows.append(row)
    return rows


STATUS_FILTERS = {
    "outstanding": lambda r: r["balance"] > 0,
    "overdue": lambda r: r["overdue_days"] > 0,
    "unpaid": lambda r: r["pay_status"] == "unpaid",
    "partially_paid": lambda r: r["pay_status"] == "partially_paid",
    "paid": lambda r: r["pay_status"] == "paid",
}


def filter_rows(rows, params) -> list[dict]:
    """Narrow a payable or receivable list the way the Finance tabs and their
    exports do: by date, supplier or client, project, status and a search."""
    from django.utils.dateparse import parse_date

    start, end = parse_date(params.get("date_from") or ""), parse_date(params.get("date_to") or "")
    party, project = params.get("party"), params.get("project")
    status_test = STATUS_FILTERS.get(params.get("status") or "")
    q = (params.get("q") or "").strip().lower()

    def keep(r):
        if start and (r["date"] is None or r["date"] < start):
            return False
        if end and (r["date"] is None or r["date"] > end):
            return False
        if party and r.get("party_id") != party:
            return False
        if project and project not in {p["id"] for p in r.get("projects", [])}:
            return False
        if status_test and not status_test(r):
            return False
        if q:
            words = " ".join(str(r.get(k) or "") for k in ("number", "party", "description", "quote_number", "client_po_number", "terms"))
            words += " " + " ".join(i["reference"] + " " + i["number"] for i in r.get("invoices", []))
            if q not in words.lower():
                return False
        return True

    return [r for r in rows if keep(r)]


def totals(rows) -> dict:
    """Per currency: the value, paid, outstanding, overdue and aging. Amounts
    in different currencies are never added together."""
    out = defaultdict(lambda: {
        "total": ZERO, "paid": ZERO, "balance": ZERO, "overdue": ZERO, "open": 0, "overdue_count": 0,
        "aging": {key: ZERO for key, _ in AGING},
    })
    for r in rows:
        t = out[r["currency"]]
        t["total"] += r["total"]
        t["paid"] += r["paid"]
        t["balance"] += r["balance"]
        if r["balance"] > 0:
            t["open"] += 1
            t["aging"][r["aging"]] += r["balance"]
        if r["overdue_days"] > 0:
            t["overdue"] += r["balance"]
            t["overdue_count"] += 1
    return dict(out)


def sync_invoice(invoice):
    """Paid amount and status follow the payments recorded against it."""
    paid = money(sum((p.amount for p in invoice.payments.all()), ZERO))
    invoice.paid_amount = paid
    if invoice.status not in (Invoice.Status.DRAFT, Invoice.Status.CANCELLED):
        if paid >= money(invoice.total_amount):
            invoice.status = Invoice.Status.PAID
        elif paid > 0:
            invoice.status = Invoice.Status.PARTIALLY_PAID
        elif invoice.status in (Invoice.Status.PAID, Invoice.Status.PARTIALLY_PAID):
            invoice.status = Invoice.Status.APPROVED
    invoice.save(update_fields=["paid_amount", "status", "updated_at"])


# --- Inventory in hand -------------------------------------------------------

def inventory() -> dict:
    """What the company holds, at cost: material in the store (shelf stock,
    pigeon holes and unique units), and the assets it has built or bought
    that are in stock: finished products, office display and FOC samples."""
    from apps.assets.models import Device
    from apps.inventory.models import InventoryItem, InventoryUnit, PigeonHole
    from apps.inventory.serializers import InventoryItemSerializer
    from apps.reports.builders import _asset_costs

    valuer = InventoryItemSerializer(context={})
    materials = []
    for item in InventoryItem.objects.filter(quantity__gt=0).select_related("material_type"):
        value = valuer.get_total_value(item)
        materials.append({
            "id": str(item.id), "name": item.material_type.name, "where": "Store",
            "quantity": item.quantity, "unit": item.material_type.unit,
            "value": money(value) if value is not None else None,
        })
    for hole in PigeonHole.objects.filter(quantity__gt=0, item__isnull=False).select_related("item__material_type", "project"):
        cost = valuer.get_latest_cost(hole.item)
        materials.append({
            "id": str(hole.id), "name": hole.item.material_type.name,
            "where": f"Pigeon hole · {hole.project.name}" + (f" · {hole.rack}" if hole.rack else ""),
            "quantity": hole.quantity, "unit": hole.item.material_type.unit,
            "value": money(hole.quantity * Decimal(str(cost))) if cost is not None else None,
        })
    held = (InventoryUnit.Status.IN_STOCK, InventoryUnit.Status.RESERVED, InventoryUnit.Status.RETURNED)
    for unit in InventoryUnit.objects.filter(status__in=held).select_related("unit_type"):
        materials.append({
            "id": str(unit.id), "name": f"{unit.unit_type.name if unit.unit_type_id else 'Unique item'} · {unit.serial_number}",
            "where": "Pigeon hole" if unit.status == InventoryUnit.Status.RESERVED else "Store",
            "quantity": 1, "unit": "pc",
            "value": money(unit.purchase_price) if unit.purchase_price is not None else None,
        })

    devices = list(Device.objects.filter(status=Device.Status.IN_STOCK).select_related("asset_type").order_by("asset_code"))
    costs = [{} for _ in devices]
    _asset_costs(devices, costs)
    assets = {key: [] for key, _ in Device.HeldAs.choices}
    for d, c in zip(devices, costs):
        # Rented or sold but not yet delivered: still a finished product in hand.
        group = Device.HeldAs.STOCK if d.held_as in (Device.HeldAs.RENTED, Device.HeldAs.SOLD) else d.held_as
        assets[group].append({
            "id": str(d.id), "name": d.display_name or d.asset_code, "asset_code": d.asset_code,
            "asset_type": d.asset_type.name if d.asset_type_id else "", "held_as": d.held_as,
            "value": c["manufacturing"],
        })

    def total(lines):
        return money(sum((line["value"] or ZERO for line in lines), ZERO))

    groups = [
        {"key": "material", "label": "Material", "lines": materials},
        {"key": "stock", "label": "Finished products", "lines": assets["stock"]},
        {"key": "office_display", "label": "Assets on office display", "lines": assets["office_display"]},
        {"key": "foc_sample", "label": "FOC samples", "lines": assets["foc_sample"]},
    ]
    for g in groups:
        g["total"] = total(g["lines"])
        g["unpriced"] = sum(1 for line in g["lines"] if line["value"] is None)
    return {"groups": groups, "total": money(sum((g["total"] for g in groups), ZERO))}
