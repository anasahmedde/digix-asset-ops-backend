"""Set a first-login password for everyone and write the sheet to hand out.

Passwords are generated here, written once to a spreadsheet, and stored
only as hashes — so this file is the single copy and should be treated
that way: share it over something private, and delete it once people have
signed in. Re-running issues fresh passwords and invalidates the old sheet.
"""

import secrets
from pathlib import Path

from django.contrib.auth import get_user_model
from django.core.management.base import BaseCommand, CommandError
from django.db import transaction

User = get_user_model()

# Unambiguous characters only: these get read off a screen and typed by
# hand, and l/1/I/O/0 cost more support calls than they are worth.
ALPHABET = "ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz23456789"

ROLE_LABELS = dict(User.Role.choices)


def make_password(length: int = 12) -> str:
    """A password someone can read aloud, with a digit and symbol on the end."""
    body = "".join(secrets.choice(ALPHABET) for _ in range(length - 2))
    return f"{body}{secrets.choice('23456789')}{secrets.choice('!@#$%&*')}"


class Command(BaseCommand):
    help = "Issue first-login passwords and write a shareable spreadsheet."

    def add_arguments(self, parser):
        parser.add_argument(
            "--out", default="team_logins.xlsx",
            help="Where to write the sheet (default: team_logins.xlsx).",
        )
        parser.add_argument(
            "--only", action="append", default=[], metavar="USERNAME",
            help="Issue for these usernames only. May be repeated.",
        )
        parser.add_argument(
            "--include-inactive", action="store_true",
            help="Include accounts that are switched off.",
        )
        parser.add_argument(
            "--include-superusers", action="store_true",
            help="Also reissue for superuser logins, which are left alone by default.",
        )
        parser.add_argument(
            "--dry-run", action="store_true",
            help="Show who would be issued one, and write nothing.",
        )

    @transaction.atomic
    def handle(self, *args, **options):
        try:
            from openpyxl import Workbook
            from openpyxl.styles import Alignment, Font, PatternFill
            from openpyxl.utils import get_column_letter
        except ImportError as exc:  # pragma: no cover - depends on the env
            raise CommandError("openpyxl is needed to write the sheet: pip install openpyxl") from exc

        people = User.objects.all()
        if options["only"]:
            people = people.filter(username__in=options["only"])
        if not options["include_inactive"]:
            people = people.filter(is_active=True)
        if not options["include_superusers"]:
            # A superuser is somebody's working login, usually the person
            # running this. Taking their password away mid-handover is not
            # a favour, so it takes an explicit flag.
            people = people.exclude(is_superuser=True)
        people = people.select_related("reports_to").order_by("role", "username")

        if not people.exists():
            raise CommandError("Nobody matched — no sheet written.")

        rows = []
        for person in people:
            rows.append((person, make_password()))

        if options["dry_run"]:
            for person, _ in rows:
                self.stdout.write(f"  would issue: {person.username}")
            self.stdout.write(self.style.WARNING(f"{len(rows)} account(s). Nothing written."))
            return

        for person, password in rows:
            person.set_password(password)
            person.save(update_fields=["password"])

        book = Workbook()
        sheet = book.active
        sheet.title = "Team logins"

        headings = [
            "Name", "Job title", "Username", "Password",
            "System role", "Reports to", "Email", "Signed in yet?",
        ]
        sheet.append(headings)
        head_fill = PatternFill("solid", fgColor="0D9488")
        for col in range(1, len(headings) + 1):
            cell = sheet.cell(row=1, column=col)
            cell.font = Font(bold=True, color="FFFFFF")
            cell.fill = head_fill
            cell.alignment = Alignment(vertical="center")
        sheet.freeze_panes = "A2"

        for person, password in rows:
            boss = person.reports_to
            sheet.append([
                person.get_full_name() or person.username,
                person.job_title or "",
                person.username,
                password,
                ROLE_LABELS.get(person.role, person.role),
                (boss.get_full_name() or boss.username) if boss else "",
                person.email,
                "No",
            ])

        # The password column is the one people copy; set it monospaced so
        # a capital I and a lowercase l cannot be confused on the page.
        for row in range(2, len(rows) + 2):
            sheet.cell(row=row, column=4).font = Font(name="Consolas")

        for i, width in enumerate([22, 26, 16, 16, 18, 20, 26, 14], start=1):
            sheet.column_dimensions[get_column_letter(i)].width = width

        note = sheet.cell(row=len(rows) + 3, column=1)
        note.value = (
            "These are first-login passwords. Everyone should change theirs on first sign-in. "
            "This file is the only copy — send it privately and delete it afterwards."
        )
        note.font = Font(italic=True, color="B45309")

        path = Path(options["out"]).resolve()
        book.save(path)
        self.stdout.write(self.style.SUCCESS(f"{len(rows)} login(s) issued. Sheet written to {path}"))
        self.stdout.write("Hand it over privately, and delete it once everyone has signed in.")
