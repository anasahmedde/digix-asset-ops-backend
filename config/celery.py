import os

from celery import Celery

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings.production")

app = Celery("digix")
app.config_from_object("django.conf:settings", namespace="CELERY")
app.autodiscover_tasks()

# Periodic jobs (requires celery beat — run the worker with -B, see deploy compose).
app.conf.beat_schedule = {
    "escalate-overdue-tickets": {
        "task": "apps.tickets.tasks.escalate_overdue_tickets",
        "schedule": 600.0,  # every 10 minutes
    },
    "escalate-overdue-installations": {
        "task": "apps.sites.tasks.escalate_overdue_installations",
        "schedule": 600.0,  # every 10 minutes
    },
    "generate-maintenance-due-alerts": {
        "task": "apps.maintenance.tasks.generate_maintenance_due_alerts",
        "schedule": 21600.0,  # every 6 hours
    },
    "complete-expired-warranties": {
        "task": "apps.warranties.tasks.complete_expired_warranties",
        "schedule": 21600.0,  # every 6 hours
    },
    "deactivate-left-employees": {
        "task": "apps.accounts.tasks.deactivate_left_employees",
        "schedule": 86400.0,  # daily
    },
}
