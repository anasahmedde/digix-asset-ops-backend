"""The DIGIX Suite link, against a fake Suite that keeps one job's stage."""

from decimal import Decimal

import pytest
from rest_framework.test import APIClient

from apps.accounts.models import User
from apps.assets.models import Device
from apps.clients.models import Client
from apps.quotations import suite
from apps.quotations.models import Quotation
from apps.teams.models import BOMAllocation, Project, ProjectBOMLine

RID = "quote-1795245837570-nfvhxl"


def _row(stage):
    return {
        "id": RID, "workflow": {"stage": stage, "label": stage.replace("_", " ").title()},
        "operations": {"execution": {
            "joNumber": "JO-001", "poNumber": "PO-UNI-001", "poAmount": 542800, "frozenVersion": 1,
            "targetClosingDate": "2026-12-05", "quotationNet": 460000, "quotationGstPct": 18, "quotationGrand": 542800,
            "frozenRevisionSnapshot": {"data": {
                "quotation": {"number": "DIGIX 2026 001", "client": "Unilever", "project": "DMC NSO Axe Category",
                              "location": "Dolmen Mall Clifton, Karachi", "gstPct": 18, "discount": 0},
                "products": [
                    {"title": "P2.5 Indoor LED / SMD Screen", "areaSqft": 60, "qty": 2, "rate": 1500},
                    {"title": "55 inch Android Signage Display", "custom": True, "qty": 3, "rate": 85000},
                ],
                "additionalItems": [
                    {"name": "Installation & Commissioning", "selected": True, "qty": 1, "rate": 25000},
                    {"name": "Steel Structure", "selected": False, "qty": 1, "rate": 40000},
                ],
            }},
        }},
    }


class FakeSuite:
    def __init__(self, stage="production_go_ahead"):
        self.stage, self.calls, self.refuse = stage, [], None

    def __call__(self, channel, arg=None):
        self.calls.append((channel, arg))
        if self.refuse and channel == self.refuse:
            raise suite.SuiteError("Assign a Primary Job Owner before requesting completion.")
        if channel == "operations:list":
            return {"rows": [_row(self.stage)]}
        if channel == "operations:get-quotation":
            return {"row": _row(self.stage)}
        if channel == "operations:acknowledge-go-ahead":
            assert self.stage == "production_go_ahead"
            self.stage = "execution"
        elif channel == "operations:complete":
            assert self.stage == "execution"
            self.stage = "sales_verification"
        return {"ok": True}

    def channels(self):
        return [c for c, _ in self.calls]


@pytest.fixture
def fake(settings, monkeypatch):
    settings.SUITE_URL = "http://suite.test"
    f = FakeSuite()
    monkeypatch.setattr(suite, "call", f)
    return f


@pytest.fixture
def api(db):
    c = APIClient()
    c.force_authenticate(User.objects.create_user(username="st-mkt", password="x", role="marketing"))
    return c


@pytest.fixture
def unilever(db):
    return Client.objects.create(name="Unilever Pakistan")


@pytest.mark.django_db
def test_suite_go_ahead_makes_quotation_project_and_one_asset_per_product_unit(fake, api, unilever):
    listed = api.get("/api/quotations/quotations/suite/").json()
    assert listed["enabled"] is True
    job = listed["jobs"][0]
    assert (job["job_number"], job["client"], job["stage"], job["linked"]) == ("JO-001", "Unilever", "production_go_ahead", None)
    assert [(x["title"], x["qty"], x["asset"]) for x in job["lines"]] == [
        ("P2.5 Indoor LED / SMD Screen", 2, True), ("55 inch Android Signage Display", 3, True),
        ("Installation & Commissioning", 1, False),  # a line not selected in Suite is not billed
    ]

    r = api.post("/api/quotations/quotations/suite/create-project/", {"record_id": RID, "client": str(unilever.pk)}, format="json")
    assert r.status_code == 201, r.content
    assert r.data["assets"] == 5 and r.data["suite_sync_error"] == ""

    q = Quotation.objects.get(suite_record_id=RID)
    assert q.quote_number == "DIGIX 2026 001"  # the same number as in Suite
    assert (q.status, q.client, q.suite_client_name, q.suite_job_number) == ("accepted", unilever, "Unilever", "JO-001")
    assert q.total_amount == Decimal("460000.00")  # net; GST goes on the invoice
    assert q.client_po_number == "PO-UNI-001" and q.client_po_amount == Decimal("542800.00")
    assert q.invoices.count() == 1  # Finance finds it drafted, as for any accepted quotation

    project = Project.objects.get(source_quotation=q)
    assert (project.name, project.client, str(project.target_date)) == ("Project: DMC NSO Axe Category", unilever, "2026-12-05")
    screen = ProjectBOMLine.objects.get(project=project, description="P2.5 Indoor LED / SMD Screen")
    assert (screen.quantity, screen.unit_price) == (2, Decimal("90000.00"))  # 60 sq ft x 1500
    assert Device.objects.filter(project=project).count() == 5
    assert BOMAllocation.objects.filter(bom_line=screen, device__isnull=False).count() == 2
    assert not BOMAllocation.objects.filter(bom_line__description="Installation & Commissioning").exists()

    # Making the project here acknowledged the Go-Ahead in Suite.
    assert fake.stage == "execution" and q.suite_acknowledged_at is not None
    again = api.get("/api/quotations/quotations/suite/").json()["jobs"][0]
    assert again["linked"]["project"] == str(project.pk)
    # One project per Suite job.
    dup = api.post("/api/quotations/quotations/suite/create-project/", {"record_id": RID, "client": str(unilever.pk)}, format="json")
    assert dup.status_code == 400


@pytest.mark.django_db
def test_completed_project_sends_suite_job_to_sales_check_and_a_refusal_can_be_retried(
        fake, api, unilever, django_capture_on_commit_callbacks):
    api.post("/api/quotations/quotations/suite/create-project/", {"record_id": RID, "client": str(unilever.pk)}, format="json")
    q = Quotation.objects.get(suite_record_id=RID)
    project = Project.objects.get(source_quotation=q)

    fake.refuse = "operations:complete"
    with django_capture_on_commit_callbacks(execute=True):
        project.status = Project.Status.COMPLETED
        project.save(update_fields=["status", "updated_at"])
    q.refresh_from_db()
    assert fake.stage == "execution" and q.suite_completed_at is None
    assert "Primary Job Owner" in q.suite_sync_error  # kept to show, and to retry

    fake.refuse = None
    r = api.post(f"/api/quotations/quotations/{q.pk}/suite-sync/")
    assert r.status_code == 200, r.content
    assert r.data["suite_sync_error"] == "" and r.data["suite_completed_at"]
    assert fake.stage == "sales_verification"
    done = [a for c, a in fake.calls if c == "operations:complete"][-1]
    assert done["referJvcToSales"] is True and done["id"] == RID


@pytest.mark.django_db
def test_suite_link_refuses_bad_input_and_closed_jobs(fake, api, unilever):
    url = "/api/quotations/quotations/suite/create-project/"
    assert api.post(url, {"record_id": RID, "client": "not-a-uuid"}, format="json").status_code == 400
    fake.stage = "closed"
    r = api.post(url, {"record_id": RID, "client": str(unilever.pk)}, format="json")
    assert r.status_code == 400 and "Production Go-Ahead" in r.data["detail"]
    assert api.get("/api/quotations/quotations/suite/").json()["jobs"] == []  # a closed job nobody linked is not offered
    assert not Quotation.objects.exists()


@pytest.mark.django_db
def test_suite_link_off_without_an_address(api):
    assert api.get("/api/quotations/quotations/suite/").json() == {"enabled": False, "jobs": []}


@pytest.mark.django_db
def test_suite_sign_in_is_only_for_people_given_the_right(settings):
    import jwt

    from apps.accounts.models import UserCapability
    from apps.accounts.roles import defaults_for

    settings.SUITE_SSO_SECRET = "s" * 40
    # Nobody but the Super Admin holds it until it is ticked on Roles & Rights.
    assert "use_suite" in defaults_for("super_admin")
    assert not any("use_suite" in defaults_for(r) for r in ("group_head", "ops_manager", "marketing_head", "marketing"))

    person = User.objects.create_user(username="st-sso", password="x", role="marketing", email="Sana@Digix.PK",
                                      first_name="Sana", last_name="Khan")
    c = APIClient()
    c.force_authenticate(person)
    url = "/api/quotations/quotations/suite/sign-in/"
    assert c.get(url).status_code == 403

    UserCapability.objects.create(user=person, capability="use_suite", allowed=True)
    r = c.get(url)
    assert r.status_code == 200, r.content
    claims = jwt.decode(r.data["token"], settings.SUITE_SSO_SECRET, algorithms=["HS256"], audience="digix-suite")
    assert (claims["email"], claims["name"], claims["iss"]) == ("sana@digix.pk", "Sana Khan", "digix-asset-ops")
    assert claims["exp"] - claims["iat"] == 60
    assert c.get(url).data["token"] != r.data["token"]  # a fresh one each time

    person.email = ""
    person.save(update_fields=["email"])
    assert c.get(url).status_code == 400
