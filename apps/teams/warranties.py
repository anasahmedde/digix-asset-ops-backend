"""The warranty a project gives its client, asset by asset.

An order is not finished when the last screen is bolted to the wall — it is
finished when the client has been told what cover they have on each asset.
That promise was being made on paper and never written down, so the register
had nothing to answer a claim with months later.

Recording it is therefore a step of Execution, and a project does not call
itself complete until every asset it handed over carries one.
"""

from apps.assets.models import Device


def term_label(months: int) -> str:
    """A term the way the asset card quotes it: "6 Months", "1 Year", "2 Years 6 Mo"."""
    if months < 12:
        return f"{months} Month{'' if months == 1 else 's'}"
    years, rest = divmod(months, 12)
    return f"{years} Year{'' if years == 1 else 's'}" + (f" {rest} Mo" if rest else "")


def handed_over(device) -> bool:
    """Has this asset actually reached the client?

    Installed, live, or signed over — the states in which somebody could
    ring up and claim against it.
    """
    return device.status in (
        Device.Status.INSTALLED,
        Device.Status.ACTIVE,
        Device.Status.CLIENT_PROPERTY,
    )


def activation_date(device):
    """The day the asset first went live. The client's cover runs from it."""
    from django.utils import timezone

    from apps.assets.models import DeviceLifecycleEvent

    first = (
        device.lifecycle_events
        .filter(event_type=DeviceLifecycleEvent.EventType.STATUS_CHANGE, to_value=Device.Status.ACTIVE)
        .order_by("created_at")
        .first()
    )
    return timezone.localdate(first.created_at) if first else None


def give_project_cover(device) -> bool:
    """An asset without client cover takes the term its project promised."""
    from apps.assets.serializers import _asset_warranty, _project_of, _upsert_asset_warranty

    if _asset_warranty(device, "client") is not None:
        return False
    project = _project_of(device)
    if project is None or not project.client_warranty_months:
        return False
    return _upsert_asset_warranty(device, "client", months=project.client_warranty_months) is not None


def cover_from_activation(device) -> None:
    """Date the client's cover from the day the asset went live.

    Gives the project's term to an asset that has none, then runs every
    term-based client warranty from activation. Before the asset is live
    nothing is dated: a term recorded in advance waits for it. A reissued
    warranty keeps its own start.
    """
    from dateutil.relativedelta import relativedelta

    started = activation_date(device)
    if started is None:
        return
    give_project_cover(device)
    for warranty in device.warranties.filter(
        warranty_type="client", status="active", months__isnull=False, reissued_from__isnull=True,
    ):
        warranty.start_date = started
        warranty.end_date = started + relativedelta(months=warranty.months)
        warranty.save(update_fields=["start_date", "end_date", "updated_at"])


def client_warranty(device):
    """The asset's live client warranty, if it has been given one."""
    return next(
        (
            w for w in device.warranties.all()
            if w.warranty_type == "client" and w.status in ("active", "claimed")
        ),
        None,
    )


def warranty_rows(project):
    """Every asset on the project, and the cover promised on it.

    Assets that have not reached the client yet are listed too — the team
    can record the term in advance — but only the handed-over ones hold the
    project open.
    """
    from apps.teams.costing import project_devices

    rows = []
    for device in project_devices(project).prefetch_related("warranties"):
        cover = client_warranty(device)
        rows.append({
            "device": str(device.pk),
            "asset_code": device.asset_code,
            "asset_name": device.display_name or (
                device.asset_type.name if device.asset_type_id else ""
            ),
            "status": device.status,
            "status_display": device.get_status_display(),
            "handed_over": handed_over(device),
            "installation_date": device.installation_date,
            "warranty": None if cover is None else {
                "id": str(cover.pk),
                "reference_number": cover.reference_number,
                "months": cover.months,
                "start_date": cover.start_date,
                "end_date": cover.end_date,
                "status": cover.status,
            },
        })
    return rows


def missing_client_warranties(project):
    """Handed-over assets with no cover recorded against them."""
    return [
        row for row in warranty_rows(project)
        if row["handed_over"] and row["warranty"] is None
    ]
