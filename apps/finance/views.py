from django.db import transaction
from django.utils import timezone
from django.utils.dateparse import parse_date
from rest_framework import serializers, status, viewsets
from rest_framework.decorators import action
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response

from common.permissions import CapabilityGate

from . import ledger
from .models import Invoice, Payment
from .serializers import InvoiceSerializer, PaymentSerializer


class InvoiceViewSet(viewsets.ModelViewSet):
    # An invoice register is money end to end, so reading it is view_prices.
    # The sidebar hid the link and nothing else did — typing the URL worked.
    read_capability = "view_finance"
    write_capability = "manage_finance"
    action_capabilities = {"approve": "approve_invoice", "print_pdf": "view_finance"}
    queryset = (
        Invoice.objects.select_related(
            "client", "supplier", "purchase_order", "quotation", "created_by", "approved_by"
        )
        .prefetch_related("payments")
        .all()
    )
    serializer_class = InvoiceSerializer
    permission_classes = [IsAuthenticated, CapabilityGate]
    filterset_fields = ["invoice_type", "status", "client", "supplier", "quotation", "purchase_order"]
    search_fields = ["invoice_number", "reference"]
    ordering_fields = ["issue_date", "due_date", "total_amount"]

    def perform_create(self, serializer):
        extra = {}
        # A supplier's bill is a fact once it arrives: nothing to approve.
        if serializer.validated_data.get("invoice_type") == Invoice.InvoiceType.PAYABLE:
            extra["status"] = Invoice.Status.APPROVED
        serializer.save(created_by=self.request.user, **extra)

    def perform_destroy(self, instance):
        if instance.status != Invoice.Status.DRAFT and instance.invoice_type != Invoice.InvoiceType.PAYABLE:
            raise serializers.ValidationError({"detail": "Only a draft invoice is deleted; cancel an approved one instead."})
        instance.delete()

    @action(detail=True, methods=["post"])
    def approve(self, request, pk=None):
        """Sign a draft off: from here it is money owed to the company."""
        invoice = self.get_object()
        if invoice.status != Invoice.Status.DRAFT:
            raise serializers.ValidationError({"detail": "Only a draft invoice is approved."})
        invoice.status = Invoice.Status.APPROVED
        invoice.approved_by = request.user
        invoice.approved_at = timezone.now()
        invoice.save(update_fields=["status", "approved_by", "approved_at", "updated_at"])
        return Response(self.get_serializer(invoice).data)

    @action(detail=True, methods=["get"], url_path="print")
    def print_pdf(self, request, pk=None):
        from django.http import HttpResponse

        from .pdf import build_invoice_pdf

        invoice = self.get_object()
        response = HttpResponse(build_invoice_pdf(invoice), content_type="application/pdf")
        response["Content-Disposition"] = f'inline; filename="{invoice.invoice_number}.pdf"'
        return response

    @action(detail=True, methods=["post"])
    def cancel(self, request, pk=None):
        invoice = self.get_object()
        if invoice.status == Invoice.Status.CANCELLED:
            raise serializers.ValidationError({"detail": "This invoice is already cancelled."})
        if invoice.payments.exists():
            raise serializers.ValidationError({"detail": "Money has been received against it; remove those payments first."})
        invoice.status = Invoice.Status.CANCELLED
        invoice.save(update_fields=["status", "updated_at"])
        return Response(self.get_serializer(invoice).data)


class PaymentViewSet(viewsets.ModelViewSet):
    read_capability = "view_finance"
    write_capability = "manage_finance"
    queryset = Payment.objects.select_related("invoice", "purchase_order", "recorded_by").all()
    serializer_class = PaymentSerializer
    permission_classes = [IsAuthenticated, CapabilityGate]
    filterset_fields = ["invoice", "purchase_order", "method", "kind"]
    ordering_fields = ["payment_date"]

    @staticmethod
    def _sync(payment):
        if payment.invoice_id:
            ledger.sync_invoice(payment.invoice)

    @transaction.atomic
    def perform_create(self, serializer):
        self._sync(serializer.save(recorded_by=self.request.user))

    @transaction.atomic
    def perform_update(self, serializer):
        before = serializer.instance.invoice
        payment = serializer.save()
        self._sync(payment)
        if before is not None and before.pk != payment.invoice_id:
            ledger.sync_invoice(before)

    @transaction.atomic
    def perform_destroy(self, instance):
        invoice = instance.invoice
        instance.delete()
        if invoice:
            ledger.sync_invoice(invoice)


def _uuid(value):
    import uuid

    try:
        return uuid.UUID(str(value))
    except (TypeError, ValueError):
        return None


def _describe(params) -> str:
    """The filters behind an export, in words, for the top of the file."""
    from apps.clients.models import Client
    from apps.suppliers.models import Supplier
    from apps.teams.models import Project

    parts = []
    if params.get("date_from") or params.get("date_to"):
        parts.append(f"Dated {params.get('date_from') or 'the start'} to {params.get('date_to') or 'today'}")
    if params.get("party"):
        pid = _uuid(params["party"])
        name = pid and (Supplier.objects.filter(pk=pid).values_list("name", flat=True).first()
                or Client.objects.filter(pk=pid).values_list("name", flat=True).first())
        parts.append(name or "One party")
    if params.get("project"):
        pid = _uuid(params["project"])
        parts.append("Project: " + ((pid and Project.objects.filter(pk=pid).values_list("name", flat=True).first()) or "none"))
    if params.get("status"):
        parts.append(params["status"].replace("_", " ").capitalize())
    if params.get("q"):
        parts.append(f"Search: {params['q']}")
    if params.get("ids"):
        parts.append(f"{len([i for i in params['ids'].split(',') if i])} selected rows")
    return f"As of {timezone.localdate():%d %b %Y} · " + (" · ".join(parts) or "All records")


def _plain(totals):
    return {
        cur: {**t, "aging": [{"key": k, "label": label, "amount": t["aging"][k]} for k, label in ledger.AGING]}
        for cur, t in totals.items()
    }


class FinancialViewSet(viewsets.ViewSet):
    """The Financial module: the dashboard and its Payable, Receivable and
    Inventory tabs. For higher management, so reading it is view_finance."""

    permission_classes = [IsAuthenticated, CapabilityGate]
    read_capability = "view_finance"
    write_capability = "manage_finance"
    action_capabilities = {"hold": ("manage_finance", "manage_stock")}

    def overview(self, request):
        payable = ledger.totals(ledger.payables())
        receivable = ledger.totals(ledger.receivables())
        stock = ledger.inventory()
        currencies = sorted(set(payable) | set(receivable) | {"PKR"}, key=lambda c: (c != "PKR", c))
        zero = ledger.ZERO
        net = {
            c: (receivable.get(c, {}).get("balance", zero)) - (payable.get(c, {}).get("balance", zero))
            for c in currencies
        }
        return Response({
            "as_of": timezone.localdate(),
            "currencies": currencies,
            "payable": _plain(payable),
            "receivable": _plain(receivable),
            "net_worth": net,
            "inventory": {
                "total": stock["total"],
                "groups": [{k: g[k] for k in ("key", "label", "total", "unpriced")} | {"count": len(g["lines"])} for g in stock["groups"]],
            },
        })

    def _listing(self, request, key, title, rows, columns, extra=None):
        """A Payable or Receivable list as the filters narrow it, or the same
        rows as an Excel, PDF or CSV file."""
        from . import exports

        rows = ledger.filter_rows(rows, request.query_params)
        fmt = request.query_params.get("export")
        if fmt in exports.FORMATS:
            return exports.respond(request, fmt, key, title, columns, exports.flat(rows), _describe(request.query_params))
        return Response({"rows": rows, "totals": _plain(ledger.totals(rows))} | (extra or {}))

    def payables(self, request):
        from .exports import PAYABLE_COLUMNS

        return self._listing(request, "payables", "Payables", ledger.payables(), PAYABLE_COLUMNS)

    def receivables(self, request):
        from .exports import RECEIVABLE_COLUMNS

        return self._listing(request, "receivables", "Receivables", ledger.receivables(), RECEIVABLE_COLUMNS,
                             {"uninvoiced": ledger.uninvoiced()})

    def inventory(self, request):
        from . import exports

        data = ledger.inventory()
        fmt = request.query_params.get("export")
        if fmt in exports.FORMATS:
            return exports.respond(request, fmt, "inventory-in-hand", "Inventory in hand", exports.INVENTORY_COLUMNS,
                                   exports.inventory_rows(data), f"As of {timezone.localdate():%d %b %Y} · at cost")
        return Response(data)

    def due_date(self, request, pk=None):
        """Set (or clear) when a supplier is to be paid on an order."""
        order = ledger.payable_orders().filter(pk=pk).first()
        if order is None:
            return Response({"detail": "Not an approved purchase order."}, status=status.HTTP_404_NOT_FOUND)
        raw = request.data.get("payment_due_date")
        due = parse_date(raw) if raw else None
        if raw and due is None:
            raise serializers.ValidationError({"payment_due_date": "Enter a date."})
        ordered = order.order_date or timezone.localtime(order.created_at).date()
        if due and due < ordered:
            raise serializers.ValidationError({"payment_due_date": f"The due date cannot be before the order date ({ordered:%d %b %Y})."})
        order.payment_due_date = due
        order.save(update_fields=["payment_due_date", "updated_at"])
        return Response({"payment_due_date": due})

    def client_po(self, request, pk=None):
        """Record the purchase order the client issued against an accepted
        quotation: its number, amount and date."""
        from decimal import Decimal, InvalidOperation

        from apps.quotations.models import Quotation

        q = Quotation.objects.filter(pk=pk, status=Quotation.Status.ACCEPTED).first()
        if q is None:
            return Response({"detail": "Not an accepted quotation."}, status=status.HTTP_404_NOT_FOUND)
        number = (request.data.get("client_po_number") or "").strip()
        raw_amount, raw_date = request.data.get("client_po_amount"), request.data.get("client_po_date")
        try:
            amount = Decimal(str(raw_amount)) if raw_amount not in (None, "") else None
        except InvalidOperation:
            raise serializers.ValidationError({"client_po_amount": "Enter an amount."})
        if amount is not None and amount <= 0:
            raise serializers.ValidationError({"client_po_amount": "Enter an amount above zero."})
        when = parse_date(raw_date) if raw_date else None
        if raw_date and when is None:
            raise serializers.ValidationError({"client_po_date": "Enter a date."})
        if amount is not None and not number:
            raise serializers.ValidationError({"client_po_number": "Enter the client's PO number."})
        q.client_po_number, q.client_po_amount, q.client_po_date = number, amount, when
        q.save(update_fields=["client_po_number", "client_po_amount", "client_po_date", "updated_at"])
        return Response(ledger.quotation_billing(q))

    def hold(self, request):
        """Mark an asset in stock as a finished product, office display or
        FOC sample."""
        from apps.assets.models import Device

        held_as = request.data.get("held_as")
        if held_as not in Device.HeldAs.values:
            raise serializers.ValidationError({"held_as": "Choose finished product, rented, sold, office display or FOC sample."})
        pk = _uuid(request.data.get("device"))
        device = pk and Device.objects.filter(pk=pk, status=Device.Status.IN_STOCK).first()
        if device is None:
            return Response({"detail": "Only an asset in stock is held."}, status=status.HTTP_404_NOT_FOUND)
        device.held_as = held_as
        device.save(update_fields=["held_as", "updated_at"])
        return Response({"device": str(device.id), "held_as": held_as})
