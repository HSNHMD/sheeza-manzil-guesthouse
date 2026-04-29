"""Group Bookings / Master Folios V1 — admin routes.

Endpoints (admin_required everywhere):

  GET  /groups/                       list all groups
  GET  /groups/new                    new-group form
  POST /groups/new                    create
  GET  /groups/<id>                   group summary view
  GET  /groups/<id>/edit              edit form
  POST /groups/<id>/edit              save metadata
  POST /groups/<id>/cancel            mark cancelled
  POST /groups/<id>/complete          mark completed
  POST /groups/<id>/reactivate        return to active
  POST /groups/<id>/add-booking       attach booking
  POST /groups/<id>/remove-booking    detach booking
  POST /groups/<id>/set-master        designate master billing booking
  POST /groups/<id>/set-billing-target flip member's billing_target

The route handlers are thin — every mutation goes through
services.groups so audit rows are written consistently.
"""

from __future__ import annotations

from datetime import date

from flask import (
    Blueprint, render_template, request, redirect, url_for, flash, abort,
)
from flask_login import login_required, current_user

from ..models import db, Booking, BookingGroup, Guest, Room
from ..decorators import admin_required
from ..services.groups import (
    create_group, update_group_meta, set_status,
    attach_booking, detach_booking,
    set_master_booking, set_billing_target,
    group_summary,
    VALID_BILLING_MODES, VALID_BILLING_TARGETS,
)


groups_bp = Blueprint('groups', __name__, url_prefix='/groups')


def _form_int(name, default=None):
    raw = (request.form.get(name) or '').strip()
    if not raw:
        return default
    try:
        return int(raw)
    except ValueError:
        return default


# ── List ────────────────────────────────────────────────────────────

@groups_bp.route('/', methods=['GET'])
@login_required
@admin_required
def index():
    status_filter = (request.args.get('status') or '').strip().lower()
    q = BookingGroup.query.order_by(BookingGroup.created_at.desc())
    if status_filter in ('active', 'cancelled', 'completed'):
        q = q.filter(BookingGroup.status == status_filter)
    groups = q.limit(200).all()

    counts = {
        s: BookingGroup.query.filter_by(status=s).count()
        for s in ('active', 'cancelled', 'completed')
    }
    counts['total'] = sum(counts.values())

    return render_template(
        'groups/list.html',
        groups=groups,
        counts=counts,
        status_filter=status_filter or None,
    )


# ── New ─────────────────────────────────────────────────────────────

@groups_bp.route('/new', methods=['GET', 'POST'])
@login_required
@admin_required
def new():
    contacts = (Guest.query
                .order_by(Guest.last_name, Guest.first_name)
                .limit(200)
                .all())
    if request.method == 'POST':
        result = create_group(
            group_code=request.form.get('group_code'),
            group_name=request.form.get('group_name'),
            billing_mode=request.form.get('billing_mode') or 'individual',
            primary_contact_guest_id=request.form.get('primary_contact_guest_id') or None,
            notes=request.form.get('notes'),
            user=current_user,
        )
        if not result['ok']:
            flash('Group: ' + result['error'], 'error')
            return render_template('groups/form.html',
                                    group=None, form=request.form,
                                    contacts=contacts,
                                    billing_modes=VALID_BILLING_MODES), 400
        db.session.commit()
        flash(f'Group "{result["group"].group_name}" created.', 'success')
        return redirect(url_for('groups.detail',
                                  group_id=result['group'].id))
    return render_template('groups/form.html',
                            group=None, form={},
                            contacts=contacts,
                            billing_modes=VALID_BILLING_MODES)


# ── Detail ─────────────────────────────────────────────────────────

@groups_bp.route('/<int:group_id>', methods=['GET'])
@login_required
@admin_required
def detail(group_id):
    group = BookingGroup.query.get_or_404(group_id)
    summary = group_summary(group)

    # Bookings the operator may attach: any non-cancelled booking
    # not already in another group.
    today = date.today()
    candidates = (
        Booking.query
        .filter((Booking.booking_group_id.is_(None))
                | (Booking.booking_group_id == group.id))
        .filter(Booking.status.notin_(
            ('cancelled', 'rejected', 'cancelled_by_guest')))
        .filter(Booking.check_out_date >= today - __import__('datetime').timedelta(days=30))
        .order_by(Booking.check_in_date.desc())
        .limit(120)
        .all()
    )
    candidates = [b for b in candidates
                  if b.booking_group_id != group.id]

    return render_template(
        'groups/detail.html',
        group=group,
        summary=summary,
        candidates=candidates,
        billing_targets=VALID_BILLING_TARGETS,
    )


# ── Edit ────────────────────────────────────────────────────────────

@groups_bp.route('/<int:group_id>/edit', methods=['GET', 'POST'])
@login_required
@admin_required
def edit(group_id):
    group = BookingGroup.query.get_or_404(group_id)
    contacts = (Guest.query
                .order_by(Guest.last_name, Guest.first_name)
                .limit(200)
                .all())
    if request.method == 'POST':
        result = update_group_meta(
            group,
            group_name=request.form.get('group_name'),
            billing_mode=request.form.get('billing_mode'),
            notes=request.form.get('notes'),
            primary_contact_guest_id=request.form.get(
                'primary_contact_guest_id'),
            user=current_user,
        )
        if not result['ok']:
            flash('Group: ' + result['error'], 'error')
            return redirect(url_for('groups.edit', group_id=group.id))
        db.session.commit()
        flash(f'Group "{group.group_name}" saved.', 'success')
        return redirect(url_for('groups.detail', group_id=group.id))
    return render_template('groups/form.html',
                            group=group, form={},
                            contacts=contacts,
                            billing_modes=VALID_BILLING_MODES)


# ── Status transitions ──────────────────────────────────────────────

@groups_bp.route('/<int:group_id>/cancel', methods=['POST'])
@login_required
@admin_required
def cancel(group_id):
    group = BookingGroup.query.get_or_404(group_id)
    r = set_status(group, 'cancelled', user=current_user)
    if not r['ok']:
        flash(r['error'], 'error')
    else:
        db.session.commit()
        flash(f'Group {group.group_code} cancelled.', 'success')
    return redirect(url_for('groups.detail', group_id=group.id))


@groups_bp.route('/<int:group_id>/complete', methods=['POST'])
@login_required
@admin_required
def complete(group_id):
    group = BookingGroup.query.get_or_404(group_id)
    r = set_status(group, 'completed', user=current_user)
    if not r['ok']:
        flash(r['error'], 'error')
    else:
        db.session.commit()
        flash(f'Group {group.group_code} marked completed.', 'success')
    return redirect(url_for('groups.detail', group_id=group.id))


@groups_bp.route('/<int:group_id>/reactivate', methods=['POST'])
@login_required
@admin_required
def reactivate(group_id):
    group = BookingGroup.query.get_or_404(group_id)
    r = set_status(group, 'active', user=current_user)
    if not r['ok']:
        flash(r['error'], 'error')
    else:
        db.session.commit()
        flash(f'Group {group.group_code} reactivated.', 'success')
    return redirect(url_for('groups.detail', group_id=group.id))


# ── Membership ──────────────────────────────────────────────────────

@groups_bp.route('/<int:group_id>/add-booking', methods=['POST'])
@login_required
@admin_required
def add_booking(group_id):
    group = BookingGroup.query.get_or_404(group_id)
    booking_id = _form_int('booking_id')
    target = (request.form.get('billing_target')
              or 'individual').strip().lower()

    if booking_id is None:
        flash('Pick a booking to add.', 'error')
        return redirect(url_for('groups.detail', group_id=group.id))
    booking = Booking.query.get(booking_id)
    if booking is None:
        flash('Booking not found.', 'error')
        return redirect(url_for('groups.detail', group_id=group.id))

    r = attach_booking(group, booking,
                        billing_target=target,
                        user=current_user)
    if not r['ok']:
        flash(r['error'], 'error')
    else:
        db.session.commit()
        if r.get('no_op'):
            flash(f'Booking {booking.booking_ref} already in group.',
                  'info')
        else:
            flash(
                f'Booking {booking.booking_ref} added '
                f'(billing: {target}).', 'success')
    return redirect(url_for('groups.detail', group_id=group.id))


@groups_bp.route('/<int:group_id>/remove-booking', methods=['POST'])
@login_required
@admin_required
def remove_booking(group_id):
    group = BookingGroup.query.get_or_404(group_id)
    booking_id = _form_int('booking_id')
    if booking_id is None:
        flash('Pick a booking to remove.', 'error')
        return redirect(url_for('groups.detail', group_id=group.id))
    booking = Booking.query.get(booking_id)
    if booking is None:
        flash('Booking not found.', 'error')
        return redirect(url_for('groups.detail', group_id=group.id))

    r = detach_booking(group, booking, user=current_user)
    if not r['ok']:
        flash(r['error'], 'error')
    else:
        db.session.commit()
        flash(f'Booking {booking.booking_ref} removed from group.',
              'success')
    return redirect(url_for('groups.detail', group_id=group.id))


@groups_bp.route('/<int:group_id>/set-master', methods=['POST'])
@login_required
@admin_required
def set_master(group_id):
    group = BookingGroup.query.get_or_404(group_id)
    booking_id = _form_int('booking_id')
    booking = None
    if booking_id and booking_id != 0:
        booking = Booking.query.get(booking_id)
        if booking is None:
            flash('Booking not found.', 'error')
            return redirect(url_for('groups.detail', group_id=group.id))

    r = set_master_booking(group, booking, user=current_user)
    if not r['ok']:
        flash(r['error'], 'error')
    else:
        db.session.commit()
        if booking is None:
            flash('Master billing booking cleared.', 'success')
        else:
            flash(f'Master billing booking set to {booking.booking_ref}.',
                  'success')
    return redirect(url_for('groups.detail', group_id=group.id))


@groups_bp.route('/<int:group_id>/set-billing-target', methods=['POST'])
@login_required
@admin_required
def set_member_billing_target(group_id):
    group = BookingGroup.query.get_or_404(group_id)
    booking_id = _form_int('booking_id')
    target = (request.form.get('billing_target') or '').strip().lower()
    if booking_id is None or target not in VALID_BILLING_TARGETS:
        flash('Invalid input.', 'error')
        return redirect(url_for('groups.detail', group_id=group.id))
    booking = Booking.query.get(booking_id)
    if booking is None or booking.booking_group_id != group.id:
        flash('Booking not in this group.', 'error')
        return redirect(url_for('groups.detail', group_id=group.id))

    r = set_billing_target(booking, target, user=current_user)
    if not r['ok']:
        flash(r['error'], 'error')
    else:
        db.session.commit()
        flash(f'Booking {booking.booking_ref} billing target: {target}.',
              'success')
    return redirect(url_for('groups.detail', group_id=group.id))
