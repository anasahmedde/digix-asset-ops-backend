"""Provision the logins the organogram calls for.

Source: DIGIX_Authority_Matrix_v2_signed.xlsx, sheet "1. Tiers & Mapping" —
the sheet the client signed. Each person keeps their organogram title in
``job_title`` and their rights come from ``role``; ``reports_to`` is the
line between them, so the org chart is drawn from the data rather than
maintained twice.

The ISL SMD Operator is deliberately absent: the client's decision was that
no login exists until the post is filled and named.
"""

import os
import secrets

from django.contrib.auth import get_user_model
from django.core.management.base import BaseCommand, CommandError
from django.db import transaction

User = get_user_model()

# No credential lives in the repository. Set DIGIX_SEED_PASSWORD to choose
# the first-login password; leave it unset and each account gets its own
# random one, which nobody can sign in with until it is reset.
PASSWORD_ENV = "DIGIX_SEED_PASSWORD"

# (username, full name, organogram position, system role, reports to)
PEOPLE = [
    ("awais.tariq", "Awais Tariq", "Group Head", "group_head", None),
    ("ahmer", "M Ahmer", "Operation Lead", "ops_manager", "awais.tariq"),
    ("ameen.naeem", "Ameen Naeem", "CS Lead", "marketing_head", "awais.tariq"),
    ("waseem", "Waseem", "Production & R&D Supervisor", "supervisor", "ahmer"),
    ("nazakat", "Nazakat", "Execution Supervisor", "supervisor", "ahmer"),
    ("arshad", "Arshad", "Store Supervisor", "warehouse", "ahmer"),
    ("zain", "Zain", "MIS Operator", "super_admin", "ahmer"),
    ("irfan", "Irfan", "Karachi CS", "marketing", "ameen.naeem"),
    ("adeel", "Adeel", "Lahore CS", "marketing", "ameen.naeem"),
    ("hamza", "Hamza", "ISL CS", "marketing", "ameen.naeem"),
    ("haris", "Haris", "Graphic Designer", "marketing", "ameen.naeem"),
    ("badr", "Badr", "External Client Supervisor", "client_viewer", "ameen.naeem"),
    ("sharjeel", "Sharjeel", "Production Worker", "technician", "waseem"),
    ("sharoon", "Sharoon", "Rider", "technician", "waseem"),
    ("salman", "Salman", "Fabrication Worker", "technician", "waseem"),
    ("jaleel", "Jaleel", "Karachi SMD Operator", "technician", "nazakat"),
    ("zeeshan", "Zeeshan", "Lahore SMD Operator", "technician", "nazakat"),
]

# Who is out in the field rather than at a desk.
FIELD_ROLES = {"technician", "supervisor", "warehouse"}


class Command(BaseCommand):
    help = "Create the organogram's people. --replace removes everyone else first."

    def add_arguments(self, parser):
        parser.add_argument(
            "--replace",
            action="store_true",
            help="Delete every other account first (the superuser running this is kept).",
        )
        parser.add_argument(
            "--keep",
            action="append",
            default=[],
            metavar="USERNAME",
            help="An extra username to keep when replacing. May be repeated.",
        )

    @transaction.atomic
    def handle(self, *args, **options):
        shared = os.environ.get(PASSWORD_ENV)
        if shared is not None and len(shared) < 10:
            raise CommandError(f"{PASSWORD_ENV} is too short to be worth setting.")
        wanted = {p[0] for p in PEOPLE}
        keep = wanted | set(options["keep"]) | set(
            User.objects.filter(is_superuser=True).values_list("username", flat=True)
        )

        if options["replace"]:
            doomed = User.objects.exclude(username__in=keep)
            names = list(doomed.values_list("username", flat=True))
            count, _ = doomed.delete()
            self.stdout.write(f"Removed {count} account(s): {', '.join(names) or 'none'}")

        # Two passes: everyone exists before anyone is pointed at a manager.
        made, updated = 0, 0
        for username, full_name, title, role, _boss in PEOPLE:
            first, _, last = full_name.partition(" ")
            person, created = User.objects.update_or_create(
                username=username,
                defaults={
                    "first_name": first,
                    "last_name": last,
                    "email": f"{username}@digix.pk",
                    "role": role,
                    "job_title": title,
                    "is_field_staff": role in FIELD_ROLES,
                    "is_active": True,
                    # The MIS Operator administers the system, so that login
                    # needs the admin site as well as the API.
                    "is_staff": role == "super_admin",
                },
            )
            if created:
                person.set_password(shared or secrets.token_urlsafe(18))
                person.save(update_fields=["password"])
                made += 1
            else:
                updated += 1

        by_name = {u.username: u for u in User.objects.filter(username__in=wanted)}
        for username, _full, _title, _role, boss in PEOPLE:
            person = by_name[username]
            person.reports_to = by_name.get(boss) if boss else None
            person.save(update_fields=["reports_to"])

        self.stdout.write(
            self.style.SUCCESS(
                f"{made} created, {updated} updated. New accounts use "
                + (f"the password from ${PASSWORD_ENV}."
                   if shared else
                   "a random password each — reset it to let anyone in.")
            )
        )
