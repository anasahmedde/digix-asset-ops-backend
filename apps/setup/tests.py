from django.test import TestCase

from common.codes import generate_code
from .models import NumberingScheme


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
