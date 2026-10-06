"""The warranty a project gives its client, asset by asset.

An order is not finished when the last screen is bolted to the wall — it is
finished when the client has been told what cover they have on each asset.
That promise was being made on paper and never written down, so the register
had nothing to answer a claim with months later.

Recording it is therefore a step of Execution, and a project does not call
itself complete until every asset it handed over carries one.
"""

from apps.assets.models import Device


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
