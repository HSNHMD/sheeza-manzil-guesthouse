"""Group Bookings / Master Folios V1 — service layer.

Pure functions that the routes call. Each helper writes ActivityLog
rows on success and never silently moves money between folios.

Hard contract:

  - V1 is ADDITIVE. Standalone bookings (booking_group_id IS NULL)
    behave exactly as they did before. The new columns default to
    a no-op state.

  - Each FolioItem has exactly ONE booking_id. We never copy or
    duplicate folio rows. The "master folio" is just the folio of
    the booking the operator designates as the group's billing
    account (BookingGroup.master_booking_id). Operators choose the
    target booking explicitly when posting an ad-hoc charge —
    services.folio.add_folio_item() and the existing routes are
    unchanged.

  - `Booking.billing_target` is purely advisory in V1. It tells the
    UI which folio to default to when the operator clicks "Add
    charge" — it does NOT auto-route existing rows. Reports compute
    per-booking outstanding the same way they always have, and the
    group summary aggregates without copying.

  - DEFERRED for V1 (documented but not built):
      * Auto-rollup of room revenue (Booking.total_amount) into the
        master booking. V1 leaves room revenue tied to its booking.
      * Mass charge re-routing across an existing group.
      * Group invoicing (one invoice for many bookings).

  - ActivityLog actions:
        group.created
        group.updated
        group.cancelled
        group.completed
        group.booking_added
        group.booking_removed
        group.master_folio_updated   (used for both setting the
                                       master_booking_id and changing
                                       a member's billing_target)
"""

from __future__ import annotations

import re
from datetime import datetime
from typing import Optional


VALID_GROUP_STATUSES = ('active', 'cancelled', 'completed')
VALID_BILLING_MODES  = ('individual', 'master', 'mixed')
VALID_BILLING_TARGETS = ('individual', 'master')

_CODE_RE = re.compile(r'^[A-Z0-9][A-Z0-9_-]{0,38}[A-Z0-9]$')


# ── Validation ──────────────────────────────────────────────────────

def normalize_group_code(raw) -> Optional[str]:
    if not raw:
        return None
    code = str(raw).strip().upper().replace(' ', '-')
    if not _CODE_RE.match(code):
        return None
    return code


def validate_group_input(*, group_code, group_name,
                          billing_mode='individual') -> dict:
    errors = []
    code = normalize_group_code(group_code)
    if code is None:
        errors.append('group_code: 2–40 chars, letters/digits/dash/underscore.')
    if not group_name or not str(group_name).strip():
        errors.append('group_name: required.')
    if len(str(group_name or '').strip()) > 160:
        errors.append('group_name: max 160 chars.')
    if billing_mode not in VALID_BILLING_MODES:
        errors.append(
            f'billing_mode must be one of '
            f'{", ".join(VALID_BILLING_MODES)}.')
    return {'errors': errors,
            'cleaned': {
                'group_code':   code,
                'group_name':   str(group_name or '').strip()[:160],
                'billing_mode': billing_mode,
            }}


# ── Create / lifecycle ──────────────────────────────────────────────

def create_group(*, group_code, group_name,
                  billing_mode='individual',
                  primary_contact_guest_id=None,
                  notes=None,
                  user=None) -> dict:
    """Create a new BookingGroup. Caller commits."""
    from ..models import db, BookingGroup, Guest
    from .audit import log_activity

    v = validate_group_input(group_code=group_code,
                               group_name=group_name,
                               billing_mode=billing_mode)
    if v['errors']:
        return {'ok': False, 'error': '; '.join(v['errors']), 'group': None}
    cleaned = v['cleaned']

    if BookingGroup.query.filter_by(group_code=cleaned['group_code']).first():
        return {'ok': False,
                'error': f'group_code {cleaned["group_code"]!r} already exists.',
                'group': None}

    contact_id = None
    if primary_contact_guest_id:
        try:
            contact_id = int(primary_contact_guest_id)
        except (TypeError, ValueError):
            return {'ok': False, 'error': 'invalid primary_contact_guest_id.',
                    'group': None}
        if Guest.query.get(contact_id) is None:
            return {'ok': False, 'error': 'contact guest not found.',
                    'group': None}

    group = BookingGroup(
        group_code=cleaned['group_code'],
        group_name=cleaned['group_name'],
        billing_mode=cleaned['billing_mode'],
        primary_contact_guest_id=contact_id,
        notes=(str(notes).strip()[:2000] if notes else None),
        status='active',
    )
    db.session.add(group)
    db.session.flush()

    log_activity(
        'group.created',
        actor_user_id=getattr(user, 'id', None),
        description=(
            f'Booking group "{group.group_name}" '
            f'(code: {group.group_code}) created.'
        ),
        metadata={
            'group_id':     group.id,
            'group_code':   group.group_code,
            'group_name':   group.group_name,
            'billing_mode': group.billing_mode,
        },
    )

    return {'ok': True, 'error': None, 'group': group}


def update_group_meta(group, *, group_name=None, billing_mode=None,
                       notes=None, primary_contact_guest_id=None,
                       user=None) -> dict:
    """Edit group fields without changing membership / master booking."""
    from ..models import db, Guest
    from .audit import log_activity

    if group_name is not None:
        gn = str(group_name).strip()
        if not gn:
            return {'ok': False, 'error': 'group_name cannot be empty.'}
        if len(gn) > 160:
            return {'ok': False, 'error': 'group_name max 160 chars.'}
        group.group_name = gn
    if billing_mode is not None:
        if billing_mode not in VALID_BILLING_MODES:
            return {'ok': False, 'error': f'invalid billing_mode {billing_mode!r}.'}
        group.billing_mode = billing_mode
    if notes is not None:
        group.notes = str(notes).strip()[:2000] or None
    if primary_contact_guest_id is not None:
        if primary_contact_guest_id in ('', '0', 0):
            group.primary_contact_guest_id = None
        else:
            try:
                gid = int(primary_contact_guest_id)
            except (TypeError, ValueError):
                return {'ok': False, 'error': 'invalid contact id.'}
            if Guest.query.get(gid) is None:
                return {'ok': False, 'error': 'contact guest not found.'}
            group.primary_contact_guest_id = gid

    log_activity(
        'group.updated',
        actor_user_id=getattr(user, 'id', None),
        description=f'Group {group.group_code} updated.',
        metadata={
            'group_id':     group.id,
            'group_code':   group.group_code,
            'group_name':   group.group_name,
            'billing_mode': group.billing_mode,
        },
    )
    return {'ok': True, 'error': None}


def set_status(group, new_status: str, *, user=None) -> dict:
    """Set group status. Refuses invalid transitions."""
    from .audit import log_activity

    if new_status not in VALID_GROUP_STATUSES:
        return {'ok': False, 'error': f'unknown status {new_status!r}.'}
    if group.status == new_status:
        return {'ok': True, 'error': None}

    # Terminal states are sticky — once cancelled or completed, they
    # stay that way until manually reactivated. Reactivation IS allowed
    # in V1 because operators occasionally undo accidental closures;
    # we just write an audit row so it's traceable.
    old = group.status
    group.status = new_status

    action = ('group.cancelled' if new_status == 'cancelled'
              else 'group.completed' if new_status == 'completed'
              else 'group.updated')
    log_activity(
        action,
        actor_user_id=getattr(user, 'id', None),
        description=f'Group {group.group_code}: {old} → {new_status}.',
        metadata={
            'group_id':   group.id,
            'group_code': group.group_code,
            'old_status': old,
            'new_status': new_status,
        },
    )
    return {'ok': True, 'error': None}


# ── Membership ──────────────────────────────────────────────────────

def attach_booking(group, booking, *,
                    billing_target: str = 'individual',
                    user=None) -> dict:
    """Attach a booking to a group. Refuses if booking is already in
    a different group."""
    from .audit import log_activity

    if booking.booking_group_id is not None and \
            booking.booking_group_id != group.id:
        return {'ok': False,
                'error': (f'booking {booking.booking_ref} is already in '
                          f'a different group; remove it first.')}
    if billing_target not in VALID_BILLING_TARGETS:
        return {'ok': False,
                'error': f'invalid billing_target {billing_target!r}.'}

    if booking.booking_group_id == group.id and \
            booking.billing_target == billing_target:
        return {'ok': True, 'error': None, 'no_op': True}

    booking.booking_group_id = group.id
    booking.billing_target = billing_target

    log_activity(
        'group.booking_added',
        actor_user_id=getattr(user, 'id', None),
        booking=booking,
        description=(
            f'Booking {booking.booking_ref} added to group '
            f'{group.group_code} (target: {billing_target}).'
        ),
        metadata={
            'group_id':       group.id,
            'group_code':     group.group_code,
            'group_name':     group.group_name,
            'booking_id':     booking.id,
            'booking_ref':    booking.booking_ref,
            'billing_target': billing_target,
        },
    )
    return {'ok': True, 'error': None, 'no_op': False}


def detach_booking(group, booking, *, user=None) -> dict:
    """Remove a booking from its group. Refuses if booking is the
    master billing account — staff must reassign master first."""
    from .audit import log_activity

    if booking.booking_group_id != group.id:
        return {'ok': False,
                'error': f'booking {booking.booking_ref} is not in '
                          f'group {group.group_code}.'}
    if group.master_booking_id == booking.id:
        return {'ok': False,
                'error': (f'booking {booking.booking_ref} is the master '
                          f'billing account; reassign master first.')}

    booking.booking_group_id = None
    booking.billing_target = 'individual'

    log_activity(
        'group.booking_removed',
        actor_user_id=getattr(user, 'id', None),
        booking=booking,
        description=(
            f'Booking {booking.booking_ref} removed from group '
            f'{group.group_code}.'
        ),
        metadata={
            'group_id':    group.id,
            'group_code':  group.group_code,
            'booking_id':  booking.id,
            'booking_ref': booking.booking_ref,
        },
    )
    return {'ok': True, 'error': None}


def set_master_booking(group, booking_or_none, *, user=None) -> dict:
    """Designate a member booking as the group's billing account.
    Pass None to clear master billing.

    Refuses if the chosen booking is not a member of this group.
    """
    from .audit import log_activity

    new_master_id = None
    if booking_or_none is not None:
        if booking_or_none.booking_group_id != group.id:
            return {'ok': False,
                    'error': 'master booking must be a member of the group.'}
        new_master_id = booking_or_none.id

    old_master_id = group.master_booking_id
    if old_master_id == new_master_id:
        return {'ok': True, 'error': None, 'no_op': True}
    group.master_booking_id = new_master_id

    log_activity(
        'group.master_folio_updated',
        actor_user_id=getattr(user, 'id', None),
        description=(
            f'Group {group.group_code} master booking '
            f'{"set to " + booking_or_none.booking_ref if booking_or_none else "cleared"}.'
        ),
        metadata={
            'group_id':            group.id,
            'group_code':          group.group_code,
            'old_master_booking_id': old_master_id,
            'new_master_booking_id': new_master_id,
            'master_booking_ref':  booking_or_none.booking_ref if booking_or_none else None,
        },
    )
    return {'ok': True, 'error': None}


def set_billing_target(booking, target: str, *, user=None) -> dict:
    """Flip a member booking's billing_target. Member-only — refuses
    bookings that aren't in any group (target only matters in a
    group context)."""
    from .audit import log_activity

    if booking.booking_group_id is None:
        return {'ok': False,
                'error': 'billing_target only applies inside a group.'}
    if target not in VALID_BILLING_TARGETS:
        return {'ok': False, 'error': f'invalid billing_target {target!r}.'}
    if booking.billing_target == target:
        return {'ok': True, 'error': None, 'no_op': True}

    old = booking.billing_target
    booking.billing_target = target

    log_activity(
        'group.master_folio_updated',
        actor_user_id=getattr(user, 'id', None),
        booking=booking,
        description=(
            f'Booking {booking.booking_ref} billing target: '
            f'{old} → {target}.'
        ),
        metadata={
            'group_id':       booking.booking_group_id,
            'booking_id':     booking.id,
            'booking_ref':    booking.booking_ref,
            'old_billing_target': old,
            'new_billing_target': target,
        },
    )
    return {'ok': True, 'error': None}


# ── Group summary (aggregation, no row mutation) ────────────────────

def group_summary(group) -> dict:
    """Return totals + per-member rows for the group view.

    Pure read aggregation — no row mutation. Anti-double-count:
    every FolioItem belongs to exactly one booking, so summing across
    members never double-counts.
    """
    from .folio import folio_balance

    members = list(group.bookings.order_by('check_in_date').all())
    rows = []
    sum_room_revenue = 0.0
    sum_outstanding  = 0.0
    sum_individual_balance = 0.0
    sum_master_balance     = 0.0

    for b in members:
        bal = folio_balance(b)
        sum_room_revenue += float(b.total_amount or 0.0)
        sum_outstanding  += bal if bal > 0 else 0.0
        if b.billing_target == 'master':
            sum_master_balance += bal
        else:
            sum_individual_balance += bal
        rows.append({
            'booking':       b,
            'folio_balance': round(bal, 2),
            'is_master':     (group.master_booking_id == b.id),
        })

    earliest_in = min(
        (b.check_in_date for b in members), default=None)
    latest_out  = max(
        (b.check_out_date for b in members), default=None)

    return {
        'group':            group,
        'rows':             rows,
        'member_count':     len(members),
        'earliest_check_in': earliest_in,
        'latest_check_out':  latest_out,
        'sum_room_revenue': round(sum_room_revenue, 2),
        'sum_outstanding':  round(sum_outstanding, 2),
        'sum_individual_balance': round(sum_individual_balance, 2),
        'sum_master_balance':     round(sum_master_balance, 2),
        'master_booking':   group.master_booking,
    }
