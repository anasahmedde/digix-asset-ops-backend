# Tests will be added alongside model implementations.
import pytest
from rest_framework.test import APIClient

from apps.accounts.models import User
from apps.assets.models import MaterialType
from apps.inventory.models import InventoryItem


@pytest.fixture
def ops(db):
    return User.objects.create_user(username="inv-ops", password="x", role="ops_manager")


def _client(user):
    c = APIClient()
    c.force_authenticate(user)
    return c


@pytest.fixture
def items(db):
    cable = MaterialType.objects.create(name="Inv Cable", unit="meter")
    mount = MaterialType.objects.create(name="Inv Mount", unit="piece")
    screw = MaterialType.objects.create(name="Inv Screw", unit="box")
    return [
        InventoryItem.objects.create(material_type=cable, quantity=10, unit_cost=100),  # value 1000
        InventoryItem.objects.create(material_type=mount, quantity=50, unit_cost=5),    # value 250
        InventoryItem.objects.create(material_type=screw, quantity=999, unit_cost=None),  # unpriced
    ]


@pytest.mark.django_db
def test_items_expose_per_row_total_value(ops, items):
    r = _client(ops).get("/api/inventory/items/", {"page_size": 100})
    assert r.status_code == 200, r.content
    by_name = {row["material_name"]: row for row in r.data["results"]}
    assert float(by_name["Inv Cable"]["total_value"]) == 1000
    assert float(by_name["Inv Mount"]["total_value"]) == 250
    assert by_name["Inv Screw"]["total_value"] is None


@pytest.mark.django_db
def test_items_order_by_total_value_desc_puts_unpriced_last(ops, items):
    r = _client(ops).get("/api/inventory/items/", {"ordering": "-total_value", "page_size": 100})
    assert r.status_code == 200, r.content
    names = [row["material_name"] for row in r.data["results"]]
    assert names == ["Inv Cable", "Inv Mount", "Inv Screw"]


@pytest.mark.django_db
def test_items_order_by_total_value_asc(ops, items):
    r = _client(ops).get("/api/inventory/items/", {"ordering": "total_value", "page_size": 100})
    names = [row["material_name"] for row in r.data["results"]]
    assert names == ["Inv Screw", "Inv Mount", "Inv Cable"]
