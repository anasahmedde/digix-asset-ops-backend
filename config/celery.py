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
}
