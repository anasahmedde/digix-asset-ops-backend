from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("notifications", "0002_notification_is_actionable_notification_resolved_at_and_more"),
    ]

    operations = [
        migrations.AlterField(
            model_name="notification",
            name="notification_type",
            field=models.CharField(
                choices=[
                    ("alert", "Alert"),
                    ("chat_message", "Chat Message"),
                    ("ticket_assigned", "Ticket Assigned"),
                    ("ticket_update", "Ticket Update"),
                    ("ticket_review", "Ticket Review Request"),
                    ("maintenance_reminder", "Maintenance Reminder"),
                    ("system", "System"),
                ],
                default="system",
                max_length=30,
            ),
        ),
    ]
