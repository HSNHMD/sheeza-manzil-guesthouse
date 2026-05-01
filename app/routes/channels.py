"""Channel Manager Foundation V1 — admin-only routes.

V1 is a SCHEMA + WORKFLOW preview. NONE of the routes here make
external API calls. The "test sync" button creates a no-op
ChannelSyncJob + ChannelSyncLog row. Real OTA integration is
Phase 4 of the build plan documented in
docs/channel_manager_build_phases.md.

Endpoints (all admin_required):

    GET  /admin/channels/                          list
    GET  /admin/channels/new                       new connection form
    POST /admin/channels/new                       create
    GET  /admin/channels/<id>                      detail (mappings + sync log)
    GET  /admin/channels/<id>/edit                 edit form
    POST /admin/channels/<id>/edit                 save metadata
    POST /admin/channels/<id>/status               flip status

    GET  /admin/channels/<id>/maps/rooms/new       room map form
    POST /admin/channels/<id>/maps/rooms/new       create room map
    POST /admin/channels/<id>/maps/rooms/<m_id>/toggle
                                                    toggle is_active
    POST /admin/channels/<id>/maps/rooms/<m_id>/delete
                                                    delete map

    GET  /admin/channels/<id>/maps/rates/new       rate plan map form
    POST /admin/channels/<id>/maps/rates/new       create rate map
    POST /admin/channels/<id>/maps/rates/<m_id>/toggle
    POST /admin/channels/<id>/maps/rates/<m_id>/delete

    POST /admin/channels/<id>/sync/test            queue + run test no-op job
"""

from __future__ import annotations

import json as _json

from flask import (
    Blueprint, render_template, request, redirect, url_for, flash,
)
from flask_login import login_required, current_user

from ..models import (
    db, ChannelConnection, ChannelRoomMap, ChannelRatePlanMap,
    ChannelSyncJob, ChannelSyncLog, RoomType, RatePlan, Booking,
)
from ..decorators import admin_required
from ..services.channels import (
    create_connection, update_connection_status,
    create_room_map, create_rate_plan_map,
    enqueue_test_sync_job,
    CHANNEL_NAMES, CONNECTION_STATUSES, SYNC_JOB_TYPES,
)


channels_bp = Blueprint('channels', __name__, url_prefix='/admin/channels')


def _form_int(name, default=None):
    raw = (request.form.get(name) or '').strip()
    if not raw:
        return default
    try:
        return int(raw)
    except ValueError:
        return default


# ── List ────────────────────────────────────────────────────────────

@channels_bp.route('/', methods=['GET'])
@login_required
@admin_required
def index():
    conns = (ChannelConnection.query
             .order_by(ChannelConnection.created_at.desc())
             .all())
    counts = {
        s: ChannelConnection.query.filter_by(status=s).count()
        for s in CONNECTION_STATUSES
    }
    return render_template(
        'channels/list.html',
        connections=conns,
        counts=counts,
        channel_names=CHANNEL_NAMES,
    )


# ── Create ──────────────────────────────────────────────────────────

@channels_bp.route('/new', methods=['GET', 'POST'])
@login_required
@admin_required
def new():
    if request.method == 'POST':
        result = create_connection(
            channel_name=request.form.get('channel_name'),
            account_label=request.form.get('account_label'),
            notes=request.form.get('notes'),
            status='inactive',          # always created inactive
            user=current_user,
        )
        if not result['ok']:
            flash(result['error'], 'error')
            return render_template('channels/form.html',
                                    conn=None, form=request.form,
                                    channel_names=CHANNEL_NAMES,
                                    statuses=CONNECTION_STATUSES), 400
        db.session.commit()
        flash(f'Channel connection {result["connection"].channel_name} '
              f'created (status: inactive).', 'success')
        return redirect(url_for('channels.detail',
                                  conn_id=result['connection'].id))
    return render_template('channels/form.html',
                            conn=None, form={},
                            channel_names=CHANNEL_NAMES,
                            statuses=CONNECTION_STATUSES)


# ── Detail ──────────────────────────────────────────────────────────

@channels_bp.route('/<int:conn_id>', methods=['GET'])
@login_required
@admin_required
def detail(conn_id):
    conn = ChannelConnection.query.get_or_404(conn_id)
    room_maps = list(conn.room_maps.order_by(ChannelRoomMap.id).all())
    rate_maps = list(conn.rate_maps.order_by(ChannelRatePlanMap.id).all())

    sync_jobs = (ChannelSyncJob.query
                 .filter_by(channel_connection_id=conn_id)
                 .order_by(ChannelSyncJob.created_at.desc())
                 .limit(20).all())
    sync_logs = (ChannelSyncLog.query
                 .filter_by(channel_connection_id=conn_id)
                 .order_by(ChannelSyncLog.created_at.desc())
                 .limit(50).all())

    # Bookings linked via external ref to this channel
    linked_bookings = (Booking.query
                       .filter_by(external_source=conn.channel_name)
                       .order_by(Booking.created_at.desc())
                       .limit(20).all())

    return render_template(
        'channels/detail.html',
        conn=conn,
        room_maps=room_maps,
        rate_maps=rate_maps,
        sync_jobs=sync_jobs,
        sync_logs=sync_logs,
        linked_bookings=linked_bookings,
        statuses=CONNECTION_STATUSES,
        sync_job_types=SYNC_JOB_TYPES,
    )


# ── Edit metadata ───────────────────────────────────────────────────

@channels_bp.route('/<int:conn_id>/edit', methods=['GET', 'POST'])
@login_required
@admin_required
def edit(conn_id):
    conn = ChannelConnection.query.get_or_404(conn_id)
    if request.method == 'POST':
        conn.account_label = (request.form.get('account_label')
                                or '').strip()[:160] or None
        conn.notes = (request.form.get('notes') or '').strip()[:2000] or None
        # config_json edits are admin-trusted; keep it as-is JSON-text.
        cfg_raw = (request.form.get('config_json') or '').strip()
        if cfg_raw:
            # Validate JSON shape — refuse malformed.
            try:
                _json.loads(cfg_raw)
            except (ValueError, TypeError):
                flash('config_json must be valid JSON or empty.', 'error')
                return redirect(url_for('channels.edit', conn_id=conn.id))
        conn.config_json = cfg_raw or None
        db.session.commit()
        flash(f'Channel {conn.channel_name} metadata saved.', 'success')
        return redirect(url_for('channels.detail', conn_id=conn.id))
    return render_template(
        'channels/form.html',
        conn=conn, form={},
        channel_names=CHANNEL_NAMES,
        statuses=CONNECTION_STATUSES,
    )


# ── Status flip ─────────────────────────────────────────────────────

@channels_bp.route('/<int:conn_id>/status', methods=['POST'])
@login_required
@admin_required
def set_status(conn_id):
    conn = ChannelConnection.query.get_or_404(conn_id)
    new_status = (request.form.get('status') or '').strip()
    result = update_connection_status(conn, new_status, user=current_user)
    if not result['ok']:
        flash(result['error'], 'error')
    else:
        db.session.commit()
        flash(f'Channel {conn.channel_name} status: {conn.status}.',
              'success')
    return redirect(url_for('channels.detail', conn_id=conn.id))


# ── Room mappings ───────────────────────────────────────────────────

@channels_bp.route('/<int:conn_id>/maps/rooms/new',
                    methods=['GET', 'POST'])
@login_required
@admin_required
def room_map_new(conn_id):
    conn = ChannelConnection.query.get_or_404(conn_id)
    types = (RoomType.query
             .filter_by(is_active=True, property_id=conn.property_id)
             .order_by(RoomType.name).all())

    if request.method == 'POST':
        result = create_room_map(
            connection=conn,
            room_type_id=_form_int('room_type_id'),
            external_room_id=request.form.get('external_room_id'),
            external_room_name_snapshot=request.form.get(
                'external_room_name_snapshot'),
            inventory_count_override=_form_int('inventory_count_override'),
            notes=request.form.get('notes'),
            user=current_user,
        )
        if not result['ok']:
            flash(result['error'], 'error')
            return render_template('channels/room_map_form.html',
                                    conn=conn, types=types,
                                    form=request.form), 400
        db.session.commit()
        flash('Room map created.', 'success')
        return redirect(url_for('channels.detail', conn_id=conn.id))
    return render_template('channels/room_map_form.html',
                            conn=conn, types=types, form={})


@channels_bp.route('/<int:conn_id>/maps/rooms/<int:map_id>/toggle',
                    methods=['POST'])
@login_required
@admin_required
def room_map_toggle(conn_id, map_id):
    m = ChannelRoomMap.query.get_or_404(map_id)
    if m.channel_connection_id != conn_id:
        flash('Map does not belong to this connection.', 'error')
        return redirect(url_for('channels.detail', conn_id=conn_id))
    m.is_active = not m.is_active
    db.session.commit()
    flash(f'Room map {"activated" if m.is_active else "deactivated"}.',
          'success')
    return redirect(url_for('channels.detail', conn_id=conn_id))


@channels_bp.route('/<int:conn_id>/maps/rooms/<int:map_id>/delete',
                    methods=['POST'])
@login_required
@admin_required
def room_map_delete(conn_id, map_id):
    m = ChannelRoomMap.query.get_or_404(map_id)
    if m.channel_connection_id != conn_id:
        flash('Map does not belong to this connection.', 'error')
        return redirect(url_for('channels.detail', conn_id=conn_id))
    db.session.delete(m)
    db.session.commit()
    flash('Room map deleted.', 'success')
    return redirect(url_for('channels.detail', conn_id=conn_id))


# ── Rate plan mappings ──────────────────────────────────────────────

@channels_bp.route('/<int:conn_id>/maps/rates/new',
                    methods=['GET', 'POST'])
@login_required
@admin_required
def rate_map_new(conn_id):
    conn = ChannelConnection.query.get_or_404(conn_id)
    plans = (RatePlan.query
             .filter_by(is_active=True, property_id=conn.property_id)
             .order_by(RatePlan.name).all())

    if request.method == 'POST':
        result = create_rate_plan_map(
            connection=conn,
            rate_plan_id=_form_int('rate_plan_id'),
            external_rate_plan_id=request.form.get('external_rate_plan_id'),
            external_rate_plan_name_snapshot=request.form.get(
                'external_rate_plan_name_snapshot'),
            meal_plan_external_id=request.form.get('meal_plan_external_id'),
            cancellation_policy_external_id=request.form.get(
                'cancellation_policy_external_id'),
            notes=request.form.get('notes'),
            user=current_user,
        )
        if not result['ok']:
            flash(result['error'], 'error')
            return render_template('channels/rate_map_form.html',
                                    conn=conn, plans=plans,
                                    form=request.form), 400
        db.session.commit()
        flash('Rate plan map created.', 'success')
        return redirect(url_for('channels.detail', conn_id=conn.id))
    return render_template('channels/rate_map_form.html',
                            conn=conn, plans=plans, form={})


@channels_bp.route('/<int:conn_id>/maps/rates/<int:map_id>/toggle',
                    methods=['POST'])
@login_required
@admin_required
def rate_map_toggle(conn_id, map_id):
    m = ChannelRatePlanMap.query.get_or_404(map_id)
    if m.channel_connection_id != conn_id:
        flash('Map does not belong to this connection.', 'error')
        return redirect(url_for('channels.detail', conn_id=conn_id))
    m.is_active = not m.is_active
    db.session.commit()
    flash(f'Rate plan map {"activated" if m.is_active else "deactivated"}.',
          'success')
    return redirect(url_for('channels.detail', conn_id=conn_id))


@channels_bp.route('/<int:conn_id>/maps/rates/<int:map_id>/delete',
                    methods=['POST'])
@login_required
@admin_required
def rate_map_delete(conn_id, map_id):
    m = ChannelRatePlanMap.query.get_or_404(map_id)
    if m.channel_connection_id != conn_id:
        flash('Map does not belong to this connection.', 'error')
        return redirect(url_for('channels.detail', conn_id=conn_id))
    db.session.delete(m)
    db.session.commit()
    flash('Rate plan map deleted.', 'success')
    return redirect(url_for('channels.detail', conn_id=conn_id))


# ── Test sync (V1 no-op) ────────────────────────────────────────────

@channels_bp.route('/<int:conn_id>/sync/test', methods=['POST'])
@login_required
@admin_required
def sync_test(conn_id):
    """V1 staging-safe test sync. NEVER calls an external API.

    Creates a ChannelSyncJob row with job_type='test_noop' that is
    immediately marked 'success' + a matching ChannelSyncLog row.
    Useful to confirm the workflow surface works before real
    Phase 4 integration arrives.
    """
    conn = ChannelConnection.query.get_or_404(conn_id)
    result = enqueue_test_sync_job(conn, user=current_user)
    if not result['ok']:
        flash(result['error'], 'error')
    else:
        db.session.commit()
        flash(
            f'V1 test sync recorded for {conn.channel_name} — '
            f'no external request was made.',
            'success')
    return redirect(url_for('channels.detail', conn_id=conn_id))


# ── Sandbox reservation import (staging-safe) ───────────────────────

@channels_bp.route('/<int:conn_id>/sandbox-import', methods=['POST'])
@login_required
@admin_required
def sandbox_import(conn_id):
    """Staging-only inbound reservation simulation.

    Drives services.channel_import.import_reservation() with a payload
    constructed from form fields. NEVER calls an external API — the
    payload is built locally so the operator can exercise the full
    pipeline (validation → mapping → availability → exception queue
    or booking creation) without an OTA round-trip.
    """
    from ..services.channel_import import import_reservation

    conn = ChannelConnection.query.get_or_404(conn_id)
    payload = {
        'external_reservation_ref': (request.form.get(
            'external_reservation_ref') or '').strip(),
        'external_room_id':         (request.form.get(
            'external_room_id') or '').strip(),
        'external_rate_plan_id':    (request.form.get(
            'external_rate_plan_id') or '').strip() or None,
        'check_in':                 (request.form.get('check_in') or '').strip(),
        'check_out':                (request.form.get('check_out') or '').strip(),
        'num_guests':               request.form.get('num_guests') or 1,
        'guest_first_name':         (request.form.get(
            'guest_first_name') or '').strip(),
        'guest_last_name':          (request.form.get(
            'guest_last_name') or '').strip(),
        'guest_email':              (request.form.get('guest_email')
                                     or '').strip() or None,
        'guest_phone':              (request.form.get('guest_phone')
                                     or '').strip() or None,
        'total_amount':             request.form.get('total_amount') or None,
    }
    result = import_reservation(connection=conn, payload=payload,
                                actor_user_id=current_user.id)
    if result.action == 'imported':
        flash(f'✓ {result.message}', 'success')
        return redirect(url_for(
            'bookings.detail', booking_id=result.booking.id))
    if result.action == 'duplicate_skipped':
        flash(f'↻ {result.message}', 'info')
        return redirect(url_for('channels.detail', conn_id=conn.id))
    if result.action == 'queued':
        flash(f'⚠ {result.message}', 'warning')
        return redirect(url_for(
            'channel_exceptions.detail', exc_id=result.exception.id))
    flash(result.message, 'error')
    return redirect(url_for('channels.detail', conn_id=conn.id))
