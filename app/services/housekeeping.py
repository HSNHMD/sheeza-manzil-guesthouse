"""Housekeeping V1 — state transitions, assignment, audit trail.

Pure-function helpers that the routes call to mutate the housekeeping
state of a Room, assign a cleaning task, and stamp who-did-what.

Hard rules:

  - Operational `Room.status` and `Room.housekeeping_status` are
    DISTINCT. The housekeeping service NEVER touches `Room.status`
    directly. Front Office controls operational status (occupied /
    maintenance / cleaning); housekeeping controls cleanliness state.

  - Vocabulary (V1, frozen):
        clean        — ready for arrival
        dirty        — needs cleaning
        in_progress  — cleaning in progress
        inspected    — passed inspection (post-clean QA)
        out_of_order — un-rentable (broken, deep clean, etc.)

  - Every status change writes BOTH a HousekeepingLog row (legacy
    table; kept for backwards compat with existing staff dashboards)
    AND an ActivityLog row via app.services.audit.log_activity().

  - ActivityLog metadata is a STRICT whitelist — only:
        room_id, room_number, old_status, new_status, assigned_user_id

  - No WhatsApp / email / Gemini side effects. Period.
  - No auto-room-assignment heuristics. The operator picks a user
    from a dropdown.

Designed to be called from app/routes/housekeeping.py and from
app/routes/staff.py — both can use the same canonical helpers.
"""

from __future__ import annotations

from datetime import datetime
from typing import Optional


# ── Canonical vocabulary (V1) ───────────────────────────────────────

HK_STATUSES = (
    'clean',
    'dirty',
    'in_progress',
    'inspected',
    'out_of_order',
)

HK_STATUS_LABELS = {
    'clean':        'Clean',
    'dirty':        'Dirty',
    'in_progress':  'In progress',
    'inspected':    'Inspected',
    'out_of_order': 'Out of order',
}


def is_valid_status(status: str) -> bool:
    return status in HK_STATUSES


# ── State change ────────────────────────────────────────────────────

def set_room_status(
    room,
    new_status: str,
    *,
    user,
    notes: Optional[str] = None,
) -> dict:
    """Set Room.housekeeping_status to new_status, write audit rows.

    Returns a dict with:
        ok:         bool
        error:      str | None
        old_status: str
        new_status: str

    Caller is responsible for committing the session.
    """
    from ..models import db, HousekeepingLog
    from .audit import log_activity

    if not is_valid_status(new_status):
        return {
            'ok': False,
            'error': (
                f'invalid housekeeping status: {new_status!r}. '
                f'must be one of {", ".join(HK_STATUSES)}.'
            ),
            'old_status': room.housekeeping_status,
            'new_status': new_status,
        }

    old_status = room.housekeeping_status

    # Even when old == new, we still write an ActivityLog row — operators
    # sometimes "re-confirm clean" deliberately. But we DON'T write a
    # noisy HousekeepingLog row in that case to keep the legacy log clean.
    if old_status != new_status:
        room.housekeeping_status = new_status
        room.housekeeping_updated_at = datetime.utcnow()
        room.housekeeping_updated_by_user_id = (
            getattr(user, 'id', None) if user is not None else None)

        # Legacy log row — preserve the existing action vocabulary so
        # existing staff dashboards keep rendering.
        legacy_action = _legacy_action_for(new_status)
        if legacy_action:
            db.session.add(HousekeepingLog(
                room_id=room.id,
                staff_id=getattr(user, 'id', None) if user else None,
                action=legacy_action,
                notes=(notes or None),
            ))

    log_activity(
        'housekeeping.status_changed',
        actor_user_id=getattr(user, 'id', None) if user else None,
        description=(
            f'Room {room.number}: housekeeping {old_status} → {new_status}.'
        ),
        metadata={
            'room_id':      room.id,
            'room_number':  room.number,
            'old_status':   old_status,
            'new_status':   new_status,
        },
    )

    if new_status == 'inspected':
        log_activity(
            'housekeeping.room_inspected',
            actor_user_id=getattr(user, 'id', None) if user else None,
            description=f'Room {room.number} passed inspection.',
            metadata={
                'room_id':     room.id,
                'room_number': room.number,
                'old_status':  old_status,
                'new_status':  new_status,
            },
        )

    return {
        'ok': True,
        'error': None,
        'old_status': old_status,
        'new_status': new_status,
    }


def _legacy_action_for(new_status: str) -> Optional[str]:
    """Map V1 vocabulary → legacy HousekeepingLog.action values.

    Existing dashboards filter on these strings, so don't break them.
    Returns None for transitions we don't want to legacy-log
    (currently: out_of_order has no legacy equivalent).
    """
    return {
        'clean':        'completed',
        'dirty':        'started_cleaning',  # close-enough legacy bucket
        'in_progress':  'started_cleaning',
        'inspected':    'inspected',
        'out_of_order': None,
    }.get(new_status)


# ── Task assignment ─────────────────────────────────────────────────

def assign_task(
    room,
    *,
    assignee,
    by_user,
    notes: Optional[str] = None,
) -> dict:
    """Assign `assignee` (a User or None to clear) to clean `room`.

    Writes an ActivityLog row (housekeeping.task_assigned) regardless
    of whether the assignment is new, changed, or cleared. Caller
    commits the session.
    """
    from .audit import log_activity

    old_assignee_id = room.assigned_to_user_id
    new_assignee_id = getattr(assignee, 'id', None) if assignee else None

    room.assigned_to_user_id = new_assignee_id
    room.assigned_at = datetime.utcnow() if new_assignee_id else None

    description = (
        f'Room {room.number} assigned to {assignee.username}.'
        if assignee else
        f'Room {room.number} assignment cleared.'
    )
    log_activity(
        'housekeeping.task_assigned',
        actor_user_id=getattr(by_user, 'id', None) if by_user else None,
        description=description,
        metadata={
            'room_id':           room.id,
            'room_number':       room.number,
            'assigned_user_id':  new_assignee_id,
        },
    )

    return {
        'ok':           True,
        'old_assignee': old_assignee_id,
        'new_assignee': new_assignee_id,
    }


# ── Convenience: room rail badge data ───────────────────────────────

def hk_badge(status: str) -> dict:
    """Return dict {label, css_class, dot_class} for templates."""
    css = {
        'clean':        ('Clean',        'hk-clean',        'hk-dot-clean'),
        'dirty':        ('Dirty',        'hk-dirty',        'hk-dot-dirty'),
        'in_progress':  ('Cleaning',     'hk-progress',     'hk-dot-progress'),
        'inspected':    ('Inspected',    'hk-inspected',    'hk-dot-inspected'),
        'out_of_order': ('Out of order', 'hk-ooo',          'hk-dot-ooo'),
    }
    label, klass, dot = css.get(status or 'clean', css['clean'])
    return {'label': label, 'css_class': klass, 'dot_class': dot}
