"""
Create or reset a super_admin user with the correct role.

Usage:
    python manage.py create_admin                          # Interactive prompts
    python manage.py create_admin --username admin --password MyPass@123
    python manage.py create_admin --username admin --reset  # Reset existing admin password
"""

import getpass

from django.contrib.auth import get_user_model
from django.core.management.base import BaseCommand, CommandError

User = get_user_model()


class Command(BaseCommand):
    help = "Create a super admin user or reset an existing admin password"

    def add_arguments(self, parser):
        parser.add_argument("--username", type=str, help="Admin username")
        parser.add_argument("--email", type=str, default="", help="Admin email")
        parser.add_argument("--password", type=str, help="Admin password")
        parser.add_argument(
            "--reset",
            action="store_true",
            help="Reset password if user already exists",
        )

    def handle(self, *args, **options):
        username = options["username"]
        email = options["email"] or ""
        password = options["password"]
        reset = options["reset"]

        interactive = not (username and password)

        if not username:
            username = input("Username (default: admin): ").strip() or "admin"

        if interactive and not email:
            email = input("Email (optional): ").strip()

        try:
            user = User.objects.get(username=username)
            if not reset:
                self.stdout.write(
                    self.style.WARNING(
                        f"User '{username}' already exists. Use --reset to reset their password."
                    )
                )
                confirm = input("Reset password and ensure admin role? (y/N): ").strip()
                if confirm.lower() != "y":
                    self.stdout.write("Cancelled.")
                    return
            if not password:
                password = self._prompt_password()
            user.set_password(password)
            user.role = "super_admin"
            user.is_superuser = True
            user.is_staff = True
            user.is_active = True
            user.save()
            self.stdout.write(
                self.style.SUCCESS(
                    f"Password reset and admin role restored for '{username}'."
                )
            )
        except User.DoesNotExist:
            if not password:
                password = self._prompt_password()
            user = User.objects.create_user(
                username=username,
                email=email,
                password=password,
                role="super_admin",
                is_superuser=True,
                is_staff=True,
            )
            self.stdout.write(
                self.style.SUCCESS(f"Super admin '{username}' created successfully.")
            )

        self.stdout.write(f"  Username: {username}")
        self.stdout.write(f"  Role:     super_admin")
        self.stdout.write(f"  Staff:    Yes")
        self.stdout.write(f"  Active:   Yes")

    def _prompt_password(self):
        while True:
            p1 = getpass.getpass("Password: ")
            if len(p1) < 8:
                self.stderr.write(self.style.ERROR("Password must be at least 8 characters."))
                continue
            p2 = getpass.getpass("Confirm password: ")
            if p1 != p2:
                self.stderr.write(self.style.ERROR("Passwords do not match."))
                continue
            return p1
