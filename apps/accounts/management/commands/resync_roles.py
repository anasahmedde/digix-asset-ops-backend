"""Put the built-in roles back in step with the code defaults.

`ensure_seeded` creates roles that do not exist and grants capabilities the
database has never heard of, but it deliberately never takes anything away
— a role somebody has edited keeps their choices. That leaves one gap: a
change to what a built-in role *should* have, where the capability already
exists elsewhere, reaches nobody.

This command closes it, and because it overwrites, it says exactly what it
would change and asks before doing it.
"""

from django.core.management.base import BaseCommand

from apps.accounts.capabilities import ROLE_DEFAULTS
from apps.accounts.models import RoleDefinition
from apps.accounts.roles import ensure_seeded, forget


class Command(BaseCommand):
    help = "Reset built-in roles to the capabilities defined in code."

    def add_arguments(self, parser):
        parser.add_argument(
            "--apply", action="store_true",
            help="Actually write the changes. Without it, only report them.",
        )
        parser.add_argument(
            "--only", action="append", default=[], metavar="ROLE",
            help="Limit to these role keys. May be repeated.",
        )

    def handle(self, *args, **options):
        ensure_seeded()
        rows = RoleDefinition.objects.filter(is_builtin=True)
        if options["only"]:
            rows = rows.filter(key__in=options["only"])

        changes = []
        for role in rows:
            want = set(ROLE_DEFAULTS.get(role.key, frozenset()))
            have = set(role.capabilities or ())
            if want == have:
                continue
            changes.append((role, sorted(want - have), sorted(have - want)))

        if not changes:
            self.stdout.write(self.style.SUCCESS("Every built-in role already matches the code."))
            return

        for role, gaining, losing in changes:
            self.stdout.write(f"{role.label} ({role.key}) — {role.holders} holder(s)")
            if gaining:
                self.stdout.write(self.style.SUCCESS(f"    + {', '.join(gaining)}"))
            if losing:
                self.stdout.write(self.style.WARNING(f"    - {', '.join(losing)}"))

        if not options["apply"]:
            self.stdout.write("")
            self.stdout.write(self.style.WARNING(
                f"{len(changes)} role(s) differ. Nothing written — pass --apply to write them."
            ))
            return

        for role, _gaining, _losing in changes:
            role.capabilities = sorted(ROLE_DEFAULTS.get(role.key, frozenset()))
            role.save(update_fields=["capabilities", "updated_at"])
        forget()
        self.stdout.write(self.style.SUCCESS(f"{len(changes)} role(s) reset to the code defaults."))
