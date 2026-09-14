from decimal import Decimal

import pytest
from rest_framework.test import APIClient

from apps.accounts.models import User
from apps.clients.models import Client
from apps.quotations.models import Quotation, QuotationItem
from apps.sites.models import Site
from apps.teams.models import Project, ProjectBOMLine


def _client(user):
    c = APIClient()
    c.force_authenticate(user)
    return c


@pytest.fixture
def people(db):
    return {
        "ops": User.objects.create_user(username="qt-ops", password="x", role="ops_manager"),
        "tech": User.objects.create_user(username="qt-tech", password="x", role="technician"),
    }


@pytest.fixture
def customer(db):
    return Client.objects.create(name="Quotation Test Client")


@pytest.fixture
def site(db):
    return Site.objects.create(name="QT Site", city="Karachi")


def _create_quotation(api, customer, **extra):
    payload = {
        "title": "LED wall for atrium",
        "client": str(customer.id),
        "items": [
            {"description": "P4 LED panel", "quantity": 4, "unit_price": "250.00"},
            {"description": "Installation labour", "quantity": 1, "unit_price": "500.00"},
        ],
    }
    payload.update(extra)
    r = api.post("/api/quotations/quotations/", payload, format="json")
    assert r.status_code == 201, r.content
    return r.json()


@pytest.mark.django_db
def test_quote_number_auto_generated(people, customer):
    api = _client(people["ops"])
    body = _create_quotation(api, customer)
    assert body["quote_number"].startswith("QT-"), body["quote_number"]
    other = _create_quotation(api, customer)
    assert other["quote_number"] != body["quote_number"]
    # status defaults to draft and is read-only on create
    forced = _create_quotation(api, customer, status="accepted")
    assert forced["status"] == "draft"


@pytest.mark.django_db
def test_nested_create_upsert_and_total(people, customer, site):
    api = _client(people["ops"])
    body = _create_quotation(api, customer, site=str(site.id))
    assert body["client_name"] == customer.name
    assert body["site_name"] == site.name
    assert len(body["items"]) == 2
    assert Decimal(str(body["total_amount"])) == Decimal("1500.00")
    assert body["spawned_project"] is None
    assert body["accepted_at"] is None

    kept_id = body["items"][0]["id"]
    dropped_id = body["items"][1]["id"]

    # Upsert: update the kept row in place, add a new one, drop the other.
    r = api.patch(f"/api/quotations/quotations/{body['id']}/", {
        "items": [
            {"id": kept_id, "description": "P4 LED panel", "quantity": 2, "unit_price": "250.00"},
            {"description": "Spare cabling", "quantity": 10, "unit_price": "5.00"},
        ],
    }, format="json")
    assert r.status_code == 200, r.content
    assert len(r.data["items"]) == 2
    ids = {item["id"] for item in r.data["items"]}
    assert kept_id in ids
    assert dropped_id not in ids
    assert Decimal(str(r.data["total_amount"])) == Decimal("550.00")
    assert not QuotationItem.objects.filter(pk=dropped_id).exists()

    # a foreign item id is rejected
    other = _create_quotation(api, customer)
    r = api.patch(f"/api/quotations/quotations/{body['id']}/", {
        "items": [{"id": other["items"][0]["id"], "description": "Smuggled", "quantity": 1}],
    }, format="json")
    assert r.status_code == 400

    # quantity must stay positive
    r = api.patch(f"/api/quotations/quotations/{body['id']}/", {
        "items": [{"description": "Broken", "quantity": 0}],
    }, format="json")
    assert r.status_code == 400


@pytest.mark.django_db
def test_transition_machine_happy_and_invalid(people, customer):
    api = _client(people["ops"])
    body = _create_quotation(api, customer)
    url = f"/api/quotations/quotations/{body['id']}/transition/"

    # draft cannot jump straight to accepted
    r = api.post(url, {"status": "accepted"}, format="json")
    assert r.status_code == 400

    r = api.post(url, {"status": "sent"}, format="json")
    assert r.status_code == 200, r.content
    assert r.data["status"] == "sent"

    r = api.post(url, {"status": "under_negotiation"}, format="json")
    assert r.status_code == 200, r.content

    r = api.post(url, {"status": "rejected"}, format="json")
    assert r.status_code == 200, r.content

    # terminal: nothing moves out of rejected
    r = api.post(url, {"status": "sent"}, format="json")
    assert r.status_code == 400


@pytest.mark.django_db
def test_items_locked_after_sent(people, customer):
    api = _client(people["ops"])
    body = _create_quotation(api, customer)
    r = api.post(f"/api/quotations/quotations/{body['id']}/transition/", {"status": "sent"}, format="json")
    assert r.status_code == 200, r.content

    # item writes are rejected once the quotation left draft…
    r = api.patch(f"/api/quotations/quotations/{body['id']}/", {
        "items": [{"description": "Sneaky add", "quantity": 1, "unit_price": "1.00"}],
    }, format="json")
    assert r.status_code == 400
    assert Quotation.objects.get(pk=body["id"]).items.count() == 2

    # …but header-only edits still work
    r = api.patch(f"/api/quotations/quotations/{body['id']}/", {"notes": "Chased by phone"}, format="json")
    assert r.status_code == 200, r.content
    assert r.data["notes"] == "Chased by phone"


@pytest.mark.django_db
def test_accept_spawns_project_with_bom_lines(people, customer, site):
    from apps.assets.models import AssetType, Brand, DeviceModel, MaterialType

    asset_type = AssetType.objects.create(name="QT SMD Screen")
    dm = DeviceModel.objects.create(brand=Brand.objects.create(name="QTB"), name="QT-55")
    material = MaterialType.objects.create(name="QT HDMI Cable")

    api = _client(people["ops"])
    body = _create_quotation(
        api, customer, site=str(site.id),
        items=[
            {"description": "Screen", "quantity": 2, "unit_price": "1000.00",
             "asset_type": str(asset_type.id), "device_model": str(dm.id)},
            {"description": "Cabling", "quantity": 20, "unit_price": "3.50",
             "material_type": str(material.id)},
        ],
    )
    url = f"/api/quotations/quotations/{body['id']}/transition/"
    assert api.post(url, {"status": "sent"}, format="json").status_code == 200
    r = api.post(url, {"status": "accepted"}, format="json")
    assert r.status_code == 200, r.content
    assert r.data["status"] == "accepted"
    assert r.data["accepted_at"] is not None
    assert r.data["spawned_project"] is not None

    project = Project.objects.get(pk=r.data["spawned_project"])
    assert project.name == "Project: LED wall for atrium"
    assert project.phase == "order_confirmation"
    assert project.client_id == customer.id
    assert project.site_id == site.id
    assert project.source_quotation_id == Quotation.objects.get(pk=body["id"]).id

    lines = list(ProjectBOMLine.objects.filter(project=project).order_by("created_at"))
    assert len(lines) == 2
    by_desc = {line.description: line for line in lines}
    screen = by_desc["Screen"]
    assert screen.quantity == 2
    assert screen.unit_price == Decimal("1000.00")
    assert screen.asset_type_id == asset_type.id
    assert screen.device_model_id == dm.id
    cabling = by_desc["Cabling"]
    assert cabling.material_type_id == material.id
    for line in lines:
        assert line.source_quotation_item is not None
        assert line.source_quotation_item.quotation_id == project.source_quotation_id

    # every quotation item is linked exactly once
    item_ids = set(
        Quotation.objects.get(pk=body["id"]).items.values_list("id", flat=True)
    )
    assert {line.source_quotation_item_id for line in lines} == item_ids

    # detail now exposes the spawned project
    detail = api.get(f"/api/quotations/quotations/{body['id']}/")
    assert detail.data["spawned_project"] == str(project.pk)

    # accepted is terminal
    assert api.post(url, {"status": "sent"}, format="json").status_code == 400


@pytest.mark.django_db
def test_print_returns_pdf(people, customer):
    api = _client(people["ops"])
    body = _create_quotation(api, customer, description="Two-line offer", notes="Valid 30 days")
    r = api.get(f"/api/quotations/quotations/{body['id']}/print/")
    assert r.status_code == 200, r.content
    assert r["Content-Type"] == "application/pdf"
    assert r.content.startswith(b"%PDF")


@pytest.mark.django_db
def test_technician_is_read_only(people, customer):
    ops_api = _client(people["ops"])
    body = _create_quotation(ops_api, customer)

    tech_api = _client(people["tech"])
    assert tech_api.get("/api/quotations/quotations/").status_code == 200
    assert tech_api.post("/api/quotations/quotations/", {
        "title": "Nope", "client": str(customer.id),
    }, format="json").status_code == 403
    assert tech_api.post(
        f"/api/quotations/quotations/{body['id']}/transition/", {"status": "sent"}, format="json"
    ).status_code == 403
