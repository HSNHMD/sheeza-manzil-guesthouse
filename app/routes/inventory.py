"""Rates & Inventory V1 — admin CRUD + audit trail.

Admin-only endpoints for managing RoomType, RatePlan, RateOverride and
RateRestriction. Every create/edit/toggle writes an ActivityLog row.

Endpoints (all admin-required):

    GET  /admin/inventory/                          — overview / fleet summary
    GET  /admin/inventory/room-types                — list
    GET  /admin/inventory/room-types/new            — form
    POST /admin/inventory/room-types/new            — create
    GET  /admin/inventory/room-types/<id>/edit      — form
    POST /admin/inventory/room-types/<id>/edit      — save
    POST /admin/inventory/room-types/<id>/toggle    — flip is_active

    GET  /admin/inventory/rate-plans                — list
    GET  /admin/inventory/rate-plans/new            — form
    POST /admin/inventory/rate-plans/new            — create
    GET  /admin/inventory/rate-plans/<id>/edit      — form
    POST /admin/inventory/rate-plans/<id>/edit      — save
    POST /admin/inventory/rate-plans/<id>/toggle    — flip is_active

    GET  /admin/inventory/overrides                 — list
    GET  /admin/inventory/overrides/new             — form
    POST /admin/inventory/overrides/new             — create
    GET  /admin/inventory/overrides/<id>/edit       — form
    POST /admin/inventory/overrides/<id>/edit       — save
    POST /admin/inventory/overrides/<id>/toggle     — flip is_active

    GET  /admin/inventory/restrictions              — list
    GET  /admin/inventory/restrictions/new          — form
    POST /admin/inventory/restrictions/new          — create
    GET  /admin/inventory/restrictions/<id>/edit    — form
    POST /admin/inventory/restrictions/<id>/edit    — save
    POST /admin/inventory/restrictions/<id>/toggle  — flip is_active

ActivityLog actions written:
    inventory.room_type_created, inventory.room_type_updated
    inventory.rate_plan_created, inventory.rate_plan_updated
    inventory.rate_override_created, inventory.rate_override_updated
    inventory.restriction_updated  (used for create + edit)

All metadata is a strict whitelist — see services/inventory.py +
the audit calls below for the per-event allowed keys.
"""

from __future__ import annotations

from datetime import date, datetime

from flask import (
    Blueprint, render_template, request, redirect, url_for, flash, abort,
)
from flask_login import login_required, current_user

from ..models import (
    db, RoomType, RatePlan, RateOverride, RateRestriction, Room,
)
from ..decorators import admin_required
from ..services.audit import log_activity
from ..services.inventory import (
    validate_date_range, validate_nightly_rate, validate_min_max_stay,
    fleet_summary,
)


inventory_bp = Blueprint('inventory', __name__,
                         url_prefix='/admin/inventory')


# ── Helpers ─────────────────────────────────────────────────────────

def _parse_date(s):
    s = (s or '').strip()
    if not s:
        return None
    try:
        return date.fromisoformat(s)
    except ValueError:
        return None


def _form_str(name, *, max_len=255):
    v = (request.form.get(name) or '').strip()
    return v[:max_len] if v else None


def _form_int(name):
    raw = (request.form.get(name) or '').strip()
    if not raw:
        return None
    try:
        return int(raw)
    except ValueError:
        return None


def _form_float(name):
    raw = (request.form.get(name) or '').strip()
    if not raw:
        return None
    try:
        return float(raw)
    except ValueError:
        return None


def _form_bool(name):
    return (request.form.get(name) or '').strip().lower() in (
        '1', 'on', 'true', 'yes')


# ── Overview ────────────────────────────────────────────────────────

@inventory_bp.route('/', methods=['GET'])
@login_required
@admin_required
def overview():
    today = date.today()
    rows = fleet_summary(today, today)
    counts = {
        'room_types':   RoomType.query.count(),
        'rate_plans':   RatePlan.query.filter_by(is_active=True).count(),
        'overrides':    RateOverride.query.filter_by(is_active=True).count(),
        'restrictions': RateRestriction.query.filter_by(is_active=True).count(),
    }
    return render_template('inventory/overview.html',
                           rows=rows, counts=counts, today=today)


# ── RoomType CRUD ───────────────────────────────────────────────────

@inventory_bp.route('/room-types', methods=['GET'])
@login_required
@admin_required
def room_types_list():
    types = RoomType.query.order_by(RoomType.name).all()
    return render_template('inventory/room_types_list.html', types=types)


@inventory_bp.route('/room-types/new', methods=['GET', 'POST'])
@login_required
@admin_required
def room_type_new():
    if request.method == 'POST':
        code = (request.form.get('code') or '').strip().upper()
        name = (request.form.get('name') or '').strip()
        max_occ = _form_int('max_occupancy') or 2
        base_cap = _form_int('base_capacity') or max_occ
        description = _form_str('description', max_len=1000)
        errors = []
        if not code or len(code) > 20:
            errors.append('code is required and ≤20 chars.')
        if not name or len(name) > 100:
            errors.append('name is required and ≤100 chars.')
        if max_occ < 1:
            errors.append('max_occupancy must be ≥1.')
        if base_cap < 1 or base_cap > max_occ:
            errors.append('base_capacity must be between 1 and max_occupancy.')
        if RoomType.query.filter_by(code=code).first() is not None:
            errors.append(f'code {code!r} already exists.')
        if errors:
            for e in errors:
                flash(e, 'error')
            return render_template('inventory/room_type_form.html',
                                   rt=None, form=request.form), 400
        rt = RoomType(
            code=code, name=name,
            max_occupancy=max_occ, base_capacity=base_cap,
            description=description, is_active=True,
        )
        db.session.add(rt)
        db.session.flush()
        log_activity(
            'inventory.room_type_created',
            description=f'Room type {rt.code} ({rt.name}) created.',
            metadata={
                'room_type_id': rt.id, 'code': rt.code, 'name': rt.name,
                'max_occupancy': rt.max_occupancy,
                'base_capacity': rt.base_capacity,
            },
        )
        db.session.commit()
        flash(f'Room type {rt.code} created.', 'success')
        return redirect(url_for('inventory.room_types_list'))
    return render_template('inventory/room_type_form.html', rt=None, form={})


@inventory_bp.route('/room-types/<int:rt_id>/edit', methods=['GET', 'POST'])
@login_required
@admin_required
def room_type_edit(rt_id):
    rt = RoomType.query.get_or_404(rt_id)
    if request.method == 'POST':
        old = {'name': rt.name, 'max_occupancy': rt.max_occupancy,
               'base_capacity': rt.base_capacity, 'is_active': rt.is_active}
        rt.name = (request.form.get('name') or rt.name).strip()
        max_occ = _form_int('max_occupancy') or rt.max_occupancy
        base_cap = _form_int('base_capacity') or rt.base_capacity
        if max_occ < 1 or base_cap < 1 or base_cap > max_occ:
            flash('invalid capacity values.', 'error')
            return redirect(url_for('inventory.room_type_edit', rt_id=rt.id))
        rt.max_occupancy = max_occ
        rt.base_capacity = base_cap
        rt.description = _form_str('description', max_len=1000)
        rt.is_active = _form_bool('is_active')
        log_activity(
            'inventory.room_type_updated',
            description=f'Room type {rt.code} updated.',
            metadata={
                'room_type_id': rt.id, 'code': rt.code,
                'old_name': old['name'], 'new_name': rt.name,
                'old_is_active': old['is_active'],
                'new_is_active': rt.is_active,
            },
        )
        db.session.commit()
        flash(f'Room type {rt.code} saved.', 'success')
        return redirect(url_for('inventory.room_types_list'))
    return render_template('inventory/room_type_form.html', rt=rt, form={})


@inventory_bp.route('/room-types/<int:rt_id>/toggle', methods=['POST'])
@login_required
@admin_required
def room_type_toggle(rt_id):
    rt = RoomType.query.get_or_404(rt_id)
    rt.is_active = not rt.is_active
    log_activity(
        'inventory.room_type_updated',
        description=(
            f'Room type {rt.code} '
            f'{"activated" if rt.is_active else "deactivated"}.'
        ),
        metadata={'room_type_id': rt.id, 'code': rt.code,
                  'new_is_active': rt.is_active},
    )
    db.session.commit()
    flash(f'Room type {rt.code} '
          f'{"activated" if rt.is_active else "deactivated"}.',
          'success')
    return redirect(url_for('inventory.room_types_list'))


# ── RatePlan CRUD ───────────────────────────────────────────────────

@inventory_bp.route('/rate-plans', methods=['GET'])
@login_required
@admin_required
def rate_plans_list():
    plans = (RatePlan.query
             .order_by(RatePlan.is_active.desc(), RatePlan.name)
             .all())
    return render_template('inventory/rate_plans_list.html', plans=plans)


@inventory_bp.route('/rate-plans/new', methods=['GET', 'POST'])
@login_required
@admin_required
def rate_plan_new():
    types = RoomType.query.filter_by(is_active=True).order_by(RoomType.name).all()
    if request.method == 'POST':
        code = (request.form.get('code') or '').strip().upper()
        name = (request.form.get('name') or '').strip()
        room_type_id = _form_int('room_type_id')
        base_rate = _form_float('base_rate')
        currency = (request.form.get('currency') or 'USD').strip().upper()[:8]
        is_refundable = _form_bool('is_refundable')

        errors = []
        if not code or len(code) > 30:
            errors.append('code required, ≤30 chars.')
        if not name:
            errors.append('name required.')
        if room_type_id is None or RoomType.query.get(room_type_id) is None:
            errors.append('valid room_type_id required.')
        rate_err = validate_nightly_rate(base_rate)
        if rate_err:
            errors.append(rate_err.replace('nightly_rate', 'base_rate'))
        if RatePlan.query.filter_by(code=code).first() is not None:
            errors.append(f'code {code!r} already exists.')
        if errors:
            for e in errors:
                flash(e, 'error')
            return render_template('inventory/rate_plan_form.html',
                                   plan=None, types=types,
                                   form=request.form), 400
        plan = RatePlan(
            code=code, name=name, room_type_id=room_type_id,
            base_rate=base_rate, currency=currency,
            is_refundable=is_refundable, is_active=True,
            notes=_form_str('notes', max_len=1000),
        )
        db.session.add(plan)
        db.session.flush()
        log_activity(
            'inventory.rate_plan_created',
            description=f'Rate plan {plan.code} created.',
            metadata={
                'rate_plan_id': plan.id, 'code': plan.code,
                'room_type_id': plan.room_type_id,
                'base_rate': plan.base_rate, 'currency': plan.currency,
            },
        )
        db.session.commit()
        flash(f'Rate plan {plan.code} created.', 'success')
        return redirect(url_for('inventory.rate_plans_list'))
    return render_template('inventory/rate_plan_form.html',
                           plan=None, types=types, form={})


@inventory_bp.route('/rate-plans/<int:plan_id>/edit',
                    methods=['GET', 'POST'])
@login_required
@admin_required
def rate_plan_edit(plan_id):
    plan = RatePlan.query.get_or_404(plan_id)
    types = RoomType.query.filter_by(is_active=True).order_by(RoomType.name).all()
    if request.method == 'POST':
        old_rate = plan.base_rate
        plan.name = (request.form.get('name') or plan.name).strip()
        new_rate = _form_float('base_rate')
        rate_err = validate_nightly_rate(new_rate)
        if rate_err:
            flash(rate_err.replace('nightly_rate', 'base_rate'), 'error')
            return redirect(url_for('inventory.rate_plan_edit',
                                    plan_id=plan.id))
        plan.base_rate = new_rate
        plan.currency = (request.form.get('currency') or plan.currency).strip().upper()[:8]
        plan.is_refundable = _form_bool('is_refundable')
        plan.is_active = _form_bool('is_active')
        plan.notes = _form_str('notes', max_len=1000)
        log_activity(
            'inventory.rate_plan_updated',
            description=f'Rate plan {plan.code} updated.',
            metadata={
                'rate_plan_id': plan.id, 'code': plan.code,
                'old_base_rate': old_rate, 'new_base_rate': plan.base_rate,
                'is_active': plan.is_active,
            },
        )
        db.session.commit()
        flash(f'Rate plan {plan.code} saved.', 'success')
        return redirect(url_for('inventory.rate_plans_list'))
    return render_template('inventory/rate_plan_form.html',
                           plan=plan, types=types, form={})


@inventory_bp.route('/rate-plans/<int:plan_id>/toggle', methods=['POST'])
@login_required
@admin_required
def rate_plan_toggle(plan_id):
    plan = RatePlan.query.get_or_404(plan_id)
    plan.is_active = not plan.is_active
    log_activity(
        'inventory.rate_plan_updated',
        description=(
            f'Rate plan {plan.code} '
            f'{"activated" if plan.is_active else "deactivated"}.'
        ),
        metadata={'rate_plan_id': plan.id, 'code': plan.code,
                  'is_active': plan.is_active},
    )
    db.session.commit()
    flash(f'Rate plan {plan.code} '
          f'{"activated" if plan.is_active else "deactivated"}.',
          'success')
    return redirect(url_for('inventory.rate_plans_list'))


# ── RateOverride CRUD ───────────────────────────────────────────────

@inventory_bp.route('/overrides', methods=['GET'])
@login_required
@admin_required
def overrides_list():
    overrides = (RateOverride.query
                 .order_by(RateOverride.is_active.desc(),
                           RateOverride.start_date.desc())
                 .all())
    types = {rt.id: rt for rt in RoomType.query.all()}
    plans = {p.id: p for p in RatePlan.query.all()}
    return render_template('inventory/overrides_list.html',
                           overrides=overrides, types=types, plans=plans)


@inventory_bp.route('/overrides/new', methods=['GET', 'POST'])
@login_required
@admin_required
def override_new():
    types = RoomType.query.filter_by(is_active=True).order_by(RoomType.name).all()
    plans = RatePlan.query.filter_by(is_active=True).order_by(RatePlan.name).all()
    if request.method == 'POST':
        room_type_id = _form_int('room_type_id')
        rate_plan_id = _form_int('rate_plan_id')  # may be None
        start_d = _parse_date(request.form.get('start_date'))
        end_d   = _parse_date(request.form.get('end_date'))
        nightly = _form_float('nightly_rate')

        errors = []
        if room_type_id is None or RoomType.query.get(room_type_id) is None:
            errors.append('room_type_id required.')
        if rate_plan_id is not None and RatePlan.query.get(rate_plan_id) is None:
            errors.append('invalid rate_plan_id.')
        d_err = validate_date_range(start_d, end_d)
        if d_err: errors.append(d_err)
        r_err = validate_nightly_rate(nightly)
        if r_err: errors.append(r_err)

        if errors:
            for e in errors: flash(e, 'error')
            return render_template('inventory/override_form.html',
                                   override=None, types=types, plans=plans,
                                   form=request.form), 400
        ov = RateOverride(
            room_type_id=room_type_id, rate_plan_id=rate_plan_id,
            start_date=start_d, end_date=end_d,
            nightly_rate=nightly, is_active=True,
            notes=_form_str('notes', max_len=1000),
        )
        db.session.add(ov)
        db.session.flush()
        log_activity(
            'inventory.rate_override_created',
            description=(
                f'Rate override on room_type {ov.room_type_id} '
                f'{ov.start_date} → {ov.end_date} @ {ov.nightly_rate}.'
            ),
            metadata={
                'override_id':   ov.id,
                'room_type_id':  ov.room_type_id,
                'rate_plan_id':  ov.rate_plan_id,
                'start_date':    ov.start_date.isoformat(),
                'end_date':      ov.end_date.isoformat(),
                'nightly_rate':  ov.nightly_rate,
            },
        )
        db.session.commit()
        flash('Rate override created.', 'success')
        return redirect(url_for('inventory.overrides_list'))
    return render_template('inventory/override_form.html',
                           override=None, types=types, plans=plans, form={})


@inventory_bp.route('/overrides/<int:ov_id>/edit', methods=['GET', 'POST'])
@login_required
@admin_required
def override_edit(ov_id):
    ov = RateOverride.query.get_or_404(ov_id)
    types = RoomType.query.filter_by(is_active=True).order_by(RoomType.name).all()
    plans = RatePlan.query.filter_by(is_active=True).order_by(RatePlan.name).all()
    if request.method == 'POST':
        old_rate = ov.nightly_rate
        start_d = _parse_date(request.form.get('start_date')) or ov.start_date
        end_d   = _parse_date(request.form.get('end_date')) or ov.end_date
        nightly = _form_float('nightly_rate')
        d_err = validate_date_range(start_d, end_d)
        r_err = validate_nightly_rate(nightly)
        if d_err or r_err:
            flash(d_err or r_err, 'error')
            return redirect(url_for('inventory.override_edit', ov_id=ov.id))
        ov.start_date = start_d
        ov.end_date = end_d
        ov.nightly_rate = nightly
        ov.is_active = _form_bool('is_active')
        ov.notes = _form_str('notes', max_len=1000)
        log_activity(
            'inventory.rate_override_updated',
            description=(
                f'Rate override #{ov.id} updated.'
            ),
            metadata={
                'override_id':   ov.id,
                'room_type_id':  ov.room_type_id,
                'rate_plan_id':  ov.rate_plan_id,
                'old_nightly_rate': old_rate,
                'new_nightly_rate': ov.nightly_rate,
                'start_date':    ov.start_date.isoformat(),
                'end_date':      ov.end_date.isoformat(),
                'is_active':     ov.is_active,
            },
        )
        db.session.commit()
        flash('Rate override saved.', 'success')
        return redirect(url_for('inventory.overrides_list'))
    return render_template('inventory/override_form.html',
                           override=ov, types=types, plans=plans, form={})


@inventory_bp.route('/overrides/<int:ov_id>/toggle', methods=['POST'])
@login_required
@admin_required
def override_toggle(ov_id):
    ov = RateOverride.query.get_or_404(ov_id)
    ov.is_active = not ov.is_active
    log_activity(
        'inventory.rate_override_updated',
        description=(f'Rate override #{ov.id} '
                     f'{"activated" if ov.is_active else "deactivated"}.'),
        metadata={'override_id': ov.id, 'is_active': ov.is_active,
                  'room_type_id': ov.room_type_id},
    )
    db.session.commit()
    flash(f'Override #{ov.id} '
          f'{"activated" if ov.is_active else "deactivated"}.',
          'success')
    return redirect(url_for('inventory.overrides_list'))


# ── RateRestriction CRUD ────────────────────────────────────────────

@inventory_bp.route('/restrictions', methods=['GET'])
@login_required
@admin_required
def restrictions_list():
    rows = (RateRestriction.query
            .order_by(RateRestriction.is_active.desc(),
                      RateRestriction.start_date.desc())
            .all())
    types = {rt.id: rt for rt in RoomType.query.all()}
    return render_template('inventory/restrictions_list.html',
                           restrictions=rows, types=types)


@inventory_bp.route('/restrictions/new', methods=['GET', 'POST'])
@login_required
@admin_required
def restriction_new():
    types = RoomType.query.filter_by(is_active=True).order_by(RoomType.name).all()
    if request.method == 'POST':
        room_type_id = _form_int('room_type_id')
        start_d = _parse_date(request.form.get('start_date'))
        end_d   = _parse_date(request.form.get('end_date'))
        min_stay = _form_int('min_stay')
        max_stay = _form_int('max_stay')
        cta = _form_bool('closed_to_arrival')
        ctd = _form_bool('closed_to_departure')
        stop = _form_bool('stop_sell')

        errors = []
        if room_type_id is None or RoomType.query.get(room_type_id) is None:
            errors.append('room_type_id required.')
        d_err = validate_date_range(start_d, end_d)
        if d_err: errors.append(d_err)
        s_err = validate_min_max_stay(min_stay, max_stay)
        if s_err: errors.append(s_err)
        if errors:
            for e in errors: flash(e, 'error')
            return render_template('inventory/restriction_form.html',
                                   restriction=None, types=types,
                                   form=request.form), 400
        r = RateRestriction(
            room_type_id=room_type_id,
            start_date=start_d, end_date=end_d,
            min_stay=min_stay, max_stay=max_stay,
            closed_to_arrival=cta, closed_to_departure=ctd,
            stop_sell=stop, is_active=True,
            notes=_form_str('notes', max_len=1000),
        )
        db.session.add(r)
        db.session.flush()
        log_activity(
            'inventory.restriction_updated',
            description=(
                f'Restriction created on room_type {r.room_type_id} '
                f'{r.start_date} → {r.end_date}.'
            ),
            metadata={
                'restriction_id': r.id,
                'room_type_id':   r.room_type_id,
                'start_date':     r.start_date.isoformat(),
                'end_date':       r.end_date.isoformat(),
                'min_stay':       r.min_stay,
                'max_stay':       r.max_stay,
                'stop_sell':      r.stop_sell,
                'closed_to_arrival':   r.closed_to_arrival,
                'closed_to_departure': r.closed_to_departure,
                'is_active':      r.is_active,
            },
        )
        db.session.commit()
        flash('Restriction created.', 'success')
        return redirect(url_for('inventory.restrictions_list'))
    return render_template('inventory/restriction_form.html',
                           restriction=None, types=types, form={})


@inventory_bp.route('/restrictions/<int:r_id>/edit',
                    methods=['GET', 'POST'])
@login_required
@admin_required
def restriction_edit(r_id):
    r = RateRestriction.query.get_or_404(r_id)
    types = RoomType.query.filter_by(is_active=True).order_by(RoomType.name).all()
    if request.method == 'POST':
        start_d = _parse_date(request.form.get('start_date')) or r.start_date
        end_d   = _parse_date(request.form.get('end_date')) or r.end_date
        min_stay = _form_int('min_stay')
        max_stay = _form_int('max_stay')
        d_err = validate_date_range(start_d, end_d)
        s_err = validate_min_max_stay(min_stay, max_stay)
        if d_err or s_err:
            flash(d_err or s_err, 'error')
            return redirect(url_for('inventory.restriction_edit', r_id=r.id))
        r.start_date = start_d; r.end_date = end_d
        r.min_stay = min_stay; r.max_stay = max_stay
        r.closed_to_arrival   = _form_bool('closed_to_arrival')
        r.closed_to_departure = _form_bool('closed_to_departure')
        r.stop_sell           = _form_bool('stop_sell')
        r.is_active           = _form_bool('is_active')
        r.notes = _form_str('notes', max_len=1000)
        log_activity(
            'inventory.restriction_updated',
            description=f'Restriction #{r.id} updated.',
            metadata={
                'restriction_id': r.id,
                'room_type_id':   r.room_type_id,
                'start_date':     r.start_date.isoformat(),
                'end_date':       r.end_date.isoformat(),
                'min_stay':       r.min_stay,
                'max_stay':       r.max_stay,
                'stop_sell':      r.stop_sell,
                'closed_to_arrival':   r.closed_to_arrival,
                'closed_to_departure': r.closed_to_departure,
                'is_active':      r.is_active,
            },
        )
        db.session.commit()
        flash('Restriction saved.', 'success')
        return redirect(url_for('inventory.restrictions_list'))
    return render_template('inventory/restriction_form.html',
                           restriction=r, types=types, form={})


@inventory_bp.route('/restrictions/<int:r_id>/toggle', methods=['POST'])
@login_required
@admin_required
def restriction_toggle(r_id):
    r = RateRestriction.query.get_or_404(r_id)
    r.is_active = not r.is_active
    log_activity(
        'inventory.restriction_updated',
        description=(f'Restriction #{r.id} '
                     f'{"activated" if r.is_active else "deactivated"}.'),
        metadata={'restriction_id': r.id, 'room_type_id': r.room_type_id,
                  'is_active': r.is_active},
    )
    db.session.commit()
    flash(f'Restriction #{r.id} '
          f'{"activated" if r.is_active else "deactivated"}.',
          'success')
    return redirect(url_for('inventory.restrictions_list'))
