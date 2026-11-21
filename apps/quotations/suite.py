"""The link to DIGIX Suite, the quotation builder.

A Suite quotation that reaches Production Go-Ahead shows on the Quotations
page. Making a project from it creates the quotation here (accepted), its
project with the BOM, and one asset per product unit. From then on the Suite
job follows the project: it moves to Execution when the project is made here,
and to Sales check (completion requested, Sales to provide the JVC) when the
project is completed.

Every call goes through Suite's own API as a Suite account kept for this link
(settings SUITE_URL / SUITE_EMAIL / SUITE_PASSWORD), so Suite's rules,
notifications and audit trail apply to what is done from here.
"""

from __future__ import annotations

import threading
from decimal import Decimal, InvalidOperation
from urllib.parse import quote

import requests
from django.conf import settings
from django.db import transaction
from django.db.models.signals import post_save
from django.dispatch import receiver
from django.utils import timezone
from django.utils.dateparse import parse_date

from .models import Quotation, QuotationItem

# The Suite stages a project can still be made from.
OPEN_STAGES = ("production_go_ahead", "execution")
# The Suite stages that mean the work is done and handed to Sales.
DONE_STAGES = ("completion_request", "sales_verification", "closed")


class SuiteError(Exception):
    """Suite refused, or could not be reached; the text says which and why."""


def enabled() -> bool:
    return bool(settings.SUITE_URL)


# ponytail: one shared Suite session per process, serialised by a lock; fine
# for a handful of calls a day. A pool is only worth it if the link gets busy.
_session: requests.Session | None = None
_lock = threading.Lock()


def _post(session: requests.Session, channel: str, arg) -> dict | list | None:
    url = f"{settings.SUITE_URL.rstrip('/')}/api/ipc/{quote(channel, safe='')}"
    try:
        r = session.post(url, json={"args": [] if arg is None else [arg]},
                         headers={"X-DIGIX-Request": "1"}, timeout=20)
    except requests.RequestException:
        raise SuiteError("Suite could not be reached. Try again in a minute.")
    try:
        body = r.json()
    except ValueError:
        raise SuiteError(f"Suite answered with an error (HTTP {r.status_code}).")
    if not body.get("ok"):
        raise SuiteError(body.get("error") or f"Suite answered with an error (HTTP {r.status_code}).")
    result = body.get("result")
    if isinstance(result, dict) and result.get("ok") is False:
        raise SuiteError(result.get("error") or "Suite refused the request.")
    return result


def call(channel: str, arg=None):
    """One Suite action, signed in as the link's account (again, if the
    session has lapsed)."""
    global _session
    with _lock:
        for attempt in (1, 2):
            if _session is None:
                session = requests.Session()
                try:
                    _post(session, "auth:login", {"email": settings.SUITE_EMAIL, "password": settings.SUITE_PASSWORD})
                except SuiteError as e:
                    raise SuiteError(f"Signing in to Suite failed: {e}")
                _session = session
            try:
                return _post(_session, channel, arg)
            except SuiteError as e:
                if attempt == 1 and "Login required" in str(e):
                    _session = None
                    continue
                raise


def sign_in_token(user) -> str:
    """What Suite's /sso checks (HS256 with the shared SUITE_SSO_SECRET):
    who it is, by email, for one minute, once."""
    import time
    import uuid

    import jwt

    now = int(time.time())
    return jwt.encode({
        "iss": "digix-asset-ops", "aud": "digix-suite",
        "email": user.email.strip().lower(), "name": user.get_full_name() or user.username,
        "iat": now, "exp": now + 60, "jti": uuid.uuid4().hex,
    }, settings.SUITE_SSO_SECRET, algorithm="HS256")


def _money(value) -> Decimal:
    try:
        return Decimal(str(value or 0)).quantize(Decimal("0.01"))
    except InvalidOperation:
        return Decimal("0.00")


def _units(value) -> int:
    try:
        return int(round(float(value or 0)))
    except (TypeError, ValueError):
        return 0


def _job(row: dict) -> dict:
    """A Suite operations row, as the Quotations page needs it."""
    ex = (row.get("operations") or {}).get("execution") or {}
    data = (ex.get("frozenRevisionSnapshot") or {}).get("data") or {}
    q = data.get("quotation") or {}
    ctx = ex.get("quotationContext") or {}
    workflow = row.get("workflow") or {}
    lines = []
    # A screen is priced by area (sq ft x rate per unit); a custom product by unit.
    for p in data.get("products") or []:
        qty = _units(p.get("qty"))
        if qty < 1:
            continue
        unit = _money(p.get("rate")) if p.get("custom") else _money(_money(p.get("areaSqft")) * _money(p.get("rate")))
        lines.append({"title": str(p.get("title") or "Product")[:300], "qty": qty, "unit_price": unit, "asset": True})
    for x in data.get("additionalItems") or []:
        qty = _units(x.get("qty"))
        if not x.get("selected") or qty < 1:
            continue
        lines.append({"title": str(x.get("name") or x.get("title") or "Additional item")[:300], "qty": qty,
                      "unit_price": _money(x.get("rate")), "asset": False})
    po_amount = ex.get("poAmount") if ex.get("poAmount") is not None else ex.get("goAheadAmount")
    return {
        "record_id": str(row.get("id") or row.get("recordId") or ""),
        "quote_number": str(q.get("number") or ctx.get("quotationNo") or ""),
        "version": ex.get("frozenVersion") or ex.get("version"),
        "job_number": str(ex.get("joNumber") or ""),
        "po_number": str(ex.get("poNumber") or ""),
        "po_amount": None if po_amount is None else _money(po_amount),
        "client": str(q.get("client") or ctx.get("client") or ""),
        "project": str(q.get("project") or ctx.get("project") or ""),
        "location": str(q.get("location") or ctx.get("location") or ""),
        "target_date": ex.get("targetClosingDate") or None,
        "stage": str(workflow.get("stage") or ""),
        "stage_display": str(workflow.get("label") or ""),
        "net": _money(ex.get("quotationNet")),
        "gst_pct": ex.get("quotationGstPct") or q.get("gstPct") or 0,
        "gross": _money(ex.get("quotationGrand")),
        "discount": _money(q.get("discount")),
        "financials_hidden": bool(row.get("financialsRedacted")),
        "lines": lines,
    }


def _linked(quotation: Quotation | None) -> dict | None:
    if quotation is None:
        return None
    project = quotation.spawned_projects.first()
    return {
        "quotation": str(quotation.pk), "quote_number": quotation.quote_number,
        "project": str(project.pk) if project else None,
        "project_code": project.project_code if project else "",
        "project_status": project.get_status_display() if project else "",
        "suite_acknowledged_at": quotation.suite_acknowledged_at,
        "suite_completed_at": quotation.suite_completed_at,
        "suite_sync_error": quotation.suite_sync_error,
    }


def jobs() -> list[dict]:
    """Suite jobs at Production Go-Ahead or later. Jobs not yet made into a
    project are offered only while a project can still be made from them."""
    rows = (call("operations:list") or {}).get("rows") or []
    by_record = {
        q.suite_record_id: q
        for q in Quotation.objects.filter(suite_record_id__isnull=False).prefetch_related("spawned_projects")
    }
    out = []
    for row in rows:
        if not (row.get("operations") or {}).get("execution"):
            continue
        job = _job(row)
        job["linked"] = _linked(by_record.get(job["record_id"]))
        if job["linked"] or job["stage"] in OPEN_STAGES:
            out.append(job)
    return out


def create_project(record_id: str, client, user):
    """Make the quotation, project and assets for one Suite job."""
    from apps.assets.models import Device
    from apps.finance.ledger import draft_full_invoice
    from apps.notifications import service as notices
    from apps.teams import activity as project_log
    from apps.teams.models import BOMAllocation, Project, ProjectBOMLine

    if not record_id:
        raise SuiteError("Choose a Suite job.")
    if Quotation.objects.filter(suite_record_id=record_id).exists():
        raise SuiteError("A project has already been made from this Suite job.")
    job = _job((call("operations:get-quotation", {"id": record_id}) or {}).get("row") or {})
    if job["stage"] not in OPEN_STAGES:
        raise SuiteError(f"This job is at {job['stage_display'] or 'another stage'} in Suite; "
                         "a project is made only from a Production Go-Ahead.")
    if job["financials_hidden"]:
        raise SuiteError("The Suite account used by this link cannot see prices. "
                         "Make it an Operations Manager in Suite.")
    if not any(line["asset"] for line in job["lines"]):
        raise SuiteError("This Suite quotation has no products to make assets from.")

    title = job["project"] or job["quote_number"] or "Suite quotation"
    refs = " · ".join(x for x in (
        f"Suite {job['quote_number']} V{job['version']}" if job["quote_number"] else "",
        f"JO {job['job_number']}" if job["job_number"] else "",
        f"PO {job['po_number']}" if job["po_number"] else "",
    ) if x)
    with transaction.atomic():
        number = job["quote_number"]
        if number and Quotation.objects.filter(quote_number=number).exists():
            number = ""  # taken here already: let it be numbered like any other
        quotation = Quotation.objects.create(
            quote_number=number, title=title[:300],
            description=f"From DIGIX Suite: {refs}. Suite client: {job['client']}. Location: {job['location']}.",
            status=Quotation.Status.ACCEPTED, accepted_at=timezone.now(),
            client=client, created_by=user,
            client_po_number=job["po_number"][:100], client_po_amount=job["po_amount"],
            notes=(f"Suite GST {job['gst_pct']}% (gross {job['gross']}) is not in the lines; "
                   "set the tax on the invoice." if job["gst_pct"] else ""),
            suite_record_id=record_id, suite_job_number=job["job_number"][:50],
            suite_version=job["version"], suite_client_name=job["client"][:300],
        )
        project = Project.objects.create(
            name=f"Project: {title}"[:300], description=quotation.description,
            location=job["location"][:300], client=client, source_quotation=quotation,
            phase=Project.Phase.PLANNING, status=Project.Status.PLANNING,
            target_date=parse_date(job["target_date"] or "") if job["target_date"] else None,
        )
        assets = []
        for line in job["lines"]:
            item = QuotationItem.objects.create(
                quotation=quotation, description=line["title"], quantity=line["qty"], unit_price=line["unit_price"],
            )
            bom = ProjectBOMLine.objects.create(
                project=project, description=item.description, quantity=item.quantity,
                unit_price=item.unit_price, source_quotation_item=item,
            )
            if line["asset"]:
                # One asset per unit, on the project and covering its BOM line;
                # its route, model and the rest are filled in on the project.
                for _ in range(item.quantity):
                    device = Device.objects.create(display_name=item.description[:200], project=project)
                    BOMAllocation.objects.create(bom_line=bom, device=device, quantity=1, allocated_by=user)
                    assets.append(device)
        if job["discount"]:
            QuotationItem.objects.create(quotation=quotation, description="Discount (Suite)",
                                         quantity=1, unit_price=-job["discount"])
        quotation.recalc_total()
        draft_full_invoice(quotation, user)
        project_log.log(project, "created", (
            f"Created from DIGIX Suite {refs} with {len(assets)} asset{'s' if len(assets) != 1 else ''}"
        ), user=user)

    notices.tell(
        notices.holders_of("edit_projects", exclude=[user]),
        kind="work_assigned",
        title=f"Project opened from Suite {job['job_number'] or job['quote_number']}",
        message=f"{client.name} · {title} — {len(assets)} assets; assign its manager",
        link=f"/projects?project={project.pk}",
        data={"quotation": str(quotation.pk)},
    )
    return quotation, project, assets


def push(quotation: Quotation) -> None:
    """Move the Suite job up to what has happened here, and note on the
    quotation why not if Suite refuses. Safe to repeat."""
    from apps.teams.models import Project

    if not (enabled() and quotation.suite_record_id):
        return
    project = quotation.spawned_projects.first()
    if project is None:
        return
    now = timezone.now()
    try:
        row = (call("operations:get-quotation", {"id": quotation.suite_record_id}) or {}).get("row") or {}
        stage = (row.get("workflow") or {}).get("stage")
        if stage == "production_go_ahead":
            call("operations:acknowledge-go-ahead", {"id": quotation.suite_record_id})
            stage = "execution"
        if stage != "production_go_ahead" and not quotation.suite_acknowledged_at:
            quotation.suite_acknowledged_at = now
        if stage == "execution" and project.status == Project.Status.COMPLETED:
            call("operations:complete", {
                "id": quotation.suite_record_id,
                "completionDate": (project.completed_date or timezone.localdate()).isoformat(),
                "note": f"Completed in Asset Management: {project.project_code} {project.name}"[:500],
                "referJvcToSales": True, "jvcAttachments": [], "afterPictures": [],
            })
            stage = "sales_verification"
        if stage in DONE_STAGES and not quotation.suite_completed_at:
            quotation.suite_completed_at = now
        quotation.suite_sync_error = ""
    except SuiteError as e:
        quotation.suite_sync_error = str(e)[:1000]
    quotation.save(update_fields=["suite_acknowledged_at", "suite_completed_at", "suite_sync_error", "updated_at"])


@receiver(post_save, sender="teams.Project")
def _project_completed(sender, instance, **kwargs):
    """A completed project sends its Suite job to Sales check."""
    if instance.status != instance.Status.COMPLETED or not instance.source_quotation_id or not enabled():
        return
    quotation = Quotation.objects.filter(
        pk=instance.source_quotation_id, suite_record_id__isnull=False, suite_completed_at__isnull=True,
    ).first()
    if quotation is not None:
        # ponytail: runs in the request that completed the project (20 s
        # timeout); a failure is kept on the quotation and retried from the
        # Quotations page. Move it to a background task if Suite gets slow.
        transaction.on_commit(lambda: push(quotation))
