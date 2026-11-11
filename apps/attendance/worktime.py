"""Man-hours from the times a field job keeps, for any job: a maintenance
visit and an installation are worked, paused, and priced the same way.

A job hands in its lead, helpers, site and times; nothing here knows which
kind of job it is.
"""
from decimal import Decimal, InvalidOperation

from django.utils import timezone
from rest_framework.exceptions import ValidationError

from common import clock


def overlap(a, b):
    """The stretches in both lists of spells."""
    out = []
    for x in a:
        for y in b:
            f, t = max(x["from"], y["from"]), min(x["to"], y["to"])
            if t > f:
                out.append({"from": f, "to": t, "reckoned": x["reckoned"] or y["reckoned"]})
    return sorted(out, key=lambda s: s["from"])


def work_spells(user_id, started, completed, pauses):
    """Start of work to work done, less the pauses, cut day by day.

    ``pauses`` is (paused_at, resumed_at) pairs in time order; an open one
    has resumed_at None.
    """
    from .sessions import split_days

    if not started or not user_id:
        return []
    out, began = [], started
    for paused_at, resumed_at in pauses:
        if paused_at > began:
            out += split_days(user_id, began, paused_at, began, paused_at)
        if resumed_at is None:
            return out
        began = max(began, resumed_at)
    stop = completed or clock.now()
    if stop > began:
        out += split_days(user_id, began, completed, began, stop)
    return out


def crew_hours(lead, helpers, site_id, begin, started, completed, pauses):
    """How long the lead and helpers were at the site for one job.

    The lead's hours are the work itself, start to done less pauses. A
    helper's are their time checked in at the site while that work ran: not
    before it started, not while it was paused, not after. A day nobody
    checked out of ends at the last work they recorded that day (reckoned).
    """
    from .sessions import hours, spells

    if not site_id or not begin:
        return None
    end = completed or timezone.now()
    # ponytail: two jobs at one site in the same hours would each count
    # them; split by asset if crews start doing several at once.
    work = work_spells(lead.pk if lead else None, started, completed, pauses) if started else None
    people = []
    for person in [lead, *helpers]:
        if person is None or any(p["id"] == str(person.pk) for p in people):
            continue
        is_lead = lead is not None and person.pk == lead.pk
        mine = spells(person.pk, site_id, begin, end)
        found = mine if work is None else work if is_lead else overlap(mine, work)
        people.append({
            "id": str(person.pk), "name": person.get_full_name() or person.username,
            "lead": is_lead, "hours": hours(found), "spells": found,
        })
    return {
        "hours": round(sum(p["hours"] for p in people), 2),
        "reckoned": any(s["reckoned"] for p in people for s in p["spells"]),
        "people": people,
    }


def stays(user_id, site_id, first_act, done):
    """Every stay at the site over a job: the check-in its work began under,
    each check-out and check-in again, up to the check-out after it was done."""
    from .models import AttendanceRecord

    if not user_id or not site_id or not first_act:
        return None
    mine = AttendanceRecord.objects.filter(user_id=user_id)
    came = mine.filter(
        check_type=AttendanceRecord.CheckType.CHECK_IN, site_id=site_id, created_at__lte=first_act,
    ).order_by("-created_at").first()
    if came is None:
        return None
    out, stay = [], None
    # ponytail: capped read; a job spans a few days of check-ins, not hundreds.
    for r in mine.filter(created_at__gte=came.created_at).order_by("created_at")[:200]:
        if r.check_type == AttendanceRecord.CheckType.CHECK_OUT:
            if stay:
                stay["checked_out_at"], stay["offline"] = r.created_at, stay["offline"] or r.recorded_offline
                stay = None
            if done and r.created_at >= done:
                break
            continue
        if done and r.created_at > done:
            break
        # Checked in again (here or elsewhere) without checking out: the last stay ends unclosed.
        stay = None
        if r.site_id == site_id:
            stay = {"checked_in_at": r.created_at, "checked_out_at": None, "offline": r.recorded_offline}
            out.append(stay)
    return {
        "checked_in_at": out[0]["checked_in_at"],
        "checked_out_at": out[-1]["checked_out_at"],
        "offline": any(s["offline"] for s in out),
        "stays": out,
    }


def labour_rows(spent, labour):
    """Man-hours at the reviewer's rate per hour, a row per person - the
    lead and each helper at their own rate.

    ``labour`` is {"people": [{"id", "hours", "rate"}]}, or the older
    {"hours", "rate"} for the job as one line. Hours left out are the hours
    counted from the job's times. Returns [{description, quantity,
    unit_cost, amount}].
    """
    if not labour:
        return []
    spent = spent or {}
    found = {p["id"]: p for p in spent.get("people", [])}
    rows = labour.get("people")
    if rows is None:
        rows = [{"hours": labour.get("hours"), "rate": labour.get("rate")}]
    out = []
    for row in rows:
        who = found.get(str(row.get("id"))) if row.get("id") else None
        counted = who["hours"] if who else (0 if row.get("id") else spent.get("hours", 0))
        try:
            rate = Decimal(str(row.get("rate") or 0))
            given = row.get("hours")
            hrs = Decimal(str(given if given not in (None, "") else counted))
        except (InvalidOperation, TypeError):
            raise ValidationError({"labour": ["Hours and the rate per hour are numbers."]})
        if rate < 0 or hrs < 0:
            raise ValidationError({"labour": ["Hours and the rate per hour are not negative."]})
        if not rate or not hrs:
            continue
        hrs = hrs.quantize(Decimal("0.01"))
        out.append({
            "description": f"Man-hours: {who['name'] + ', ' if who else ''}{hrs.normalize():f} h on site",
            "quantity": hrs, "unit_cost": rate, "amount": (hrs * rate).quantize(Decimal("0.01")),
        })
    return out


def manual_rows(extra):
    """Anything else the job cost, added by whoever records it:
    [{description, amount}] checked and returned as [{description, amount}]."""
    out = []
    for row in extra or []:
        what = str(row.get("description") or "").strip()
        if not what:
            raise ValidationError({"cost_lines": ["Every cost line needs a description."]})
        try:
            amount = Decimal(str(row.get("amount")))
        except (InvalidOperation, TypeError):
            raise ValidationError({"cost_lines": [f"{what}: say what it cost."]})
        if amount < 0:
            raise ValidationError({"cost_lines": [f"{what}: a cost is not negative."]})
        out.append({"description": what, "amount": amount.quantize(Decimal("0.01"))})
    return out
