import django.db.models.deletion
import uuid
from django.conf import settings
from django.db import migrations, models

import common.utils


class Migration(migrations.Migration):

    dependencies = [
        ("tickets", "0001_initial"),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        # Alter status field: new max_length + new choices
        migrations.AlterField(
            model_name="ticket",
            name="status",
            field=models.CharField(
                choices=[
                    ("open", "Open"),
                    ("in_progress", "In Progress"),
                    ("on_hold", "On Hold"),
                    ("blocked", "Blocked"),
                    ("pending_review", "Pending Review"),
                    ("approved", "Approved"),
                    ("rejected", "Rejected"),
                    ("closed", "Closed"),
                ],
                default="open",
                max_length=20,
            ),
        ),
        # New fields on Ticket
        migrations.AddField(
            model_name="ticket",
            name="completion_notes",
            field=models.TextField(blank=True, default=""),
            preserve_default=False,
        ),
        migrations.AddField(
            model_name="ticket",
            name="completed_by",
            field=models.ForeignKey(
                blank=True, null=True,
                on_delete=django.db.models.deletion.SET_NULL,
                related_name="completed_tickets",
                to=settings.AUTH_USER_MODEL,
            ),
        ),
        migrations.AddField(
            model_name="ticket",
            name="completed_at",
            field=models.DateTimeField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name="ticket",
            name="reviewed_by",
            field=models.ForeignKey(
                blank=True, null=True,
                on_delete=django.db.models.deletion.SET_NULL,
                related_name="reviewed_tickets",
                to=settings.AUTH_USER_MODEL,
            ),
        ),
        migrations.AddField(
            model_name="ticket",
            name="reviewed_at",
            field=models.DateTimeField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name="ticket",
            name="review_comments",
            field=models.TextField(blank=True, default=""),
            preserve_default=False,
        ),
        migrations.AddField(
            model_name="ticket",
            name="blocked_reason",
            field=models.TextField(blank=True, default=""),
            preserve_default=False,
        ),
        migrations.AddField(
            model_name="ticket",
            name="hold_reason",
            field=models.TextField(blank=True, default=""),
            preserve_default=False,
        ),
        # TicketAttachment model
        migrations.CreateModel(
            name="TicketAttachment",
            fields=[
                ("id", models.UUIDField(default=uuid.uuid4, editable=False, primary_key=True, serialize=False)),
                ("created_at", models.DateTimeField(auto_now_add=True, db_index=True)),
                ("updated_at", models.DateTimeField(auto_now=True)),
                ("file", models.ImageField(upload_to=common.utils.upload_to_path)),
                ("caption", models.CharField(blank=True, max_length=300)),
                (
                    "attachment_type",
                    models.CharField(
                        choices=[("general", "General"), ("completion", "Completion Evidence"), ("review", "Review Attachment")],
                        default="general",
                        max_length=20,
                    ),
                ),
                (
                    "ticket",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="attachments",
                        to="tickets.ticket",
                    ),
                ),
                (
                    "uploaded_by",
                    models.ForeignKey(
                        null=True,
                        on_delete=django.db.models.deletion.SET_NULL,
                        related_name="ticket_attachments",
                        to=settings.AUTH_USER_MODEL,
                    ),
                ),
            ],
            options={
                "ordering": ["created_at"],
            },
        ),
        # TicketComment model
        migrations.CreateModel(
            name="TicketComment",
            fields=[
                ("id", models.UUIDField(default=uuid.uuid4, editable=False, primary_key=True, serialize=False)),
                ("created_at", models.DateTimeField(auto_now_add=True, db_index=True)),
                ("updated_at", models.DateTimeField(auto_now=True)),
                ("content", models.TextField()),
                (
                    "comment_type",
                    models.CharField(
                        choices=[
                            ("comment", "Comment"),
                            ("status_change", "Status Change"),
                            ("completion", "Completion Submission"),
                            ("approval", "Approval"),
                            ("rejection", "Rejection"),
                        ],
                        default="comment",
                        max_length=20,
                    ),
                ),
                ("old_status", models.CharField(blank=True, max_length=20)),
                ("new_status", models.CharField(blank=True, max_length=20)),
                (
                    "ticket",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="comments",
                        to="tickets.ticket",
                    ),
                ),
                (
                    "author",
                    models.ForeignKey(
                        null=True,
                        on_delete=django.db.models.deletion.SET_NULL,
                        related_name="ticket_comments",
                        to=settings.AUTH_USER_MODEL,
                    ),
                ),
            ],
            options={
                "ordering": ["created_at"],
            },
        ),
    ]
