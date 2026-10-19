"""Clear the test activity before the system is handed to the client.

Removes every transactional record - assets, projects, purchase and work
orders, deliveries, installations, tickets, maintenance, material requests,
warranties, invoices, quotations, documents, notifications, attendance and
the logs of all that - and zeroes the stock on hand. Keeps the people and
their roles, clients, sites, vendors, the stock catalogue with its prices,
the templates and the settings. Numbering restarts at 1 for what was cleared.

    manage.py clear_test_data            # dry run: what would go
    manage.py clear_test_data --yes      # back up, then clear
    manage.py clear_test_data --yes --once   # ... unless it already ran

A full JSON backup of the database is written to MEDIA_ROOT/backups first;
restore with ``manage.py loaddata <file>`` on an empty database.
"""
from __future__ import annotations

import gzip
import os

from django.apps import apps
from django.conf import settings
from django.core.management import call_command
from django.core.management.base import BaseCommand
from django.db import transaction
from django.utils import timezone

# Children before parents, so nothing protected is left pointing at a parent.
CLEAR = [
    "notifications.Notification", "notifications.StickyNoteReply", "notifications.StickyNote",
    "notifications.CalendarEvent", "notifications.WebhookLog",
    "attendance.AttendanceRecord", "attendance.DutyRecord",
    "chat.ChatMessage",
    "analytics.Alert",
    "finance.Payment", "finance.Invoice",
    "quotations.QuotationItem", "quotations.Quotation",
    "warranties.WarrantyClaim", "warranties.Warranty",
    "tickets.TicketComment", "tickets.TicketAttachment", "tickets.Ticket",
    "maintenance.MaintenanceActivity", "maintenance.MaintenancePurchase", "maintenance.MaintenancePartRequest",
    "maintenance.MaintenanceRecordPhoto", "maintenance.MaintenanceCostLine", "maintenance.MaintenanceRecord",
    "maintenance.MaintenanceVisitPhoto", "maintenance.MaintenanceVisit", "maintenance.MaintenanceSchedule",
    "sites.InstallationActivity", "sites.InstallationPhoto", "sites.InstallationDelay", "sites.HandoverRecord",
    "sites.InstallationStep", "sites.DeviceInstallation",
    "workorders.WorkOrderItem", "workorders.WorkOrder",
    "inventory.StockMovement", "inventory.Issuance", "inventory.IssuanceRequest", "inventory.ReorderRequest",
    "inventory.GoodsReceiptLine", "inventory.GoodsReceipt", "inventory.InventoryUnit",
    "procurement.PurchaseOrderItem", "procurement.PurchaseOrder",
    "teams.ProjectActivity", "teams.BOMAllocation", "teams.ProjectBOMLine", "teams.ProjectCostLine",
    "teams.ProjectBudget", "teams.ProjectMember", "teams.ProjectBottleneck", "teams.ProjectMilestone",
    "teams.ProjectScopeItem", "teams.Project",
    "assets.DeviceLifecycleEvent", "assets.DeviceImage", "assets.ProductionStep", "assets.AssetComponent",
    "assets.AssetCode", "assets.Device",
    "infrastructure.Document",
    "accounts.AuditLog", "accounts.RequestReceipt",
]

# Their records are gone, so their numbers start again; clients, vendors and
# catalogue items stay, and so do their counters.
RESTART_NUMBERING = [
    "asset", "client_warranty", "component_warranty", "goods_receipt", "inventory_unit", "invoice",
    "issuance", "material_request", "project", "purchase_order", "purchase_requisition", "quotation",
    "ticket", "vendor_warranty", "warranty_claim", "work_order",
]

ONCE_KEY = "test_data_cleared"


class Command(BaseCommand):
    help = "Clear test activity before handover (dry run unless --yes)."

    def add_arguments(self, parser):
        parser.add_argument("--yes", action="store_true", help="Really clear (after a backup).")
        parser.add_argument("--once", action="store_true", help="Do nothing if it has already run here.")

    def handle(self, *args, yes=False, once=False, **options):
        AppSetting = apps.get_model("accounts", "AppSetting")
        done = AppSetting.objects.filter(key=ONCE_KEY).first()
        if once and done is not None:
            self.stdout.write(f"Test data was already cleared on {done.value.get('at', '?')}; nothing to do.")
            return

        counts = [(label, apps.get_model(label).objects.count()) for label in CLEAR]
        items = apps.get_model("inventory", "InventoryItem").objects.exclude(quantity=0).count()
        for label, n in counts:
            if n:
                self.stdout.write(f"  {label}: {n}")
        self.stdout.write(f"  stock items to zero: {items}")
        self.stdout.write(f"Total records to remove: {sum(n for _, n in counts)}")
        if not yes:
            self.stdout.write("Dry run - nothing removed. Run again with --yes to clear.")
            return

        backup = self._backup()
        self.stdout.write(f"Backup written: {backup}")

        with transaction.atomic():
            for label in CLEAR:
                apps.get_model(label).objects.all().delete()
            apps.get_model("inventory", "InventoryItem").objects.update(quantity=0)
            apps.get_model("setup", "NumberingScheme").objects.filter(entity__in=RESTART_NUMBERING).update(next_number=1)
            AppSetting.objects.update_or_create(
                key=ONCE_KEY, defaults={"value": {"at": timezone.now().isoformat(), "backup": backup}},
            )
        self.stdout.write(self.style.SUCCESS("Test data cleared."))

    def _backup(self) -> str:
        folder = os.path.join(settings.MEDIA_ROOT, "backups")
        os.makedirs(folder, exist_ok=True)
        path = os.path.join(folder, f"before-clear-{timezone.now():%Y%m%d-%H%M%S}.json.gz")
        # UTF-8 whatever the server's locale: the data has arrows, dashes, Urdu names.
        with gzip.open(path, "wt", encoding="utf-8") as out:
            call_command(
                "dumpdata", exclude=["contenttypes", "auth.permission", "sessions", "admin.logentry"],
                stdout=out, verbosity=0,
            )
        return path
