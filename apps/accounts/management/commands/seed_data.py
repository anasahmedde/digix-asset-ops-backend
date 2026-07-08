"""
Management command to seed the database with Pakistan-specific test data.

Usage:
    python manage.py seed_data          # Create all data
    python manage.py seed_data --flush  # Clear existing data first
"""

import random
from datetime import date, timedelta
from decimal import Decimal

from django.contrib.auth import get_user_model
from django.core.management.base import BaseCommand
from django.utils import timezone

from apps.assets.models import (
    AssetCode,
    Brand,
    Device,
    DeviceLifecycleEvent,
    DeviceModel,
    MaterialType,
)
from apps.clients.models import Client
from apps.finance.models import Invoice, Payment
from apps.inventory.models import InventoryItem, StockMovement
from apps.maintenance.models import MaintenanceRecord, MaintenanceSchedule
from apps.procurement.models import PurchaseOrder, PurchaseOrderItem
from apps.sites.models import DeviceInstallation, InstallationStep, Site, SiteZone
from apps.suppliers.models import Supplier
from apps.tickets.models import Ticket
from apps.warranties.models import Warranty

User = get_user_model()

PASSWORD = "Test@1234"


class Command(BaseCommand):
    help = "Seed database with Pakistan-specific test data"

    def add_arguments(self, parser):
        parser.add_argument(
            "--flush",
            action="store_true",
            help="Delete existing seeded data before creating new data",
        )

    def handle(self, *args, **options):
        if options["flush"]:
            self.stdout.write("Flushing existing data...")
            self._flush()

        self.stdout.write("Seeding database with Pakistan data...")

        users = self._create_users()
        clients = self._create_clients()
        suppliers = self._create_suppliers()
        brands = self._create_brands()
        device_models = self._create_device_models(brands)
        material_types = self._create_material_types()
        sites = self._create_sites(clients)
        zones = self._create_zones(sites)
        devices = self._create_devices(device_models, suppliers, sites, clients, users)
        installations = self._create_installations(devices, sites, zones, users)
        self._create_lifecycle_events(devices, users)

        tickets = self._create_tickets(devices, sites, users)
        warranties = self._create_warranties(devices, suppliers)
        schedules, records = self._create_maintenance(devices, sites, users)
        inv_items, movements = self._create_inventory(material_types, sites, users)
        pos = self._create_procurement(suppliers, users)
        invoices, payments = self._create_finance(clients, suppliers, pos, users)
        self._create_alerts(devices, sites)

        self.stdout.write("")
        self.stdout.write(self.style.SUCCESS("=" * 60))
        self.stdout.write(self.style.SUCCESS("  SEED DATA CREATED SUCCESSFULLY"))
        self.stdout.write(self.style.SUCCESS("=" * 60))
        self.stdout.write("")
        self.stdout.write(self.style.WARNING(f"  Default password for all users: {PASSWORD}"))
        self.stdout.write("")
        self.stdout.write("  Test Accounts:")
        self.stdout.write(f"    Super Admin:   admin        / {PASSWORD}")
        self.stdout.write(f"    Ops Manager:   ops_manager  / {PASSWORD}")
        self.stdout.write(f"    Technician 1:  tech1        / {PASSWORD}")
        self.stdout.write(f"    Technician 2:  tech2        / {PASSWORD}")
        self.stdout.write(f"    Finance:       finance_user / {PASSWORD}")
        self.stdout.write(f"    Warehouse:     warehouse    / {PASSWORD}")
        self.stdout.write(f"    Client Viewer: viewer       / {PASSWORD}")
        self.stdout.write("")
        self.stdout.write(f"  Created: {len(users)} users, {len(clients)} clients,")
        self.stdout.write(f"           {len(suppliers)} suppliers, {len(brands)} brands,")
        self.stdout.write(f"           {len(device_models)} device models, {len(sites)} sites,")
        self.stdout.write(f"           {len(zones)} zones, {len(devices)} devices,")
        self.stdout.write(f"           {len(installations)} installations,")
        self.stdout.write(f"           {len(tickets)} tickets, {len(warranties)} warranties,")
        self.stdout.write(f"           {len(schedules)} maintenance schedules,")
        self.stdout.write(f"           {len(records)} maintenance records,")
        self.stdout.write(f"           {len(inv_items)} inventory items,")
        self.stdout.write(f"           {len(movements)} stock movements,")
        self.stdout.write(f"           {len(pos)} purchase orders,")
        self.stdout.write(f"           {len(invoices)} invoices, {len(payments)} payments")
        self.stdout.write(self.style.SUCCESS("=" * 60))

    def _flush(self):
        Payment.objects.all().delete()
        Invoice.objects.all().delete()
        PurchaseOrderItem.objects.all().delete()
        PurchaseOrder.objects.all().delete()
        StockMovement.objects.all().delete()
        InventoryItem.objects.all().delete()
        MaintenanceRecord.objects.all().delete()
        MaintenanceSchedule.objects.all().delete()
        Warranty.objects.all().delete()
        Ticket.objects.all().delete()
        DeviceLifecycleEvent.objects.all().delete()
        AssetCode.objects.all().delete()
        InstallationStep.objects.all().delete()
        DeviceInstallation.objects.all().delete()
        Device.objects.all().delete()
        DeviceModel.objects.all().delete()
        Brand.objects.all().delete()
        MaterialType.objects.all().delete()
        SiteZone.objects.all().delete()
        Site.objects.all().delete()
        Client.objects.all().delete()
        Supplier.objects.all().delete()
        User.objects.filter(is_superuser=False).delete()
        User.objects.filter(username="admin").delete()
        try:
            from apps.analytics.models import Alert
            Alert.objects.all().delete()
        except Exception:
            pass
        self.stdout.write(self.style.WARNING("  Existing data flushed."))

    # ------------------------------------------------------------------
    # Users
    # ------------------------------------------------------------------
    def _create_users(self):
        users = []
        user_data = [
            {
                "username": "admin",
                "email": "admin@digix.pk",
                "first_name": "System",
                "last_name": "Administrator",
                "role": "super_admin",
                "is_superuser": True,
                "is_staff": True,
            },
            {
                "username": "ops_manager",
                "email": "ops@digix.pk",
                "first_name": "Usman",
                "last_name": "Tariq",
                "role": "ops_manager",
                "phone": "+923001234567",
            },
            {
                "username": "tech1",
                "email": "tech1@digix.pk",
                "first_name": "Ahmed",
                "last_name": "Khan",
                "role": "technician",
                "is_field_staff": True,
                "phone": "+923012345678",
            },
            {
                "username": "tech2",
                "email": "tech2@digix.pk",
                "first_name": "Bilal",
                "last_name": "Hussain",
                "role": "technician",
                "is_field_staff": True,
                "phone": "+923023456789",
            },
            {
                "username": "finance_user",
                "email": "finance@digix.pk",
                "first_name": "Ayesha",
                "last_name": "Malik",
                "role": "finance",
                "phone": "+923034567890",
            },
            {
                "username": "warehouse",
                "email": "warehouse@digix.pk",
                "first_name": "Hassan",
                "last_name": "Ali",
                "role": "warehouse",
                "phone": "+923045678901",
            },
            {
                "username": "viewer",
                "email": "viewer@digix.pk",
                "first_name": "Fatima",
                "last_name": "Noor",
                "role": "client_viewer",
            },
        ]

        for data in user_data:
            is_superuser = data.pop("is_superuser", False)
            is_staff = data.pop("is_staff", False)
            is_active = data.pop("is_active", True)

            user, created = User.objects.get_or_create(
                username=data["username"],
                defaults={
                    **data,
                    "is_superuser": is_superuser,
                    "is_staff": is_staff,
                    "is_active": is_active,
                },
            )
            if created:
                user.set_password(PASSWORD)
                user.save()
                self.stdout.write(f"  Created user: {user.username} ({user.role})")
            else:
                self.stdout.write(f"  User exists:  {user.username}")
            users.append(user)

        return users

    # ------------------------------------------------------------------
    # Clients — Pakistani companies
    # ------------------------------------------------------------------
    def _create_clients(self):
        clients = []
        client_data = [
            {
                "name": "Packages Mall Lahore",
                "code": "CLT-001",
                "contact_person": "Imran Siddiqui",
                "contact_email": "ops@packagesmall.pk",
                "contact_phone": "+924235100100",
                "address": "Shahrah-e-Quaid-e-Azam, Lahore, Pakistan",
            },
            {
                "name": "Dolmen Group",
                "code": "CLT-002",
                "contact_person": "Nadeem Riaz",
                "contact_email": "info@dolmengroup.com",
                "contact_phone": "+922135800800",
                "address": "Tariq Road, Karachi, Pakistan",
            },
            {
                "name": "Centaurus Mall Islamabad",
                "code": "CLT-003",
                "contact_person": "Tariq Mehmood",
                "contact_email": "admin@centaurus.com.pk",
                "contact_phone": "+925126100100",
                "address": "Jinnah Avenue, Islamabad, Pakistan",
            },
            {
                "name": "Nishat Group",
                "code": "CLT-004",
                "contact_person": "Amir Bashir",
                "contact_email": "corporate@nishat.net",
                "contact_phone": "+924237100100",
                "address": "53-A, Lawrence Road, Lahore, Pakistan",
            },
            {
                "name": "Lucky One Mall",
                "code": "CLT-005",
                "contact_person": "Zeeshan Ahmed",
                "contact_email": "ops@luckyone.pk",
                "contact_phone": "+922138900900",
                "address": "Main University Road, Karachi, Pakistan",
            },
        ]

        for data in client_data:
            client, created = Client.objects.get_or_create(
                code=data["code"], defaults=data
            )
            action = "Created" if created else "Exists"
            self.stdout.write(f"  {action} client: {client.name}")
            clients.append(client)

        return clients

    # ------------------------------------------------------------------
    # Suppliers
    # ------------------------------------------------------------------
    def _create_suppliers(self):
        suppliers = []
        supplier_data = [
            {
                "name": "Samsung Pakistan",
                "code": "SUP-001",
                "contact_person": "Farhan Iqbal",
                "contact_email": "b2b@samsung.pk",
                "contact_phone": "+922135678901",
                "address": "I.I. Chundrigar Road, Karachi, Pakistan",
                "website": "https://samsung.com/pk",
            },
            {
                "name": "LG Electronics Pakistan",
                "code": "SUP-002",
                "contact_person": "Kamran Shah",
                "contact_email": "enterprise@lg.pk",
                "contact_phone": "+924237654321",
                "address": "Gulberg III, Lahore, Pakistan",
                "website": "https://lg.com/pk",
            },
            {
                "name": "Greenstar LED Solutions",
                "code": "SUP-003",
                "contact_person": "Waqas Rana",
                "contact_email": "sales@greenstarled.pk",
                "contact_phone": "+925127654321",
                "address": "Blue Area, Islamabad, Pakistan",
                "website": "https://greenstarled.pk",
            },
            {
                "name": "Orient Electronics",
                "code": "SUP-004",
                "contact_person": "Naeem Bhatti",
                "contact_email": "orders@orient.pk",
                "contact_phone": "+924238765432",
                "address": "GT Road, Gujranwala, Pakistan",
                "website": "https://orient.com.pk",
            },
        ]

        for data in supplier_data:
            supplier, created = Supplier.objects.get_or_create(
                code=data["code"], defaults=data
            )
            action = "Created" if created else "Exists"
            self.stdout.write(f"  {action} supplier: {supplier.name}")
            suppliers.append(supplier)

        return suppliers

    # ------------------------------------------------------------------
    # Brands & Device Models
    # ------------------------------------------------------------------
    def _create_brands(self):
        brands = []
        brand_names = [
            ("Samsung", "https://samsung.com"),
            ("LG", "https://lg.com"),
            ("NEC", "https://nec-display.com"),
            ("BrightSign", "https://brightsign.biz"),
            ("Sony", "https://sony.com"),
        ]
        for name, website in brand_names:
            brand, created = Brand.objects.get_or_create(
                name=name, defaults={"website": website}
            )
            action = "Created" if created else "Exists"
            self.stdout.write(f"  {action} brand: {brand.name}")
            brands.append(brand)
        return brands

    def _create_device_models(self, brands):
        models_list = []
        model_data = [
            (brands[0], "QM55R", "LH55QMREBGCXZA", "LED", '55"'),
            (brands[0], "QM75R", "LH75QMREBGCXZA", "LED", '75"'),
            (brands[0], "QB43R", "LH43QBREBGCXZA", "LED", '43"'),
            (brands[1], "55UH5F", "55UH5F-H", "IPS", '55"'),
            (brands[1], "86UH5F", "86UH5F-H", "IPS", '86"'),
            (brands[2], "V554Q", "V554Q", "LED", '55"'),
            (brands[3], "XT1144", "XT1144", "N/A", "N/A"),
            (brands[3], "XD234", "XD234", "N/A", "N/A"),
            (brands[4], "FW-55BZ40H", "FW-55BZ40H", "LED", '55"'),
        ]
        for brand, name, model_number, screen_type, screen_size in model_data:
            dm, created = DeviceModel.objects.get_or_create(
                brand=brand,
                name=name,
                defaults={
                    "model_number": model_number,
                    "screen_type": screen_type,
                    "screen_size": screen_size,
                },
            )
            action = "Created" if created else "Exists"
            self.stdout.write(f"  {action} device model: {brand.name} {name}")
            models_list.append(dm)
        return models_list

    # ------------------------------------------------------------------
    # Material Types
    # ------------------------------------------------------------------
    def _create_material_types(self):
        materials_list = []
        materials = [
            ("HDMI Cable 2m", "Cables", "piece"),
            ("HDMI Cable 5m", "Cables", "piece"),
            ("Power Cable 3-pin", "Cables", "piece"),
            ("Wall Mount Bracket", "Mounts", "piece"),
            ("Ceiling Mount Kit", "Mounts", "piece"),
            ("Cat6 Cable", "Cables", "meter"),
            ("Media Player Stand", "Accessories", "piece"),
            ('Screen Protector 55"', "Accessories", "piece"),
        ]
        for name, category, unit in materials:
            mt, created = MaterialType.objects.get_or_create(
                name=name, defaults={"category": category, "unit": unit}
            )
            if created:
                self.stdout.write(f"  Created material: {name}")
            materials_list.append(mt)
        return materials_list

    # ------------------------------------------------------------------
    # Sites — real Pakistani cities with accurate coordinates
    # ------------------------------------------------------------------
    def _create_sites(self, clients):
        sites = []
        site_data = [
            {
                "name": "Packages Mall - Main Entrance",
                "address": "Shahrah-e-Quaid-e-Azam, Gulberg III",
                "city": "Lahore",
                "state_province": "Punjab",
                "country": "Pakistan",
                "latitude": Decimal("31.5204000"),
                "longitude": Decimal("74.3587000"),
                "contact_person": "Imran Siddiqui",
                "contact_phone": "+924235100100",
                "operating_hours": "10:00-22:00",
                "client": clients[0],
            },
            {
                "name": "Dolmen Mall Clifton",
                "address": "Block 4, Clifton",
                "city": "Karachi",
                "state_province": "Sindh",
                "country": "Pakistan",
                "latitude": Decimal("24.8138000"),
                "longitude": Decimal("67.0280000"),
                "contact_person": "Nadeem Riaz",
                "contact_phone": "+922135800800",
                "operating_hours": "10:00-23:00",
                "client": clients[1],
            },
            {
                "name": "Centaurus Mall - Ground Floor",
                "address": "Jinnah Avenue, F-8",
                "city": "Islamabad",
                "state_province": "Islamabad Capital Territory",
                "country": "Pakistan",
                "latitude": Decimal("33.7080000"),
                "longitude": Decimal("73.0479000"),
                "contact_person": "Tariq Mehmood",
                "contact_phone": "+925126100100",
                "operating_hours": "10:00-22:00",
                "client": clients[2],
            },
            {
                "name": "Nishat Emporium Mall",
                "address": "Abdul Haque Road, Johar Town",
                "city": "Lahore",
                "state_province": "Punjab",
                "country": "Pakistan",
                "latitude": Decimal("31.4697000"),
                "longitude": Decimal("74.2728000"),
                "contact_person": "Amir Bashir",
                "contact_phone": "+924237100100",
                "operating_hours": "10:00-22:00",
                "client": clients[3],
            },
            {
                "name": "Lucky One Mall - Atrium",
                "address": "Main University Road",
                "city": "Karachi",
                "state_province": "Sindh",
                "country": "Pakistan",
                "latitude": Decimal("24.9180000"),
                "longitude": Decimal("67.0903000"),
                "contact_person": "Zeeshan Ahmed",
                "contact_phone": "+922138900900",
                "operating_hours": "10:00-23:00",
                "client": clients[4],
            },
            {
                "name": "Giga Mall - DHA Phase II",
                "address": "GT Road, DHA Phase II",
                "city": "Islamabad",
                "state_province": "Islamabad Capital Territory",
                "country": "Pakistan",
                "latitude": Decimal("33.5331000"),
                "longitude": Decimal("73.1260000"),
                "contact_person": "Shahid Khan",
                "contact_phone": "+925127654321",
                "operating_hours": "10:00-22:00",
                "client": clients[2],
            },
            {
                "name": "Faisalabad Trade Center",
                "address": "Satiana Road",
                "city": "Faisalabad",
                "state_province": "Punjab",
                "country": "Pakistan",
                "latitude": Decimal("31.4187000"),
                "longitude": Decimal("73.0791000"),
                "contact_person": "Naveed Aslam",
                "contact_phone": "+924138001001",
                "operating_hours": "09:00-21:00",
                "client": clients[3],
            },
            {
                "name": "Multan Digital Hub",
                "address": "Bosan Road",
                "city": "Multan",
                "state_province": "Punjab",
                "country": "Pakistan",
                "latitude": Decimal("30.1984000"),
                "longitude": Decimal("71.4687000"),
                "contact_person": "Kashif Mumtaz",
                "contact_phone": "+926161001001",
                "operating_hours": "09:00-21:00",
                "client": clients[0],
            },
            {
                "name": "Peshawar Mall of Hayatabad",
                "address": "Phase 5, Hayatabad",
                "city": "Peshawar",
                "state_province": "KPK",
                "country": "Pakistan",
                "latitude": Decimal("34.0151000"),
                "longitude": Decimal("71.5249000"),
                "contact_person": "Junaid Khan",
                "contact_phone": "+929191001001",
                "operating_hours": "10:00-22:00",
                "client": clients[1],
            },
            {
                "name": "Quetta Serena Hotel Lobby",
                "address": "Shahrah-e-Zarghoon",
                "city": "Quetta",
                "state_province": "Balochistan",
                "country": "Pakistan",
                "latitude": Decimal("30.1798000"),
                "longitude": Decimal("66.9750000"),
                "contact_person": "Abdul Qadir",
                "contact_phone": "+928181001001",
                "operating_hours": "24/7",
                "client": clients[2],
            },
            {
                "name": "Rawalpindi Commercial Market",
                "address": "Bank Road, Saddar",
                "city": "Rawalpindi",
                "state_province": "Punjab",
                "country": "Pakistan",
                "latitude": Decimal("33.5651000"),
                "longitude": Decimal("73.0169000"),
                "contact_person": "Adnan Shah",
                "contact_phone": "+925151001001",
                "operating_hours": "09:00-21:00",
                "client": clients[3],
            },
            {
                "name": "Hyderabad Tower Mall",
                "address": "Autobahn Road",
                "city": "Hyderabad",
                "state_province": "Sindh",
                "country": "Pakistan",
                "latitude": Decimal("25.3960000"),
                "longitude": Decimal("68.3578000"),
                "contact_person": "Sajid Memon",
                "contact_phone": "+922229001001",
                "operating_hours": "10:00-22:00",
                "client": clients[4],
            },
            {
                "name": "Sialkot Business Center",
                "address": "Paris Road",
                "city": "Sialkot",
                "state_province": "Punjab",
                "country": "Pakistan",
                "latitude": Decimal("32.4945000"),
                "longitude": Decimal("74.5229000"),
                "contact_person": "Rizwan Cheema",
                "contact_phone": "+925241001001",
                "operating_hours": "09:00-20:00",
                "client": clients[0],
            },
            {
                "name": "Gujranwala Mega Center",
                "address": "GT Road",
                "city": "Gujranwala",
                "state_province": "Punjab",
                "country": "Pakistan",
                "latitude": Decimal("32.1877000"),
                "longitude": Decimal("74.1945000"),
                "contact_person": "Waseem Akram",
                "contact_phone": "+925551001001",
                "operating_hours": "09:00-21:00",
                "client": clients[1],
            },
            {
                "name": "Abbottabad Pine Mall",
                "address": "Mansehra Road",
                "city": "Abbottabad",
                "state_province": "KPK",
                "country": "Pakistan",
                "latitude": Decimal("34.1688000"),
                "longitude": Decimal("73.2215000"),
                "contact_person": "Sajjad Shah",
                "contact_phone": "+929921001001",
                "operating_hours": "10:00-21:00",
                "client": clients[2],
            },
        ]

        for data in site_data:
            site, created = Site.objects.get_or_create(
                name=data["name"], defaults=data
            )
            action = "Created" if created else "Exists"
            self.stdout.write(f"  {action} site: {site.name} ({site.city})")
            sites.append(site)

        return sites

    # ------------------------------------------------------------------
    # Zones
    # ------------------------------------------------------------------
    def _create_zones(self, sites):
        zones = []
        zone_templates = [
            [("Entrance A", "Main entrance area", "Ground"),
             ("Food Court", "Food court section", "2nd Floor"),
             ("Atrium", "Central atrium", "Ground")],
            [("Main Hall", "Primary display area", "Ground"),
             ("Escalator Landing", "Near escalators", "1st Floor")],
            [("Reception", "Main reception", "Ground"),
             ("Parking Entrance", "Multi-storey parking entry", "Basement")],
        ]

        for i, site in enumerate(sites):
            template = zone_templates[i % len(zone_templates)]
            for name, description, floor in template:
                zone, created = SiteZone.objects.get_or_create(
                    site=site,
                    name=name,
                    defaults={"description": description, "floor": floor},
                )
                if created:
                    self.stdout.write(f"  Created zone: {site.name} > {name}")
                zones.append(zone)

        return zones

    # ------------------------------------------------------------------
    # Devices — spread across Pakistani sites
    # ------------------------------------------------------------------
    def _create_devices(self, device_models, suppliers, sites, clients, users):
        devices = []
        technicians = [u for u in users if u.role == "technician"]

        status_weights = [
            Device.Status.ACTIVE,
            Device.Status.ACTIVE,
            Device.Status.ACTIVE,
            Device.Status.ACTIVE,
            Device.Status.INSTALLED,
            Device.Status.INSTALLED,
            Device.Status.IN_STOCK,
            Device.Status.UNDER_MAINTENANCE,
            Device.Status.PROCURED,
            Device.Status.ASSIGNED,
            Device.Status.DECOMMISSIONED,
            Device.Status.IN_TRANSIT,
            Device.Status.RMA,
        ]

        active_sites = [s for s in sites if s.is_active]

        for i in range(45):
            serial = f"SN-2026-PK-{i+1:05d}"
            dm = device_models[i % len(device_models)]
            sup = suppliers[i % len(suppliers)]
            stat = status_weights[i % len(status_weights)]

            site = active_sites[i % len(active_sites)] if stat in (
                Device.Status.ACTIVE, Device.Status.INSTALLED
            ) else None

            client = site.client if site else (
                clients[i % len(clients)] if stat == Device.Status.ASSIGNED else None
            )

            tech = (
                technicians[i % len(technicians)]
                if stat in (Device.Status.ACTIVE, Device.Status.INSTALLED, Device.Status.UNDER_MAINTENANCE)
                else None
            )

            purchase_date = date.today() - timedelta(days=random.randint(30, 365))

            device, created = Device.objects.get_or_create(
                serial_number=serial,
                defaults={
                    "device_model": dm,
                    "status": stat,
                    "supplier": sup,
                    "current_site": site,
                    "assigned_client": client,
                    "assigned_technician": tech,
                    "purchase_date": purchase_date,
                    "purchase_price": Decimal(str(random.randint(500, 15000))),
                    "invoice_reference": f"INV-{2026}-PK-{i+1:04d}",
                    "batch_number": f"BATCH-PK-{(i // 5) + 1:03d}",
                    "firmware_version": f"v{random.randint(1, 4)}.{random.randint(0, 9)}.{random.randint(0, 20)}",
                    "installation_date": (
                        purchase_date + timedelta(days=random.randint(5, 30))
                        if stat in (Device.Status.ACTIVE, Device.Status.INSTALLED)
                        else None
                    ),
                    "mac_address": ":".join(f"{random.randint(0, 255):02x}" for _ in range(6)),
                },
            )
            if created:
                self.stdout.write(f"  Created device: {device.asset_code} ({stat}) @ {site.city if site else 'warehouse'}")
            devices.append(device)

        return devices

    # ------------------------------------------------------------------
    # Installations
    # ------------------------------------------------------------------
    def _create_installations(self, devices, sites, zones, users):
        installations = []
        technicians = [u for u in users if u.role == "technician"]

        active_devices = [
            d for d in devices
            if d.status in (Device.Status.ACTIVE, Device.Status.INSTALLED) and d.current_site
        ]

        for device in active_devices:
            site = device.current_site
            site_zones = [z for z in zones if z.site_id == site.id]
            zone = random.choice(site_zones) if site_zones else None
            tech = random.choice(technicians) if technicians else None

            inst, created = DeviceInstallation.objects.get_or_create(
                device=device,
                site=site,
                defaults={
                    "zone": zone,
                    "installed_by": tech,
                    "installed_at": timezone.now() - timedelta(days=random.randint(1, 180)),
                    "position_label": f"Position {random.randint(1, 20)}",
                    "notes": "Installed and verified.",
                },
            )
            if created:
                self.stdout.write(f"  Created installation: {device.asset_code} @ {site.name}")
            installations.append(inst)

        return installations

    # ------------------------------------------------------------------
    # Lifecycle Events
    # ------------------------------------------------------------------
    def _create_lifecycle_events(self, devices, users):
        technicians = [u for u in users if u.role == "technician"]
        count = 0

        for device in devices[:20]:
            if DeviceLifecycleEvent.objects.filter(device=device).exists():
                continue

            DeviceLifecycleEvent.objects.create(
                device=device,
                event_type=DeviceLifecycleEvent.EventType.STATUS_CHANGE,
                from_value="procured",
                to_value=device.status,
                description=f"Device moved to {device.get_status_display()}",
                performed_by=random.choice(technicians) if technicians else None,
            )
            count += 1

            if device.status in (Device.Status.ACTIVE, Device.Status.INSTALLED) and device.current_site:
                DeviceLifecycleEvent.objects.create(
                    device=device,
                    event_type=DeviceLifecycleEvent.EventType.LOCATION_CHANGE,
                    to_value=device.current_site.name,
                    description="Device deployed to site",
                    performed_by=random.choice(technicians) if technicians else None,
                )
                count += 1

        self.stdout.write(f"  Created {count} lifecycle events")

    # ------------------------------------------------------------------
    # Tickets
    # ------------------------------------------------------------------
    def _create_tickets(self, devices, sites, users):
        technicians = [u for u in users if u.role == "technician"]
        ops = [u for u in users if u.role in ("ops_manager", "super_admin")]
        tickets = []

        ticket_data = [
            ("Display black screen at Packages Mall", "Screen powered on but no image", "high", "open", "repair"),
            ("Install new kiosk at Dolmen Clifton", "Client requested 55\" kiosk", "medium", "in_progress", "installation"),
            ("Screen flickering at Centaurus", "Intermittent flickering on ground floor", "critical", "open", "repair"),
            ("Replace damaged display at Lucky One", "Physical damage reported", "high", "on_hold", "replacement"),
            ("Annual inspection - Lahore sites", "Scheduled annual audit", "low", "in_progress", "inspection"),
            ("Relocate screens to new Giga Mall wing", "Expansion requires relocation", "medium", "open", "relocation"),
            ("Firmware update - Faisalabad screens", "BrightSign units need v4.2", "low", "resolved", "other"),
            ("HDMI failure at Multan Digital Hub", "HDMI port not detecting signal", "high", "open", "repair"),
            ("New installation at Peshawar Mall", "4 screens for entrance area", "medium", "in_progress", "installation"),
            ("Overheating alert - Quetta unit", "Temperature sensor triggered", "critical", "open", "repair"),
            ("Warranty claim - Samsung panel Islamabad", "Dead pixels on 3-month old panel", "medium", "resolved", "replacement"),
            ("Quarterly cleaning - Karachi sites", "Routine cleaning for all screens", "low", "closed", "inspection"),
        ]

        active_devices = [d for d in devices if d.current_site]
        active_sites = [s for s in sites if s.is_active]

        for i, (title, desc, prio, stat, cat) in enumerate(ticket_data):
            device = active_devices[i % len(active_devices)] if active_devices else None
            site = device.current_site if device and device.current_site else (active_sites[i % len(active_sites)] if active_sites else None)

            ticket, created = Ticket.objects.get_or_create(
                title=title,
                defaults={
                    "description": desc,
                    "priority": prio,
                    "status": stat,
                    "category": cat,
                    "device": device,
                    "site": site,
                    "assigned_to": technicians[i % len(technicians)] if technicians else None,
                    "reported_by": ops[i % len(ops)] if ops else None,
                    "due_date": date.today() + timedelta(days=random.randint(1, 30)),
                },
            )
            if created:
                self.stdout.write(f"  Created ticket: {title[:50]}")
            tickets.append(ticket)

        return tickets

    # ------------------------------------------------------------------
    # Warranties
    # ------------------------------------------------------------------
    def _create_warranties(self, devices, suppliers):
        warranties = []
        active_devices = [d for d in devices if d.status in (Device.Status.ACTIVE, Device.Status.INSTALLED)]

        types = [Warranty.WarrantyType.MANUFACTURER, Warranty.WarrantyType.EXTENDED, Warranty.WarrantyType.SUPPLIER]

        for i, device in enumerate(active_devices[:15]):
            start = device.purchase_date or (date.today() - timedelta(days=365))
            end = start + timedelta(days=random.choice([365, 730, 1095]))
            is_expired = end < date.today()

            warranty, created = Warranty.objects.get_or_create(
                device=device,
                warranty_type=types[i % len(types)],
                defaults={
                    "supplier": suppliers[i % len(suppliers)],
                    "status": "expired" if is_expired else "active",
                    "start_date": start,
                    "end_date": end,
                    "coverage_details": f"Covers hardware defects for {(end - start).days // 365} year(s).",
                    "reference_number": f"WRN-PK-{2026}-{i+1:04d}",
                },
            )
            if created:
                self.stdout.write(f"  Created warranty: {device.asset_code}")
            warranties.append(warranty)

        return warranties

    # ------------------------------------------------------------------
    # Maintenance
    # ------------------------------------------------------------------
    def _create_maintenance(self, devices, sites, users):
        technicians = [u for u in users if u.role == "technician"]
        schedules = []
        records = []

        schedule_data = [
            ("Monthly Display Cleaning", "preventive", "monthly"),
            ("Quarterly Hardware Inspection", "preventive", "quarterly"),
            ("Annual Calibration Check", "preventive", "yearly"),
            ("HVAC Filter Replacement", "preventive", "monthly"),
            ("Emergency Panel Replacement", "corrective", "one_time"),
            ("Predictive Sensor Check", "predictive", "quarterly"),
            ("Weekly Media Player Health Check", "preventive", "weekly"),
            ("Bi-annual Power Supply Test", "preventive", "quarterly"),
        ]

        active_devices = [d for d in devices if d.current_site]
        active_sites = [s for s in sites if s.is_active]

        for i, (title, mtype, freq) in enumerate(schedule_data):
            device = active_devices[i % len(active_devices)] if active_devices else None
            site = device.current_site if device else (active_sites[i % len(active_sites)] if active_sites else None)

            sched, created = MaintenanceSchedule.objects.get_or_create(
                title=title,
                defaults={
                    "maintenance_type": mtype,
                    "frequency": freq,
                    "device": device,
                    "site": site,
                    "assigned_to": technicians[i % len(technicians)] if technicians else None,
                    "next_due": date.today() + timedelta(days=random.randint(1, 60)),
                    "instructions": f"Standard {mtype} procedure. Follow SOP-{i+1:03d}.",
                },
            )
            if created:
                self.stdout.write(f"  Created schedule: {title}")
            schedules.append(sched)

            if freq != "one_time":
                for j in range(random.randint(1, 3)):
                    rec = MaintenanceRecord.objects.create(
                        schedule=sched,
                        performed_by=technicians[(i + j) % len(technicians)] if technicians else None,
                        performed_at=timezone.now() - timedelta(days=random.randint(1, 120)),
                        status=random.choice(["completed", "completed", "partial"]),
                        notes=f"Maintenance #{j+1} completed.",
                        cost=Decimal(str(random.randint(50, 500))),
                    )
                    records.append(rec)

        self.stdout.write(f"  Created {len(records)} maintenance records")
        return schedules, records

    # ------------------------------------------------------------------
    # Inventory
    # ------------------------------------------------------------------
    def _create_inventory(self, material_types, sites, users):
        warehouse_user = next((u for u in users if u.role == "warehouse"), users[0])
        items = []
        movements = []

        active_sites = [s for s in sites if s.is_active]

        for i, mt in enumerate(material_types):
            item, created = InventoryItem.objects.get_or_create(
                sku=f"INV-{mt.category[:3].upper()}-PK-{i+1:04d}",
                defaults={
                    "material_type": mt,
                    "quantity": random.randint(2, 100),
                    "min_stock_level": random.randint(3, 10),
                    "location": "warehouse" if i < 5 else "in_transit",
                    "unit_cost": Decimal(str(random.randint(5, 200))),
                    "notes": f"Inventory for {mt.name}",
                },
            )
            if created:
                self.stdout.write(f"  Created inventory: {mt.name} ({item.sku})")
            items.append(item)

            for j in range(random.randint(1, 3)):
                mv = StockMovement.objects.create(
                    item=item,
                    movement_type=random.choice(["in", "out", "transfer"]),
                    quantity=random.randint(1, 10),
                    reference=f"MV-PK-{2026}-{i*3+j+1:04d}",
                    notes="Routine stock movement",
                    performed_by=warehouse_user,
                )
                movements.append(mv)

        self.stdout.write(f"  Created {len(movements)} stock movements")
        return items, movements

    # ------------------------------------------------------------------
    # Procurement
    # ------------------------------------------------------------------
    def _create_procurement(self, suppliers, users):
        ops = [u for u in users if u.role in ("ops_manager", "super_admin")]
        pos = []

        po_data = [
            ("PO-PK-2026-0001", "ordered", 10),
            ("PO-PK-2026-0002", "received", -30),
            ("PO-PK-2026-0003", "draft", 0),
            ("PO-PK-2026-0004", "pending_approval", 5),
            ("PO-PK-2026-0005", "approved", 7),
            ("PO-PK-2026-0006", "partially_received", -10),
            ("PO-PK-2026-0007", "cancelled", -60),
        ]

        items_desc = [
            ('Samsung 55" QM55R Display', 3, Decimal("4500.00")),
            ("BrightSign XT1144 Media Player", 5, Decimal("750.00")),
            ('Wall Mount Bracket (55")', 10, Decimal("85.00")),
            ("HDMI Cable 5m Premium", 20, Decimal("25.00")),
            ("Cat6 Cable Spool 300m", 2, Decimal("350.00")),
        ]

        for i, (po_num, status, day_offset) in enumerate(po_data):
            supplier = suppliers[i % len(suppliers)]
            order_date = date.today() + timedelta(days=day_offset) if day_offset <= 0 else None
            expected = (date.today() + timedelta(days=day_offset + 30)) if order_date else (date.today() + timedelta(days=30))

            po, created = PurchaseOrder.objects.get_or_create(
                po_number=po_num,
                defaults={
                    "supplier": supplier,
                    "status": status,
                    "order_date": order_date,
                    "expected_delivery": expected,
                    "notes": f"Purchase order for {supplier.name}",
                    "ordered_by": ops[i % len(ops)] if ops else None,
                    "approved_by": ops[0] if status in ("approved", "ordered", "received", "partially_received") and ops else None,
                },
            )
            if created:
                total = Decimal("0")
                selected_items = random.sample(items_desc, k=random.randint(2, 4))
                for desc, qty, price in selected_items:
                    qty_var = random.randint(1, qty)
                    PurchaseOrderItem.objects.create(
                        purchase_order=po,
                        description=desc,
                        quantity=qty_var,
                        unit_price=price,
                        received_quantity=qty_var if status == "received" else (qty_var // 2 if status == "partially_received" else 0),
                    )
                    total += qty_var * price
                po.total_amount = total
                po.save()
                self.stdout.write(f"  Created PO: {po_num} ({status})")
            pos.append(po)

        return pos

    # ------------------------------------------------------------------
    # Finance
    # ------------------------------------------------------------------
    def _create_finance(self, clients, suppliers, pos, users):
        finance_user = next((u for u in users if u.role == "finance"), users[0])
        invoices = []
        payments = []

        active_clients = [c for c in clients if c.is_active]

        inv_data = [
            ("INV-R-PK-2026-001", "receivable", "paid", 0),
            ("INV-R-PK-2026-002", "receivable", "sent", 15),
            ("INV-R-PK-2026-003", "receivable", "overdue", -20),
            ("INV-R-PK-2026-004", "receivable", "draft", 30),
            ("INV-P-PK-2026-001", "payable", "paid", -5),
            ("INV-P-PK-2026-002", "payable", "sent", 10),
            ("INV-P-PK-2026-003", "payable", "partially_paid", 5),
            ("INV-P-PK-2026-004", "payable", "overdue", -15),
        ]

        for i, (inv_num, inv_type, status, day_offset) in enumerate(inv_data):
            amount = Decimal(str(random.randint(200000, 5000000)))
            tax = (amount * Decimal("0.17")).quantize(Decimal("0.01"))
            total = amount + tax
            paid = total if status == "paid" else (total / 2 if status == "partially_paid" else Decimal("0"))

            client = active_clients[i % len(active_clients)] if inv_type == "receivable" and active_clients else None
            supplier = suppliers[i % len(suppliers)] if inv_type == "payable" else None
            po = pos[i % len(pos)] if inv_type == "payable" and pos else None

            inv, created = Invoice.objects.get_or_create(
                invoice_number=inv_num,
                defaults={
                    "invoice_type": inv_type,
                    "status": status,
                    "client": client,
                    "supplier": supplier,
                    "purchase_order": po,
                    "amount": amount,
                    "tax_amount": tax,
                    "total_amount": total,
                    "issue_date": date.today() + timedelta(days=day_offset - 10),
                    "due_date": date.today() + timedelta(days=day_offset),
                    "paid_amount": paid,
                    "created_by": finance_user,
                },
            )
            if created:
                self.stdout.write(f"  Created invoice: {inv_num} ({inv_type}, {status})")

                if paid > 0:
                    pmt = Payment.objects.create(
                        invoice=inv,
                        amount=paid,
                        payment_date=date.today() - timedelta(days=random.randint(0, 10)),
                        method=random.choice(["bank_transfer", "cheque", "credit_card"]),
                        reference=f"PMT-{inv_num}",
                        recorded_by=finance_user,
                    )
                    payments.append(pmt)

            invoices.append(inv)

        return invoices, payments

    # ------------------------------------------------------------------
    # Alerts
    # ------------------------------------------------------------------
    def _create_alerts(self, devices, sites):
        try:
            from apps.analytics.models import Alert
        except ImportError:
            self.stdout.write("  Skipping alerts (model not found)")
            return

        active_devices = [d for d in devices if d.current_site]
        alert_data = [
            ("High temperature detected", "critical", "device_health", 0),
            ("Screen offline for 30+ minutes", "error", "connectivity", 1),
            ("Scheduled maintenance overdue", "warning", "maintenance", 2),
            ("Firmware update available", "info", "device_health", 3),
            ("Low storage on media player", "warning", "device_health", 4),
            ("Unusual power consumption spike", "error", "device_health", 5),
            ("Network latency exceeding threshold", "warning", "connectivity", 6),
            ("New device registered at site", "info", "device_health", 7),
        ]

        count = 0
        for title, severity, category, idx in alert_data:
            device = active_devices[idx % len(active_devices)] if active_devices else None
            site = device.current_site if device else None
            Alert.objects.create(
                title=title,
                severity=severity,
                category=category,
                device=device,
                site=site,
            )
            count += 1

        self.stdout.write(f"  Created {count} alerts")
