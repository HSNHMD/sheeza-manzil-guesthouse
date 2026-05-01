"""Maintenance / Work Orders V1 — service helpers.

Pure-ish helpers that the route handlers call. They validate input,
perform the DB writes, and emit ActivityLog rows. Each returns a
small Result dataclass so callers can render a flash message or a
JSON body without knowing the model details.

Design contract:
  - Allowed enum values live on the WorkOrder model (CATEGORIES,
    PRIORITIES, STATUSES) so form templates, tests, and the validator
    here all share one source of truth.
  - Status transitions are validated against an allow-list per source
    state (you can't go from `cancelled` back to `in_progress` via
    update_status; create a new work order instead).
  - This module never sends WhatsApp / email / Gemini calls; pure DB
    writes only.
  - ActivityLog metadata is always a small flat dict of scalars per
    the Audit module's contract — never embeds full descriptions,
    private notes, or message bodies.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Optional


# Allowed status transitions. Keys = current status, values = set of
# statuses you can move TO from there. `resolved` and `cancelled`
# are terminal in V1 (no resurrection — file a new ticket).
_ALLOWED_TRANSITIONS = {
    'new':         {'assigned', 'in_progress', 'waiting', 'resolved', 'cancelled'},
    'assigned':    {'in_progress', 'waiting', 'resolved', 'cancelled'},
    'in_progress': {'waiting', 'resolved', 'cancelled'},
    'waiting':     {'in_progress', 'resolved', 'cancelled'},
    'resolved':    set(),  # terminal
    'cancelled':   set(),  # terminal
}


@dataclass
class Result:
    ok:       bool
    message:  str
    work_order: Optional[object] = None
    extra:    dict = field(default_factory=dict)

    @classmethod
    def fail(cls, msg: str, **extra) -> 'Result':
        return cls(ok=False, message=msg, extra=extra)

    @classmethod
    def success(cls, msg: str, work_order=None, **extra) -> 'Result':
        return cls(ok=True, message=msg, work_order=work_order, extra=extra)


# ── Validation helpers ────────────────────────────────────────────

def _allowed_set(pairs):
    return {slug for slug, _label in pairs}


def normalize_category(value: Optional[str]) -> Optional[str]:
    from ..models import WorkOrder
    if not value:
        return None
    norm = str(value).strip().lower()
    return norm if norm in _allowed_set(WorkOrder.CATEGORIES) else None


def normalize_priority(value: Optional[str]) -> Optional[str]:
    from ..models import WorkOrder
    if not value:
        return None
    norm = str(value).strip().lower()
    return norm if norm in _allowed_set(WorkOrder.PRIORITIES) else None


def normalize_status(value: Optional[str]) -> Optional[str]:
    from ..models import WorkOrder
    if not value:
        return None
    norm = str(value).strip().lower()
    return norm if norm in _allowed_set(WorkOrder.STATUSES) else None


# ── Mutation services ─────────────────────────────────────────────

def create_work_order(*, title: str,
                      category: str = 'general',
                      priority: str = 'medium',
                      description: Optional[str] = None,
                      room_id: Optional[int] = None,
                      booking_id: Optional[int] = None,
                      assigned_to_user_id: Optional[int] = None,
                      reported_by_user_id: Optional[int] = None,
                      due_date=None,
                      actor_user_id: Optional[int] = None) -> Result:
    """Create a work order. Validates enums + room/booking refs.

    Logs `maintenance.created`. Returns Result(work_order=<row>).
    """
    from ..models import db, WorkOrder, Room, Booking
    from .audit import log_activity

    title = (title or '').strip()
    if not title:
        return Result.fail('Title is required.')
    if len(title) > 160:
        return Result.fail('Title is too long (160 chars max).')

    cat = normalize_category(category)
    if cat is None:
        return Result.fail(f'Invalid category {category!r}.')
    pri = normalize_priority(priority)
    if pri is None:
        return Result.fail(f'Invalid priority {priority!r}.')

    if room_id is not None:
        if Room.query.get(room_id) is None:
            return Result.fail('Room not found.')
    if booking_id is not None:
        if Booking.query.get(booking_id) is None:
            return Result.fail('Booking not found.')

    desc = (description or '').strip() or None
    if desc and len(desc) > 5000:
        desc = desc[:5000]

    wo = WorkOrder(
        title=title, description=desc,
        category=cat, priority=pri, status='new',
        room_id=room_id, booking_id=booking_id,
        assigned_to_user_id=assigned_to_user_id,
        reported_by_user_id=reported_by_user_id,
        due_date=due_date,
    )
    if assigned_to_user_id is not None:
        # Pre-assigned at creation time — bump status to assigned.
        wo.status = 'assigned'
    db.session.add(wo)
    db.session.flush()

    log_activity(
        'maintenance.created',
        actor_user_id=actor_user_id,
        description=(
            f'Work order #{wo.id} created: {wo.title!r}'
            + (f' (room {wo.room.number})' if wo.room else '')
            + f' · {pri} priority · {cat}.'
        ),
        metadata={
            'work_order_id':  wo.id,
            'room_id':        wo.room_id,
            'room_number':    wo.room.number if wo.room else None,
            'category':       wo.category,
            'priority':       wo.priority,
            'status':         wo.status,
            'assigned_to_user_id': wo.assigned_to_user_id,
        },
    )
    db.session.commit()
    return Result.success(f'Work order #{wo.id} created.', work_order=wo)


def assign(*, work_order_id: int, user_id: Optional[int],
           actor_user_id: Optional[int] = None) -> Result:
    """Assign (or unassign with user_id=None) a work order."""
    from ..models import db, WorkOrder, User
    from .audit import log_activity

    wo = WorkOrder.query.get(work_order_id)
    if wo is None:
        return Result.fail('Work order not found.')
    if wo.status in ('resolved', 'cancelled'):
        return Result.fail(
            f'Work order #{wo.id} is {wo.status} and cannot be reassigned.')

    if user_id is not None and User.query.get(user_id) is None:
        return Result.fail('Assignee not found.')

    old_assignee = wo.assigned_to_user_id
    old_status   = wo.status
    wo.assigned_to_user_id = user_id
    # If we're assigning a user and the order was still 'new', bump
    # to 'assigned' automatically. Other statuses stay where they are.
    if user_id is not None and wo.status == 'new':
        wo.status = 'assigned'

    log_activity(
        'maintenance.assigned',
        actor_user_id=actor_user_id,
        description=(
            f'Work order #{wo.id} '
            + (f'assigned to user #{user_id}'
               if user_id is not None else 'unassigned')
            + (f' (was user #{old_assignee})' if old_assignee else '')
            + '.'
        ),
        metadata={
            'work_order_id':       wo.id,
            'room_id':             wo.room_id,
            'room_number':         wo.room.number if wo.room else None,
            'old_assignee_id':     old_assignee,
            'assigned_to_user_id': user_id,
            'old_status':          old_status,
            'new_status':          wo.status,
        },
    )
    db.session.commit()
    return Result.success('Assignment updated.', work_order=wo)


def update_status(*, work_order_id: int, new_status: str,
                  actor_user_id: Optional[int] = None,
                  resolution_notes: Optional[str] = None) -> Result:
    """Move a work order to a new status. Validates the transition.

    `resolution_notes` is captured when the new status is `resolved`
    or `cancelled` (audit + display).
    """
    from ..models import db, WorkOrder
    from .audit import log_activity

    wo = WorkOrder.query.get(work_order_id)
    if wo is None:
        return Result.fail('Work order not found.')

    target = normalize_status(new_status)
    if target is None:
        return Result.fail(f'Invalid status {new_status!r}.')

    allowed = _ALLOWED_TRANSITIONS.get(wo.status, set())
    if target == wo.status:
        return Result.fail(f'Already {target}.')
    if target not in allowed:
        return Result.fail(
            f'Cannot move from {wo.status} to {target}. '
            f'Allowed next: {sorted(allowed)}.'
        )

    old_status = wo.status
    wo.status = target
    if target in ('resolved', 'cancelled'):
        wo.resolved_at = datetime.utcnow()
        notes = (resolution_notes or '').strip() or None
        if notes:
            wo.resolution_notes = notes[:1000]

    log_activity(
        'maintenance.resolved' if target == 'resolved'
        else 'maintenance.status_changed',
        actor_user_id=actor_user_id,
        description=(
            f'Work order #{wo.id} {old_status} → {target}'
            + (f' (room {wo.room.number})' if wo.room else '') + '.'
        ),
        metadata={
            'work_order_id':  wo.id,
            'room_id':        wo.room_id,
            'room_number':    wo.room.number if wo.room else None,
            'old_status':     old_status,
            'new_status':     target,
            'priority':       wo.priority,
            'category':       wo.category,
        },
    )
    db.session.commit()
    return Result.success(
        f'Work order #{wo.id} → {target}.', work_order=wo,
        old_status=old_status, new_status=target,
    )


def mark_room_out_of_order(*, work_order_id: int,
                           actor_user_id: Optional[int] = None) -> Result:
    """Flip the linked room to OOO + status='maintenance'.

    No-op if the work order has no room_id. Doesn't create a
    date-ranged RoomBlock — that's a separate explicit action via
    the existing /board/rooms/<id>/blocks endpoint. This action just
    flips the indefinite room-state flags.

    Logs `maintenance.room_out_of_order`. The Reservation Board's
    conflict checks already factor in `housekeeping_status='out_of_order'`
    on top of RoomBlocks.
    """
    from ..models import db, WorkOrder
    from .audit import log_activity

    wo = WorkOrder.query.get(work_order_id)
    if wo is None:
        return Result.fail('Work order not found.')
    if wo.room is None:
        return Result.fail('Work order has no linked room.')

    room = wo.room
    old_status = room.status
    old_hk     = room.housekeeping_status
    room.status              = 'maintenance'
    room.housekeeping_status = 'out_of_order'

    log_activity(
        'maintenance.room_out_of_order',
        actor_user_id=actor_user_id,
        description=(
            f'Room #{room.number} marked out-of-order via work order '
            f'#{wo.id} ({wo.priority} priority, {wo.category}).'
        ),
        metadata={
            'work_order_id':       wo.id,
            'room_id':             room.id,
            'room_number':         room.number,
            'category':            wo.category,
            'priority':            wo.priority,
            'old_room_status':     old_status,
            'new_room_status':     room.status,
            'old_housekeeping':    old_hk,
            'new_housekeeping':    room.housekeeping_status,
        },
    )
    db.session.commit()
    return Result.success(
        f'Room {room.number} marked out-of-order.',
        work_order=wo,
    )


# ── Read-side helpers (for the list view) ─────────────────────────

def list_work_orders(*, open_only: bool = False,
                     priority: Optional[str] = None,
                     status: Optional[str] = None,
                     room_id: Optional[int] = None,
                     assigned_to_user_id: Optional[int] = None,
                     limit: int = 200) -> list:
    """Return WorkOrder rows ordered by priority + created_at desc.

    Used by the /maintenance list view. Filters are AND-combined.
    """
    from ..models import WorkOrder
    q = WorkOrder.query
    if open_only:
        q = q.filter(WorkOrder.status.notin_(('resolved', 'cancelled')))
    if priority is not None:
        norm = normalize_priority(priority)
        if norm:
            q = q.filter(WorkOrder.priority == norm)
    if status is not None:
        norm = normalize_status(status)
        if norm:
            q = q.filter(WorkOrder.status == norm)
    if room_id is not None:
        q = q.filter(WorkOrder.room_id == room_id)
    if assigned_to_user_id is not None:
        q = q.filter(WorkOrder.assigned_to_user_id == assigned_to_user_id)
    return q.order_by(WorkOrder.created_at.desc()).limit(limit).all()


def open_count_by_room(room_ids=None) -> dict:
    """Return {room_id: count} of OPEN work orders for given rooms.

    Used by the Reservation Board / Housekeeping room-rail badge so
    the operator sees a small "wrench" indicator on rooms that have
    open issues without leaving the board.
    """
    from ..models import WorkOrder
    q = (WorkOrder.query
         .filter(WorkOrder.room_id.isnot(None),
                 WorkOrder.status.notin_(('resolved', 'cancelled'))))
    if room_ids:
        q = q.filter(WorkOrder.room_id.in_(room_ids))
    out = {}
    for wo in q.all():
        out[wo.room_id] = out.get(wo.room_id, 0) + 1
    return out


def summary_counts() -> dict:
    """KPI tile counts for the list-page header."""
    from ..models import WorkOrder
    open_q = WorkOrder.query.filter(
        WorkOrder.status.notin_(('resolved', 'cancelled')))
    return {
        'total':       WorkOrder.query.count(),
        'open':        open_q.count(),
        'urgent':      open_q.filter(WorkOrder.priority == 'urgent').count(),
        'in_progress': open_q.filter(WorkOrder.status == 'in_progress').count(),
        'waiting':     open_q.filter(WorkOrder.status == 'waiting').count(),
    }
