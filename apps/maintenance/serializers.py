from rest_framework import serializers

from common.money import HidesMoney

from .models import (
    MaintenancePartRequest,
    MaintenanceRecord,
    MaintenanceRecordPhoto,
    MaintenanceSchedule,
    MaintenanceVisit,
    MaintenanceVisitPhoto,
)


class MaintenanceScheduleSerializer(serializers.ModelSerializer):
    device_code = serializers.CharField(source="device.asset_code", read_only=True, default=None)
    device_name = serializers.CharField(source="device.display_name", read_only=True, default=None)
    device_status = serializers.CharField(source="device.status", read_only=True, default=None)
    site_name = serializers.CharField(source="site.name", read_only=True, default=None)
    # Which order the asset belongs to. It reaches a project by its own link or
    # a Scope row, so the asset is asked rather than one field being read.
    project_name = serializers.SerializerMethodField()
    # Corrective: the whole history, and what this reader may do next. The
    # clients render their buttons from this instead of each keeping their
    # own copy of the rules, which is how they came to disagree.
    # Resolved at call time: the visit serializer is defined below this one.
    visits = serializers.SerializerMethodField()
    allowed_actions = serializers.SerializerMethodField()

    def get_visits(self, obj):
        """Every attendance on this job, breakdown or scheduled round alike.

        Both are rendered by the same panel, so both are described the
        same way — a round that reported itself differently is how the two
        screens drifted apart in the first place.
        """
        rounds = obj.visits.order_by("sequence", "created_at")
        return MaintenanceVisitSerializer(rounds, many=True, context=self.context).data
    is_corrective = serializers.SerializerMethodField()

    def get_is_corrective(self, obj):
        return (
            obj.maintenance_type == MaintenanceSchedule.MaintenanceType.CORRECTIVE
            and obj.ticket_id is not None
        )

    def get_allowed_actions(self, obj):
        from . import lifecycle

        request = self.context.get("request")
        user = getattr(request, "user", None)
        return lifecycle.allowed_actions(obj, user) if user else []

    def get_project_name(self, obj):
        if obj.device_id is None:
            return None
        project = obj.device.project_on
        return project.name if project is not None else None

    assigned_to_name = serializers.CharField(source="assigned_to.get_full_name", read_only=True, default=None)
    status_display = serializers.CharField(source="get_status_display", read_only=True)
    effective_status = serializers.CharField(read_only=True)
    # Who is going next, which is rarely the same person every month.
    next_visit_assignee = serializers.SerializerMethodField()
    # The fault that raised this job, when it came in as a ticket.
    ticket_number = serializers.CharField(source="ticket.ticket_number", read_only=True, default=None)
    # What the person reporting the fault actually wrote. Read from the
    # ticket, not kept again here: one answer, and it stays right when the
    # ticket is edited.
    ticket_description = serializers.CharField(
        source="ticket.description", read_only=True, default="",
    )
    ticket_raised_at = serializers.DateTimeField(
        source="ticket.created_at", read_only=True, default=None,
    )
    # The last person who actually worked on this asset, on any job of any
    # kind. "Who was this schedule assigned to" is a different question and
    # the wrong one for a fault: what the office wants to know is who was
    # here last, because they are the one who knows the thing.
    last_technician_on_asset = serializers.SerializerMethodField()

    def get_last_technician_on_asset(self, obj):
        if not obj.device_id:
            return None
        from .models import MaintenanceRecord

        done = (
            MaintenanceRecord.objects
            .filter(schedule__device_id=obj.device_id, performed_by__isnull=False)
            .select_related("performed_by")
            .order_by("-performed_at")
            .first()
        )
        if done is not None:
            who = done.performed_by
            return {
                "name": who.get_full_name() or who.username,
                "when": done.performed_at,
            }
        # Nothing finished yet — the last person who was sent is the best
        # answer there is.
        from .models import MaintenanceVisit

        sent = (
            MaintenanceVisit.objects
            .filter(schedule__device_id=obj.device_id, assigned_to__isnull=False)
            .select_related("assigned_to")
            .order_by("-due_date", "-created_at")
            .first()
        )
        if sent is None:
            return None
        who = sent.assigned_to
        return {"name": who.get_full_name() or who.username, "when": None}
    vendor_names = serializers.SerializerMethodField()
    # A schedule has to say when its rounds begin: the next one due is worked
    # out from it, so without it there is nothing to work out.
    start_date = serializers.DateField(required=True)

    class Meta:
        model = MaintenanceSchedule
        fields = [
            "id", "title", "maintenance_type", "frequency", "priority",
            "device", "device_code", "device_name", "device_status", "site", "site_name",
            "project_name",
            "assigned_to", "assigned_to_name", "vendors", "vendor_names",
            "start_date", "next_due", "instructions", "required_components",
            "status", "status_display",
            "effective_status", "is_active", "next_visit_assignee",
            "ticket", "ticket_number", "ticket_description", "ticket_raised_at",
            "last_technician_on_asset",
            "visits", "allowed_actions", "is_corrective",
            "created_at", "updated_at",
        ]
        # next_due is worked out from the start date and the frequency, and
        # moves on by itself as rounds are completed. Accepting it would let a
        # caller set a date that disagrees with the two it comes from, and the
        # model would overwrite it on save anyway.
        read_only_fields = ["id", "next_due", "created_at", "updated_at"]

    def get_next_visit_assignee(self, obj):
        visit = next(
            (v for v in obj.visits.all() if v.status in ("planned", "in_progress")), None
        )
        person = visit.assigned_to if visit else None
        if person is None:
            return None
        return person.get_full_name() or person.username

    def get_vendor_names(self, obj):
        return [v.name for v in obj.vendors.all()]

    def validate(self, attrs):
        # A finished one-off job stays finished: its completion record is the
        # history, and a new fault raises a new job. Without this, a stale copy
        # of the schedule on someone's screen, saved from the edit form,
        # silently reopened jobs that had already been closed out.
        instance = self.instance
        if (
            instance is not None
            and instance.status == MaintenanceSchedule.Status.COMPLETED
            and not instance.is_active
            and "status" in attrs
            and attrs["status"] != MaintenanceSchedule.Status.COMPLETED
        ):
            raise serializers.ValidationError({
                "status": "This job was completed and closed out — raise a new one for a new fault."
            })
        return attrs

    def validate_required_components(self, value):
        """What the visit takes along, picked from inventory where possible.

        Each row is a stock item, an opened unique product, or (for older
        schedules) a name typed by hand. The name is filled in from inventory,
        so the list reads the same however it was built.
        """
        from django.core.exceptions import ValidationError as DjangoValidationError

        from apps.inventory.models import InventoryItem, InventoryUnitType

        if not isinstance(value, list):
            raise serializers.ValidationError("Must be a list of components.")
        cleaned = []
        for row in value:
            if not isinstance(row, dict):
                raise serializers.ValidationError("Each component must be an object.")
            try:
                quantity = max(1, int(row.get("quantity", 1)))
            except (TypeError, ValueError):
                quantity = 1
            name = str(row.get("name", "")).strip()
            entry = {"quantity": quantity}
            try:
                if row.get("inventory_item"):
                    item = (
                        InventoryItem.objects.select_related("material_type")
                        .filter(pk=row["inventory_item"]).first()
                    )
                    if item is None:
                        raise serializers.ValidationError("That stock item no longer exists.")
                    entry["inventory_item"] = str(item.pk)
                    entry["name"] = name or (item.material_type.name if item.material_type_id else item.sku)
                elif row.get("inventory_unit_type"):
                    product = InventoryUnitType.objects.filter(pk=row["inventory_unit_type"]).first()
                    if product is None:
                        raise serializers.ValidationError("That product no longer exists.")
                    entry["inventory_unit_type"] = str(product.pk)
                    entry["name"] = name or str(product)
                elif name:
                    entry["name"] = name
                else:
                    raise serializers.ValidationError(
                        "Each component needs an inventory item — or at least a name."
                    )
            except (DjangoValidationError, ValueError):
                raise serializers.ValidationError("That inventory reference is not valid.")
            entry["name"] = entry["name"][:200]
            cleaned.append(entry)
        return cleaned


class MaintenanceRecordPhotoSerializer(serializers.ModelSerializer):
    class Meta:
        model = MaintenanceRecordPhoto
        fields = ["id", "record", "image", "caption", "taken_by", "created_at"]
        read_only_fields = ["id", "taken_by", "created_at"]


class MaintenanceVisitPhotoSerializer(serializers.ModelSerializer):
    """A photograph, and what it is a photograph of."""

    kind_display = serializers.CharField(source="get_kind_display", read_only=True)
    taken_by_name = serializers.SerializerMethodField()

    class Meta:
        model = MaintenanceVisitPhoto
        fields = [
            "id", "visit", "kind", "kind_display", "image", "caption",
            "taken_by", "taken_by_name", "taken_at", "latitude", "longitude",
        ]
        # The time and the photographer are the server's to record, not the
        # caller's to claim.
        read_only_fields = ["id", "taken_by", "taken_by_name", "taken_at"]

    def get_taken_by_name(self, obj):
        who = obj.taken_by
        return (who.get_full_name() or who.username) if who else None


class MaintenanceVisitSerializer(HidesMoney, serializers.ModelSerializer):
    """One round of a schedule — when it is due and who is going."""

    assigned_to_name = serializers.SerializerMethodField()
    status_display = serializers.CharField(source="get_status_display", read_only=True)
    schedule_title = serializers.CharField(source="schedule.title", read_only=True)
    # What the round came to, once it was closed out.
    performed_at = serializers.DateTimeField(source="record.performed_at", read_only=True, default=None)
    performed_by_name = serializers.SerializerMethodField()
    cost = serializers.DecimalField(
        source="record.cost", max_digits=10, decimal_places=2, read_only=True, default=None,
    )
    is_billable = serializers.BooleanField(source="record.is_billable", read_only=True, default=None)
    charge_to = serializers.CharField(source="record.charge_to", read_only=True, default="")
    record_notes = serializers.CharField(source="record.notes", read_only=True, default="")
    component_names = serializers.SerializerMethodField()
    photos = serializers.SerializerMethodField()
    # Corrective: the evidence and the verdicts the office reviews.
    visit_photos = MaintenanceVisitPhotoSerializer(source="photos", many=True, read_only=True)
    review_decision_display = serializers.CharField(
        source="get_review_decision_display", read_only=True, default="",
    )
    reviewed_by_name = serializers.SerializerMethodField()
    has_before_photo = serializers.BooleanField(read_only=True)
    has_after_photo = serializers.BooleanField(read_only=True)
    # What the parts this visit used are worth, so the person accepting it
    # sees the figure before they commit to it rather than after.
    component_costs = serializers.SerializerMethodField()
    cost_lines = serializers.SerializerMethodField()

    def get_component_costs(self, obj):
        from . import lifecycle

        return [
            {
                "part_request": row["part_request"],
                "description": row["description"],
                "quantity": str(row["quantity"]),
                "unit_cost": str(row["unit_cost"]) if row["unit_cost"] is not None else None,
                "amount": str(row["amount"]),
            }
            for row in lifecycle.component_costs(obj)
        ]

    def get_cost_lines(self, obj):
        """What it came to in the end, once the office had accepted it."""
        if not obj.record_id:
            return []
        return [
            {
                "id": str(line.pk), "source": line.source,
                "description": line.description,
                "quantity": str(line.quantity) if line.quantity is not None else None,
                "unit_cost": str(line.unit_cost) if line.unit_cost is not None else None,
                "amount": str(line.amount),
            }
            for line in obj.record.cost_lines.all()
        ]

    class Meta:
        model = MaintenanceVisit
        fields = [
            "id", "schedule", "schedule_title", "due_date", "assigned_to", "assigned_to_name",
            "status", "status_display", "started_at", "record", "notes",
            "start_latitude", "start_longitude",
            "performed_at", "performed_by_name", "cost", "is_billable", "charge_to",
            "record_notes", "component_names", "photos",
            "sequence", "completed_at", "resolved", "remarks",
            "review_decision", "review_decision_display", "review_reason",
            "review_note", "reviewed_by", "reviewed_by_name",
            "reviewed_at", "visit_photos", "has_before_photo", "has_after_photo",
            "component_costs", "cost_lines",
            "created_at", "updated_at",
        ]
        # A round is closed out by recording the visit, not by editing it here.
        read_only_fields = [
            "id", "schedule", "status", "started_at", "record", "created_at", "updated_at",
            # Stamped by the act of starting, never typed in afterwards.
            "start_latitude", "start_longitude",
            # Everything below is written by the lifecycle, never by a PATCH:
            # a verdict somebody could edit is not a verdict.
            "sequence", "completed_at", "resolved", "remarks", "review_decision",
            "review_reason", "review_note", "reviewed_by", "reviewed_at",
        ]

    def _name(self, user):
        if user is None:
            return None
        return user.get_full_name() or user.username

    def get_assigned_to_name(self, obj):
        return self._name(obj.assigned_to)

    def get_reviewed_by_name(self, obj):
        return self._name(obj.reviewed_by)

    def get_performed_by_name(self, obj):
        return self._name(obj.record.performed_by) if obj.record_id else None

    def get_component_names(self, obj):
        return [c.name for c in obj.record.components_used.all()] if obj.record_id else []

    def get_photos(self, obj):
        if not obj.record_id:
            return []
        return MaintenanceRecordPhotoSerializer(obj.record.photos.all(), many=True).data

    def update(self, instance, validated_data):
        # The open round is the next one due, so moving its date moves the
        # schedule with it — two answers to "when is it next?" is one too many.
        visit = super().update(instance, validated_data)
        schedule = visit.schedule
        if visit.is_open and schedule.next_due != visit.due_date:
            schedule.next_due = visit.due_date
            schedule.save(update_fields=["next_due", "updated_at"])
        return visit


class MaintenanceRecordSerializer(HidesMoney, serializers.ModelSerializer):
    schedule_title = serializers.CharField(source="schedule.title", read_only=True, default=None)
    performed_by_name = serializers.SerializerMethodField()
    status_display = serializers.CharField(source="get_status_display", read_only=True)
    component_names = serializers.SerializerMethodField()
    photos = MaintenanceRecordPhotoSerializer(many=True, read_only=True)
    # What the visit did with the parts the store issued it: rows of
    # {part_request, used, serials}. It is sent when the visit is closed out,
    # because that is the moment anybody knows.
    parts_settlement = serializers.ListField(
        child=serializers.DictField(), write_only=True, required=False
    )
    # The round being closed out. Left out, it is the open one on the schedule.
    visit = serializers.PrimaryKeyRelatedField(
        queryset=MaintenanceVisit.objects.all(), write_only=True, required=False,
    )
    # The receipt the returned material waits on, so whoever closed the visit
    # can be told where it went. Set while the visit is being recorded.
    return_grn = serializers.SerializerMethodField()

    class Meta:
        model = MaintenanceRecord
        fields = [
            "id", "schedule", "schedule_title",
            "performed_by", "performed_by_name",
            "performed_at", "status", "status_display", "notes", "cost",
            "is_billable", "charge_to",
            "components_used", "component_names", "photos",
            "parts_settlement", "return_grn", "visit",
            "created_at",
        ]
        read_only_fields = ["id", "performed_by", "created_at"]

    def get_performed_by_name(self, obj):
        """Whoever attended, by whatever name the system knows them."""
        user = obj.performed_by
        if user is None:
            return None
        return user.get_full_name() or user.username

    def get_return_grn(self, obj):
        return getattr(obj, "return_grn", None)

    def get_component_names(self, obj):
        return [c.name for c in obj.components_used.all()]

    def create(self, validated_data):
        # Warranty-aware billability defaults (MW-01/02) — explicit payload
        # values win over the derived ones.
        from django.db import transaction

        from apps.warranties.services import derive_billability

        from .parts import settle

        settlement = validated_data.pop("parts_settlement", None)
        visit = validated_data.pop("visit", None)
        schedule = validated_data.get("schedule")
        device = schedule.device if schedule else None
        if device is not None:
            _, billable, charge = derive_billability(device)
            if "is_billable" not in validated_data:
                validated_data["is_billable"] = billable
            if not validated_data.get("charge_to"):
                validated_data["charge_to"] = charge
        # One action: the visit is recorded and its leftovers are handed back
        # together, so a rejected return cannot leave a closed-out visit whose
        # parts are unaccounted for.
        with transaction.atomic():
            record = super().create(validated_data)
            # A visit is a round of the schedule: closing it out is what this
            # record is, so the two are tied together here.
            if visit is None and schedule is not None:
                visit = schedule.open_visit()
            if visit is not None:
                visit.record = record
                visit.status = MaintenanceVisit.Status.COMPLETED
                visit.save(update_fields=["record", "status", "updated_at"])
            if settlement:
                request = self.context.get("request")
                receipt = settle(
                    schedule, user=getattr(request, "user", None), rows=settlement,
                    visit=visit,
                )
                record.return_grn = receipt.grn_number if receipt else None
        return record

    def validate(self, attrs):
        schedule = attrs.get("schedule") or getattr(self.instance, "schedule", None)
        components = attrs.get("components_used") or []
        if schedule and schedule.device_id:
            for component in components:
                if component.device_id != schedule.device_id:
                    raise serializers.ValidationError(
                        {"components_used": "All components must belong to the schedule's asset."}
                    )
        elif components:
            raise serializers.ValidationError(
                {"components_used": "This schedule has no asset — components cannot be attached."}
            )
        return attrs


class MaintenancePartRequestSerializer(serializers.ModelSerializer):
    """A part a technician has asked for on a job, and what was agreed."""

    what = serializers.CharField(read_only=True)
    unit = serializers.CharField(read_only=True)
    status_display = serializers.CharField(source="get_status_display", read_only=True)
    schedule_title = serializers.CharField(source="schedule.title", read_only=True)
    requested_by_name = serializers.SerializerMethodField()
    decided_by_name = serializers.SerializerMethodField()
    # Where the store has got to with it, once the line became a request.
    issue_status = serializers.CharField(
        source="issuance_request.status", read_only=True, default=None,
    )
    issue_number = serializers.CharField(
        source="issuance_request.request_number", read_only=True, default=None,
    )
    quantity_issued = serializers.IntegerField(
        source="issuance_request.quantity_issued", read_only=True, default=None,
    )
    # Which units went out, so the ones coming back can be named.
    issued_serials = serializers.JSONField(
        source="issuance_request.issued_serials", read_only=True, default=list,
    )

    class Meta:
        model = MaintenancePartRequest
        fields = [
            "id", "schedule", "schedule_title", "item", "unit_type", "name",
            "what", "unit", "quantity_requested", "quantity_approved",
            "status", "status_display", "reason",
            "requested_by", "requested_by_name",
            "decided_by", "decided_by_name", "decided_at", "decision_note",
            "issuance_request", "issue_status", "issue_number", "quantity_issued",
            "issued_serials", "visit", "quantity_used", "quantity_returned", "return_reference",
            "created_at", "updated_at",
        ]
        # The answer is given through the decide action, which is where the
        # store request gets raised — setting these directly would approve a
        # line without anything reaching the store.
        read_only_fields = [
            "id", "status", "quantity_approved", "requested_by", "decided_by",
            "decided_at", "decision_note", "issuance_request",
            "quantity_used", "quantity_returned", "return_reference",
            "created_at", "updated_at",
        ]

    def _name_of(self, user):
        if user is None:
            return None
        return user.get_full_name() or user.username

    def get_requested_by_name(self, obj):
        return self._name_of(obj.requested_by)

    def get_decided_by_name(self, obj):
        return self._name_of(obj.decided_by)

    def validate(self, attrs):
        item = attrs.get("item") or getattr(self.instance, "item", None)
        unit_type = attrs.get("unit_type") or getattr(self.instance, "unit_type", None)
        if item and unit_type:
            raise serializers.ValidationError(
                {"item": "A line names one thing: a stock item or a unique product, not both."}
            )
        if not item and not unit_type and not (attrs.get("name") or "").strip():
            raise serializers.ValidationError(
                {"item": "Say what is needed — pick it from stock, or name it."}
            )
        if attrs.get("quantity_requested", 1) < 1:
            raise serializers.ValidationError({"quantity_requested": "Ask for at least one."})
        return attrs


class MaintenancePartDecisionSerializer(serializers.Serializer):
    """A supervisor's answer to one line."""

    approve = serializers.BooleanField()
    # How much is actually being released. Left out on an approval, the whole
    # amount asked for is agreed.
    quantity = serializers.IntegerField(required=False, min_value=1)
    note = serializers.CharField(required=False, allow_blank=True, default="")

    def validate(self, attrs):
        # A rejection with no reason tells the technician nothing: they
        # cannot tell a wrong part from a part the store has not got, so
        # they ask again and the same answer comes back.
        if not attrs.get("approve") and not (attrs.get("note") or "").strip():
            raise serializers.ValidationError(
                {"note": "Say why this is being refused."}
            )
        return attrs
