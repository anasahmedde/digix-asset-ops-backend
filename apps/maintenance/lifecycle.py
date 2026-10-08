"""The corrective lifecycle, in one place.

A fault is reported as a ticket and worked as a job. The job and its visits
are where the work happens — who is going, what they photographed, what they
found — and the ticket mirrors it, so the two can never again say different
things about the same repair.

Everything that moves a corrective job moves it through here. The allowed
moves are declared once, in ``TRANSITIONS``; every caller asks rather than
assumes, and the clients ask the server rather than hard-coding a list of
buttons that goes stale.

    Open ──assign──▶ Assigned ──start──▶ In Progress ──complete──▶ Pending Review
                        ▲                                               │
                        └──────────── mark unresolved ──────────────────┤
                                                                        │
                                             accept ──▶ Closed ◀────────┘

Cancelled is reachable from anything that is not already finished.
"""
from __future__ import annotations

from decimal import Decimal, InvalidOperation

from django.db import transaction
from django.utils import timezone
from rest_framework.exceptions import PermissionDenied, ValidationError

from apps.tickets.models import Ticket, TicketComment

from .models import MaintenanceSchedule, MaintenanceVisit, MaintenanceVisitPhoto

# ── Who may do what ──────────────────────────────────────────────────────
#: Raising, assigning, reviewing, closing and cancelling are the office's.
OFFICE_ROLES = ("super_admin", "group_head", "ops_manager", "supervisor")

#: The named moves, and the ticket status each one leaves behind.
ASSIGN = "assign"
START = "start"
COMPLETE = "complete"
ACCEPT = "accept"
UNRESOLVED = "unresolved"
CANCEL = "cancel"

#: From which ticket status each move may be made. One map, read by the
#: server to decide and by the clients to draw their buttons.
TRANSITIONS: dict[str, tuple[str, ...]] = {
    ASSIGN: (Ticket.Status.OPEN, Ticket.Status.ASSIGNED),
    START: (Ticket.Status.ASSIGNED,),
    COMPLETE: (Ticket.Status.IN_PROGRESS,),
    ACCEPT: (Ticket.Status.PENDING_REVIEW,),
    UNRESOLVED: (Ticket.Status.PENDING_REVIEW,),
    CANCEL: (
        Ticket.Status.OPEN, Ticket.Status.ASSIGNED,
        Ticket.Status.IN_PROGRESS, Ticket.Status.PENDING_REVIEW,
    ),
}

#: What the ticket reads once a move has been made.
LANDS_ON: dict[str, str] = {
    ASSIGN: Ticket.Status.ASSIGNED,
    START: Ticket.Status.IN_PROGRESS,
    COMPLETE: Ticket.Status.PENDING_REVIEW,
    ACCEPT: Ticket.Status.CLOSED,
    UNRESOLVED: Ticket.Status.ASSIGNED,
    CANCEL: Ticket.Status.CANCELLED,
}

#: The steps a reader sees, in order. One list, shared by web and mobile.
STEPPER = (
    Ticket.Status.OPEN,
    Ticket.Status.ASSIGNED,
    Ticket.Status.IN_PROGRESS,
    Ticket.Status.PENDING_REVIEW,
    Ticket.Status.CLOSED,
)

TERMINAL = (Ticket.Status.CLOSED, Ticket.Status.CANCELLED)


def is_office(user) -> bool:
    from common.permissions import can_any

    return can_any(user, "review_maintenance", "assign_maintenance", "manage_maintenance")


def is_admin(user) -> bool:
    """A super admin holds every role's rights, including the technician's.

    The work is still recorded against the technician it was given to —
    this is about who is allowed to press the button, not whose job it is.
    """
    return bool(
        getattr(user, "is_superuser", False)
        or getattr(user, "role", None) == "super_admin"
    )


def theirs(visit: MaintenanceVisit, user) -> bool:
    """Is this visit this person's to work on?"""
    return visit.assigned_to_id == getattr(user, "pk", None) or is_admin(user)


# ── Reading the state ────────────────────────────────────────────────────

def current_visit(job: MaintenanceSchedule) -> MaintenanceVisit | None:
    """The visit the job is on: the last one raised."""
    return job.visits.order_by("-sequence", "-created_at").first()


def _ticket_of(job: MaintenanceSchedule) -> Ticket | None:
    return job.ticket


def allowed_actions(job: MaintenanceSchedule, user) -> list[str]:
    """What this person may do to this job right now.

    The clients render from this rather than deciding for themselves — a
    button list written twice is a button list that disagrees with the
    server, which is how "Complete visit" came to be clickable on a visit
    nobody had started.
    """
    office = is_office(user)
    visit = current_visit(job)
    mine = visit is not None and theirs(visit, user)
    ticket = _ticket_of(job)

    if ticket is None:
        # A scheduled round has no ticket to move, so its own state is the
        # whole answer. The moves are the same ones the office gets on a
        # breakdown, including sending the work back for another visit —
        # an unsatisfactory round is unsatisfactory whatever raised it.
        # The one it does not have is cancelling: a schedule is stopped by
        # pausing it, which is a different thing in a different place.
        done = (MaintenanceSchedule.Status.COMPLETED, MaintenanceSchedule.Status.CANCELLED)
        if visit is None or not job.is_active or job.status in done:
            return []
        rounds: list[str] = []
        if office and visit.status == MaintenanceVisit.Status.PLANNED:
            rounds.append(ASSIGN)
        if (
            mine and visit.status == MaintenanceVisit.Status.PLANNED
            and visit.assigned_to_id and visit.has_before_photo
            and not parts_not_yet_in_hand(visit)
        ):
            rounds.append(START)
        if (
            mine and visit.status == MaintenanceVisit.Status.IN_PROGRESS
            and visit.has_after_photo
        ):
            rounds.append(COMPLETE)
        if office and visit.status == MaintenanceVisit.Status.AWAITING_REVIEW:
            rounds.append(ACCEPT)
            rounds.append(UNRESOLVED)
        return rounds

    if ticket.status in TERMINAL:
        return []

    out: list[str] = []

    def may(action: str) -> bool:
        return ticket.status in TRANSITIONS[action]

    if office and may(ASSIGN):
        out.append(ASSIGN)
    if (
        mine and may(START) and visit is not None and visit.has_before_photo
        and not parts_not_yet_in_hand(visit)
    ):
        out.append(START)
    if (
        mine and may(COMPLETE) and visit is not None
        and visit.status == MaintenanceVisit.Status.IN_PROGRESS
        and visit.has_after_photo
    ):
        # The verdict is given in the act of completing, so it cannot also
        # be a condition of being allowed to complete.
        out.append(COMPLETE)
    # Accepting work the technician has just said is not fixed would be
    # the office overruling the only person who saw it. When the verdict
    # is "not fixed" the only way on is another visit.
    if office and may(ACCEPT) and visit is not None and visit.resolved is not False:
        out.append(ACCEPT)
    if office and may(UNRESOLVED):
        out.append(UNRESOLVED)
    if office and may(CANCEL):
        out.append(CANCEL)
    return out


def _require(action: str, job: MaintenanceSchedule) -> Ticket:
    ticket = _ticket_of(job)
    if ticket is None:
        raise ValidationError(
            {"detail": "This job was not raised from a ticket, so it has no corrective lifecycle."}
        )
    if ticket.status not in TRANSITIONS[action]:
        allowed = ", ".join(
            dict(Ticket.Status.choices).get(s, s) for s in TRANSITIONS[action]
        )
        raise ValidationError({"detail": (
            f"'{dict(Ticket.Status.choices).get(ticket.status, ticket.status)}' is not a "
            f"state to {action} from. That is allowed from: {allowed}."
        )})
    return ticket


def _office_only(user, what: str) -> None:
    if not is_office(user):
        raise PermissionDenied(f"Only the office can {what}.")


def _say(ticket: Ticket, user, content: str, old: str, new: str) -> None:
    """Everything that happens goes on the ticket's own trail."""
    TicketComment.objects.create(
        ticket=ticket, author=user, content=content,
        comment_type=TicketComment.CommentType.STATUS_CHANGE,
        old_status=old, new_status=new,
    )


def _land(ticket: Ticket, action: str, user, note: str) -> None:
    was = ticket.status
    ticket.status = LANDS_ON[action]
    fields = ["status", "updated_at"]
    if ticket.status == Ticket.Status.CLOSED:
        ticket.closed_at = timezone.now()
        fields.append("closed_at")
    elif ticket.status == Ticket.Status.CANCELLED:
        ticket.closed_at = timezone.now()
        fields.append("closed_at")
    ticket.save(update_fields=fields)
    _say(ticket, user, note, was, ticket.status)


# ── Making the moves ─────────────────────────────────────────────────────

@transaction.atomic
def assign_round(job: MaintenanceSchedule, *, user, technician, due_date=None):
    """Give a scheduled round to a technician. No ticket, same act."""
    _office_only(user, "assign a round")
    if technician is None:
        raise ValidationError({"technician": "Say who is going."})
    visit = job.open_visit()
    if visit is None:
        raise ValidationError({"detail": "This schedule has no round to plan."})
    if visit.status != MaintenanceVisit.Status.PLANNED:
        raise ValidationError({"detail": (
            f"This round is {visit.get_status_display().lower()}, so it cannot be reassigned."
        )})
    visit.assigned_to = technician
    fields = ["assigned_to", "updated_at"]
    if due_date is not None:
        visit.due_date = due_date
        fields.insert(1, "due_date")
    visit.save(update_fields=fields)
    job.assigned_to = technician
    job.save(update_fields=["assigned_to", "updated_at"])
    return visit


@transaction.atomic
def assign(job: MaintenanceSchedule, *, user, technician, due_date) -> MaintenanceVisit:
    """Give the work to somebody, with a date. This opens the next visit."""
    ticket = _require(ASSIGN, job)
    _office_only(user, "assign a technician")
    if technician is None:
        raise ValidationError({"technician": "Say who is going."})
    if due_date is None:
        raise ValidationError({"due_date": "Say when it is due."})

    visit = current_visit(job)
    # While nobody has set off, assigning again moves the same visit rather
    # than opening another: changing your mind is not a second trip.
    if visit is not None and visit.status == MaintenanceVisit.Status.PLANNED:
        visit.assigned_to = technician
        visit.due_date = due_date
        visit.save(update_fields=["assigned_to", "due_date", "updated_at"])
        made = False
    else:
        visit = MaintenanceVisit.objects.create(
            schedule=job, sequence=(visit.sequence + 1) if visit else 1,
            due_date=due_date, assigned_to=technician,
            status=MaintenanceVisit.Status.PLANNED,
        )
        made = True

    job.assigned_to = technician
    job.next_due = due_date
    job.status = MaintenanceSchedule.Status.PENDING
    job.save(update_fields=["assigned_to", "next_due", "status", "updated_at"])

    who = technician.get_full_name() or technician.username
    _land(ticket, ASSIGN, user, (
        f"Visit {visit.sequence} {'opened for' if made else 'reassigned to'} {who}, due {due_date:%d %b %Y}."
    ))
    return visit


@transaction.atomic
def start(visit: MaintenanceVisit, *, user, where=None) -> MaintenanceVisit:
    """On site, work begins — once there is a photograph of what was found."""
    job = visit.schedule
    ticket = _require(START, job)
    if not theirs(visit, user):
        raise PermissionDenied("Only the technician this visit was given to can start it.")
    if visit.status != MaintenanceVisit.Status.PLANNED:
        raise ValidationError({"detail": f"Visit {visit.sequence} has already been started."})
    if not visit.has_before_photo:
        raise ValidationError({"detail": (
            "Upload a BEFORE photo first — it is the record of what was found."
        )})
    _refuse_if_parts_are_missing(visit)

    visit.status = MaintenanceVisit.Status.IN_PROGRESS
    visit.started_at = timezone.now()
    lat, lng = (where or (None, None))
    visit.start_latitude, visit.start_longitude = lat, lng
    visit.save(update_fields=[
        "status", "started_at", "start_latitude", "start_longitude", "updated_at",
    ])
    job.status = MaintenanceSchedule.Status.IN_PROCESS
    job.save(update_fields=["status", "updated_at"])
    _land(ticket, START, user, f"Visit {visit.sequence} started on site.")
    return visit


@transaction.atomic
def parts_not_yet_in_hand(visit: MaintenanceVisit):
    """Lines this visit asked for that the store has not handed over.

    A technician who has said they need a part needs it before they go,
    not after: setting off without it means a second trip, and the visit
    photographs a job that could not be finished. A line nobody approved
    counts too — it is just as absent.

    Withdrawn and rejected lines are not waiting on anybody, so they do
    not hold the visit up.
    """
    from apps.inventory.models import IssuanceRequest

    from .models import MaintenancePartRequest

    waiting = []
    for line in visit.schedule.part_requests.select_related("issuance_request").all():
        if line.visit_id not in (None, visit.pk):
            continue
        if line.status == MaintenancePartRequest.Status.REQUESTED:
            waiting.append((line, "not approved yet"))
            continue
        if line.status != MaintenancePartRequest.Status.APPROVED:
            continue
        issue = line.issuance_request
        # Partly issued is still issued: the store has handed over what it
        # had, and a technician holding two of the three approved brackets
        # is not waiting on anybody — they are on their way.
        if issue is None or issue.status == IssuanceRequest.Status.PENDING:
            waiting.append((line, "not issued yet"))
    return waiting


def _refuse_if_parts_are_missing(visit: MaintenanceVisit) -> None:
    waiting = parts_not_yet_in_hand(visit)
    if waiting:
        raise ValidationError({"detail": (
            "The store has not handed these over yet: "
            + ", ".join(f"{line.what} ({why})" for line, why in waiting)
            + "."
        )})


def outstanding_parts(visit: MaintenanceVisit):
    """Lines the store issued for this visit that nobody has accounted for.

    A part leaves the shelf on a technician's say-so; it comes back on
    somebody's say-so too. Until the visit says how much was used, the
    difference is simply missing.
    """
    return [
        line for line in visit.schedule.part_requests.select_related("issuance_request")
        if line.quantity_used is None
        and line.issuance_request_id is not None
        and (line.issuance_request.quantity_issued or 0) > 0
        and (line.visit_id is None or line.visit_id == visit.pk)
    ]


def complete(
    visit: MaintenanceVisit, *, user, resolved: bool, remarks: str = "",
    settlement=None,
) -> MaintenanceVisit:
    """The technician is done and says whether it is fixed. The office decides."""
    job = visit.schedule
    ticket = _require(COMPLETE, job)
    if not theirs(visit, user):
        raise PermissionDenied("Only the technician on this visit can complete it.")
    if visit.status != MaintenanceVisit.Status.IN_PROGRESS:
        raise ValidationError({"detail": (
            f"Visit {visit.sequence} has not been started, so there is nothing to complete."
        )})
    if not visit.has_after_photo:
        raise ValidationError({"detail": "Upload an AFTER photo before completing the visit."})
    if resolved is None:
        raise ValidationError({"resolved": "Say whether the fault is fixed."})
    if not resolved and not (remarks or "").strip():
        raise ValidationError({"remarks": "Say what is still wrong."})

    # What the store handed over is squared up here, while the technician
    # still has it: what was used, and what is going back. Anything coming
    # back goes to receiving to be inspected, like any other delivery —
    # it is not put straight on the shelf.
    from .parts import settle

    owed = outstanding_parts(visit)
    if owed and not settlement:
        raise ValidationError({"parts_settlement": [
            "Say how much of each issued component was used: "
            + ", ".join(line.what for line in owed)
        ]})
    receipt = settle(
        visit.schedule, user=user, rows=settlement, visit=visit,
    ) if settlement else None

    visit.status = MaintenanceVisit.Status.AWAITING_REVIEW
    visit.completed_at = timezone.now()
    visit.resolved = resolved
    visit.remarks = remarks or ""
    visit.save(update_fields=[
        "status", "completed_at", "resolved", "remarks", "updated_at",
    ])
    verdict = "reports it fixed" if resolved else "reports it not fixed"
    _land(ticket, COMPLETE, user, (
        f"Visit {visit.sequence} completed — the technician {verdict}."
        + (f" {remarks.strip()}" if remarks and remarks.strip() else "")
    ))
    # The ticket carries the outcome in its own fields too: a queue that only
    # knows the status cannot say what was done or when.
    ticket.completed_at = visit.completed_at
    ticket.completion_notes = (remarks or "").strip() or (
        "Technician reports the fault fixed." if resolved
        else "Technician reports the fault is not fixed."
    )
    ticket.save(update_fields=["completed_at", "completion_notes", "updated_at"])
    # Not a field on the visit: the caller needs to tell the technician
    # where the leftovers went, and only this call knows.
    visit.return_grn = receipt.grn_number if receipt else None
    return visit


@transaction.atomic
def accept(
    visit: MaintenanceVisit, *, user, note: str = "", cost_lines=None, component_prices=None,
) -> MaintenanceVisit:
    """The office agrees it is fixed. The job is finished and so is the ticket."""
    from .services import return_to_service_if_done

    job = visit.schedule
    ticket = _require(ACCEPT, job)
    _office_only(user, "accept work as resolved")
    if visit.resolved is False:
        raise ValidationError({"detail": (
            f"Visit {visit.sequence} reports the fault is not fixed, so there is "
            "nothing to accept. Send it back for another visit."
        )})

    visit.status = MaintenanceVisit.Status.COMPLETED
    visit.review_decision = MaintenanceVisit.Review.ACCEPTED
    visit.reviewed_by = user
    visit.reviewed_at = timezone.now()
    visit.review_note = note or ""
    visit.save(update_fields=[
        "status", "review_decision", "reviewed_by", "reviewed_at",
        "review_note", "updated_at",
    ])

    job.status = MaintenanceSchedule.Status.COMPLETED
    job.is_active = False
    job.save(update_fields=["status", "is_active", "updated_at"])

    record = _file_record(job, visit, user)
    price_the_visit(record, visit, extra=cost_lines, prices=component_prices)
    return_to_service_if_done(record, user)
    _land(ticket, ACCEPT, user, (
        f"Visit {visit.sequence} accepted as resolved."
        + (f" {note.strip()}" if note and note.strip() else "")
    ))
    return visit


@transaction.atomic
def mark_unresolved(
    visit: MaintenanceVisit, *, user, reason: str, technician, next_due, note: str = "",
) -> MaintenanceVisit:
    """Not fixed. Another trip, on the same ticket, decided here and now.

    The reason, the technician and the date are one decision: a visit sent
    back without all three leaves a ticket nobody is going to attend.
    """
    job = visit.schedule
    ticket = _require(UNRESOLVED, job)
    _office_only(user, "mark work unresolved")

    reason = (reason or "").strip()
    if not reason:
        raise ValidationError({"reason": "Add remarks for the next visit."})
    # "Same as before" is a real answer, and the commonest one: the person
    # who has already seen it goes back. Naming nobody means them.
    technician = technician or visit.assigned_to
    if technician is None:
        raise ValidationError({"technician": "Say who is going next."})
    if next_due is None:
        raise ValidationError({"next_due": "Say when the next visit is due."})

    visit.status = MaintenanceVisit.Status.COMPLETED
    visit.review_decision = MaintenanceVisit.Review.UNRESOLVED
    visit.reviewed_by = user
    visit.reviewed_at = timezone.now()
    visit.review_reason = reason
    visit.review_note = note or ""
    visit.save(update_fields=[
        "status", "review_decision", "reviewed_by", "reviewed_at",
        "review_reason", "review_note", "updated_at",
    ])

    nxt = MaintenanceVisit.objects.create(
        schedule=job, sequence=visit.sequence + 1, due_date=next_due,
        assigned_to=technician, status=MaintenanceVisit.Status.PLANNED,
    )
    job.assigned_to = technician
    job.next_due = next_due
    job.status = MaintenanceSchedule.Status.PENDING
    job.save(update_fields=["assigned_to", "next_due", "status", "updated_at"])

    who = technician.get_full_name() or technician.username
    ticket.completed_at = None
    ticket.completion_notes = ""
    ticket.save(update_fields=["completed_at", "completion_notes", "updated_at"])
    _land(ticket, UNRESOLVED, user, (
        f"Visit {visit.sequence} marked unresolved: {reason}"
        + (f" {note.strip()}" if note and note.strip() else "")
        + f" Visit {nxt.sequence} opened for {who}, due {next_due:%d %b %Y}."
    ))
    return nxt


# ── Scheduled rounds ─────────────────────────────────────────────────────
#
# A round has no ticket, so it has no ticket state machine. Everything else
# is the same shape as a breakdown and is deliberately the same code path:
# the technician photographs it, squares up the store and hands it in; the
# office looks at the evidence and accepts it. Only then is the register
# written and the schedule rolled to its next cycle.


@transaction.atomic
def complete_round(
    visit: MaintenanceVisit, *, user, remarks: str = "", settlement=None,
) -> MaintenanceVisit:
    """The technician is done on site. The office still has to accept it."""
    from .parts import settle

    if not theirs(visit, user):
        raise PermissionDenied("Only the technician on this round can complete it.")
    if visit.status != MaintenanceVisit.Status.IN_PROGRESS:
        raise ValidationError({"detail": (
            f"This round is {visit.get_status_display().lower()}, so there is "
            "nothing to complete."
        )})
    if not visit.has_after_photo:
        raise ValidationError({"detail": "Upload an AFTER photo before completing the round."})

    owed = outstanding_parts(visit)
    if owed and not settlement:
        raise ValidationError({"parts_settlement": [
            "Say how much of each issued component was used: "
            + ", ".join(line.what for line in owed)
        ]})
    receipt = settle(
        visit.schedule, user=user, rows=settlement, visit=visit,
    ) if settlement else None

    visit.status = MaintenanceVisit.Status.AWAITING_REVIEW
    visit.completed_at = timezone.now()
    visit.resolved = True
    visit.remarks = remarks or ""
    visit.save(update_fields=[
        "status", "completed_at", "resolved", "remarks", "updated_at",
    ])
    visit.return_grn = receipt.grn_number if receipt else None
    return visit


@transaction.atomic
def accept_round(
    visit: MaintenanceVisit, *, user, note: str = "", cost_lines=None, component_prices=None,
):
    """The office accepts the round: the register is written, the cycle rolls."""
    _office_only(user, "accept a round")
    if visit.status != MaintenanceVisit.Status.AWAITING_REVIEW:
        raise ValidationError({"detail": (
            f"This round is {visit.get_status_display().lower()}, so there is "
            "nothing to review."
        )})

    job = visit.schedule
    visit.status = MaintenanceVisit.Status.COMPLETED
    visit.review_decision = MaintenanceVisit.Review.ACCEPTED
    visit.reviewed_by = user
    visit.reviewed_at = timezone.now()
    visit.review_note = note or ""
    visit.save(update_fields=[
        "status", "review_decision", "reviewed_by", "reviewed_at",
        "review_note", "updated_at",
    ])

    record = _file_record(job, visit, user)
    price_the_visit(record, visit, extra=cost_lines, prices=component_prices)
    # Writing the register is what rolls the schedule on: the next round
    # opens from here, not from the moment the technician walked away.
    job.advance_after_completion((visit.completed_at or timezone.now()).date())
    return visit


@transaction.atomic
def send_round_back(
    visit: MaintenanceVisit, *, user, reason: str, technician, next_due, note: str = "",
):
    """The office is not satisfied: another round, with a reason on the record.

    No register entry and no roll forward — the cycle has not been served
    until somebody accepts that it has.
    """
    _office_only(user, "send a round back")
    if visit.status != MaintenanceVisit.Status.AWAITING_REVIEW:
        raise ValidationError({"detail": (
            f"This round is {visit.get_status_display().lower()}, so there is "
            "nothing to review."
        )})
    reason = (reason or "").strip()
    if not reason:
        raise ValidationError({"reason": "Add remarks for the next visit."})
    # "Same as before" is a real answer, and the commonest one: the person
    # who has already seen it goes back. Naming nobody means them.
    technician = technician or visit.assigned_to
    if technician is None:
        raise ValidationError({"technician": "Say who is going next."})
    if next_due is None:
        raise ValidationError({"next_due": "Say when the next visit is due."})

    job = visit.schedule
    visit.status = MaintenanceVisit.Status.COMPLETED
    visit.review_decision = MaintenanceVisit.Review.UNRESOLVED
    visit.reviewed_by = user
    visit.reviewed_at = timezone.now()
    visit.review_reason = reason
    visit.review_note = note or ""
    visit.save(update_fields=[
        "status", "review_decision", "reviewed_by", "reviewed_at",
        "review_reason", "review_note", "updated_at",
    ])

    nxt = MaintenanceVisit.objects.create(
        schedule=job, sequence=visit.sequence + 1, due_date=next_due,
        assigned_to=technician, status=MaintenanceVisit.Status.PLANNED,
    )
    job.assigned_to = technician
    job.next_due = next_due
    job.status = MaintenanceSchedule.Status.PENDING
    job.save(update_fields=["assigned_to", "next_due", "status", "updated_at"])
    return nxt


@transaction.atomic
def cancel(job: MaintenanceSchedule, *, user, reason: str) -> Ticket:
    """Called off. The asset goes back into service and the job is closed."""
    from .services import release_asset_for_cancelled_ticket

    ticket = _require(CANCEL, job)
    _office_only(user, "cancel a ticket")
    if not (reason or "").strip():
        raise ValidationError({"reason": "Say why it is being cancelled."})

    job.visits.filter(
        status__in=(MaintenanceVisit.Status.PLANNED, MaintenanceVisit.Status.IN_PROGRESS)
    ).update(status=MaintenanceVisit.Status.SKIPPED)
    job.status = MaintenanceSchedule.Status.CANCELLED
    job.is_active = False
    job.save(update_fields=["status", "is_active", "updated_at"])

    _land(ticket, CANCEL, user, f"Cancelled: {reason.strip()}")
    release_asset_for_cancelled_ticket(ticket, user, reason)
    return ticket


def component_costs(visit: MaintenanceVisit, prices=None):
    """What the parts this visit used are worth, at the store's own prices.

    Priced from what was actually used, not what was issued: the rest went
    back to receiving and is somebody else's stock again.

    A part the store has no price on file for is still listed, at nothing,
    so the person accepting the work can see it and say what it cost.
    Dropping it silently made the total quietly wrong with no way to tell.
    ``prices`` maps a part line's id to a unit cost supplied on review.
    """
    said = {str(k): v for k, v in (prices or {}).items()}
    out = []
    for line in visit.schedule.part_requests.select_related("item", "unit_type").all():
        if line.visit_id != visit.pk or not line.quantity_used:
            continue
        unit = (
            getattr(line.item, "unit_cost", None) if line.item_id
            else getattr(line.unit_type, "unit_cost", None)
        )
        given = said.get(str(line.pk))
        if unit is None and given not in (None, ""):
            try:
                unit = Decimal(str(given))
            except InvalidOperation:
                raise ValidationError(
                    {"component_prices": [f"{line.what}: say what one costs."]}
                )
        if unit is not None and unit < 0:
            raise ValidationError({"component_prices": [f"{line.what}: a price is not negative."]})
        qty = Decimal(line.quantity_used)
        out.append({
            "part_request": str(line.pk),
            "description": line.what,
            "quantity": qty,
            "unit_cost": unit,
            "amount": (unit * qty).quantize(Decimal("0.01")) if unit is not None else Decimal("0.00"),
        })
    return out


def price_the_visit(record, visit: MaintenanceVisit, extra=None, prices=None):
    """Write the cost lines and total them onto the record."""
    from .models import MaintenanceCostLine

    record.cost_lines.all().delete()
    lines = [
        MaintenanceCostLine(
            record=record, source=MaintenanceCostLine.Source.COMPONENT,
            description=row["description"], quantity=row["quantity"],
            unit_cost=row["unit_cost"], amount=row["amount"],
        )
        for row in component_costs(visit, prices)
    ]
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
        lines.append(MaintenanceCostLine(
            record=record, source=MaintenanceCostLine.Source.MANUAL,
            description=what, amount=amount.quantize(Decimal("0.01")),
        ))
    MaintenanceCostLine.objects.bulk_create(lines)
    record.cost = sum((x.amount for x in lines), Decimal("0.00"))
    record.save(update_fields=["cost", "updated_at"])
    return record.cost


def _file_record(job: MaintenanceSchedule, visit: MaintenanceVisit, user):
    """The completion record the register keeps, written from the visit."""
    from apps.warranties.services import derive_billability

    from .models import MaintenanceRecord

    if visit.record_id:
        return visit.record
    billable, charge = False, ""
    if job.device_id:
        _, billable, charge = derive_billability(job.device)
    record = MaintenanceRecord.objects.create(
        schedule=job,
        performed_by=visit.assigned_to or user,
        performed_at=visit.completed_at or timezone.now(),
        status=MaintenanceRecord.Status.COMPLETED,
        notes=visit.remarks,
        is_billable=billable,
        charge_to=charge or "",
    )
    visit.record = record
    visit.save(update_fields=["record", "updated_at"])
    return record
