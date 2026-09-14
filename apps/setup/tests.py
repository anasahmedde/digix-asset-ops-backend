from django.db import IntegrityError, transaction
from django.test import TestCase

from common.codes import generate_code
from .models import EscalationPolicy, NumberingScheme
from .serializers import EscalationPolicySerializer


class NumberingSchemeTests(TestCase):
    def test_build_with_year_and_padding(self):
        scheme = NumberingScheme(prefix="WO", separator="-", include_year=True, padding=5)
        code = scheme.build(42)
        self.assertRegex(code, r"^WO-\d{4}-00042$")

    def test_build_without_year(self):
        scheme = NumberingScheme(prefix="SUP", separator="-", include_year=False, padding=4)
        self.assertEqual(scheme.build(7), "SUP-0007")


class GenerateCodeTests(TestCase):
    def test_sequential_and_atomic(self):
        # Default schemes are seeded by migration 0002; normalise the supplier
        # scheme to a known starting point.
        NumberingScheme.objects.update_or_create(
            entity=NumberingScheme.Entity.SUPPLIER,
            defaults={
                "prefix": "SUP", "separator": "-", "include_year": False,
                "padding": 4, "next_number": 1, "is_active": True,
            },
        )
        first = generate_code(NumberingScheme.Entity.SUPPLIER)
        second = generate_code(NumberingScheme.Entity.SUPPLIER)
        self.assertEqual(first, "SUP-0001")
        self.assertEqual(second, "SUP-0002")
        self.assertEqual(NumberingScheme.objects.get(entity="supplier").next_number, 3)

    def test_fallback_when_no_scheme(self):
        # Remove the scheme entirely to exercise the UUID-based fallback path.
        NumberingScheme.objects.filter(entity=NumberingScheme.Entity.CLIENT).delete()
        code = generate_code(NumberingScheme.Entity.CLIENT)
        self.assertTrue(code.startswith("CLI-"))
        self.assertEqual(len(code.split("-")), 3)  # CLI-YYYY-XXXXX


class EscalationPolicyStageTests(TestCase):
    """Wave 4: scope + stage on escalation policies."""

    def test_legacy_rows_migrated_to_ticket_stage1(self):
        for trigger in ("assignment_sla", "response_sla", "due_date"):
            row = EscalationPolicy.objects.get(scope="ticket", trigger=trigger, stage=1)
            self.assertEqual(row.scope, EscalationPolicy.Scope.TICKET)
            self.assertEqual(row.stage, 1)

    def test_seeded_stage_ladders(self):
        s2 = EscalationPolicy.objects.get(scope="ticket", trigger="response_sla", stage=2)
        self.assertEqual(s2.hours, 24)  # stage-1 hours (None -> 0) + 24
        self.assertEqual(s2.escalate_to_role, "group_head")
        self.assertEqual(s2.also_notify_role, "ops_manager")
        self.assertTrue(
            EscalationPolicy.objects.filter(
                scope="ticket", trigger="due_date", stage=2, hours=24,
                escalate_to_role="group_head", also_notify_role="ops_manager",
            ).exists()
        )
        inst1 = EscalationPolicy.objects.get(scope="installation", trigger="due_date", stage=1)
        self.assertEqual(inst1.hours, 0)
        self.assertEqual(inst1.escalate_to_role, "ops_manager")
        self.assertEqual(inst1.also_notify_role, "")
        inst2 = EscalationPolicy.objects.get(scope="installation", trigger="due_date", stage=2)
        self.assertEqual(inst2.hours, 24)
        self.assertEqual(inst2.escalate_to_role, "group_head")
        self.assertEqual(inst2.also_notify_role, "ops_manager")

    def test_unique_per_scope_trigger_stage(self):
        with self.assertRaises(IntegrityError):
            with transaction.atomic():
                EscalationPolicy.objects.create(scope="ticket", trigger="response_sla", stage=1)
        # Same trigger+stage under a different scope is allowed.
        EscalationPolicy.objects.create(scope="installation", trigger="response_sla", stage=1, hours=4)

    def test_serializer_round_trip(self):
        row = EscalationPolicy.objects.get(scope="ticket", trigger="response_sla", stage=2)
        data = EscalationPolicySerializer(row).data
        self.assertEqual(data["scope"], "ticket")
        self.assertEqual(data["stage"], 2)
        self.assertEqual(data["hours"], 24)

        ser = EscalationPolicySerializer(data={
            "scope": "installation", "trigger": "assignment_sla", "stage": 3,
            "hours": 48, "escalate_to_role": "group_head", "also_notify_role": "",
        })
        self.assertTrue(ser.is_valid(), ser.errors)
        obj = ser.save()
        self.assertEqual(
            (obj.scope, obj.trigger, obj.stage, obj.hours),
            ("installation", "assignment_sla", 3, 48),
        )

    def test_serializer_rejects_duplicate_stage(self):
        ser = EscalationPolicySerializer(data={
            "scope": "ticket", "trigger": "response_sla", "stage": 2,
            "hours": 30, "escalate_to_role": "group_head", "also_notify_role": "",
        })
        self.assertFalse(ser.is_valid())
