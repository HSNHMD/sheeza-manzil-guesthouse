"""OTA Reservation Import — Exception Queue admin routes.

Admin-only manual-review queue for inbound reservation imports that
couldn't be auto-applied (duplicate, conflict, mapping missing,
invalid payload, parse error). All routes are @login_required +
@admin_required at the route level — staff are bounced by the
existing staff_guard since /admin/channel-exceptions/ isn't on the
whitelist.

Endpoints:

    GET  /admin/channel-exceptions/                 list with filters + KPI tiles
    GET  /admin/channel-exceptions/<id>             detail + lifecycle actions
    POST /admin/channel-exceptions/<id>/status      change status (incl. resolve)

V1 NEVER makes outbound HTTP from this surface — these are pure
DB lifecycle transitions on the queue.
"""

from __future__ import annotations

from flask import (Blueprint, render_template, request, redirect,
                   url_for, flash, abort)
from flask_login import login_required, current_user

from ..decorators import admin_required
from ..models import (db, ChannelImportException, ChannelConnection,
                      Booking, ActivityLog)
from ..services import channel_import as svc


channel_exceptions_bp = Blueprint(
    'channel_exceptions', __name__,
    url_prefix='/admin/channel-exceptions',
)


@channel_exceptions_bp.route('/', methods=['GET'])
@login_required
@admin_required
def index():
    """List the queue with KPI tiles + status/issue/connection filters."""
    open_only = request.args.get('open') == '1'
    status    = (request.args.get('status') or '').strip() or None
    issue     = (request.args.get('issue_type') or '').strip() or None
    conn_raw  = (request.args.get('connection_id') or '').strip()
    conn_id   = int(conn_raw) if conn_raw.isdigit() else None

    rows = svc.list_exceptions(
        open_only=open_only, status=status, issue_type=issue,
        channel_connection_id=conn_id,
    )
    summary = svc.summary_counts()
    connections = (ChannelConnection.query
                   .order_by(ChannelConnection.channel_name).all())

    return render_template(
        'channel_exceptions/index.html',
        rows=rows, summary=summary, connections=connections,
        open_only=open_only, status=status, issue_type=issue,
        connection_id=conn_id,
        statuses=ChannelImportException.STATUSES,
        issue_types=ChannelImportException.ISSUE_TYPES,
    )


@channel_exceptions_bp.route('/<int:exc_id>', methods=['GET'])
@login_required
@admin_required
def detail(exc_id):
    exc = ChannelImportException.query.get(exc_id)
    if exc is None:
        abort(404)
    # Latest few activity rows that mention this exception.
    activity = (ActivityLog.query
                .filter(ActivityLog.action.in_((
                    'channel.reservation_conflict_queued',
                    'channel.reservation_import_failed',
                    'channel.exception_status_changed',
                )))
                .order_by(ActivityLog.created_at.desc())
                .limit(50).all())
    import json
    relevant = []
    for a in activity:
        try:
            md = json.loads(a.metadata_json or '{}')
        except (TypeError, ValueError):
            md = {}
        if md.get('entity_type') == 'channel_import_exception' and \
           md.get('entity_id') == exc.id:
            relevant.append(a)
            continue
        if (md.get('external_reservation_ref') ==
                exc.external_reservation_ref and
                md.get('external_source') == exc.external_source):
            relevant.append(a)
    return render_template(
        'channel_exceptions/detail.html',
        exc=exc, activity=relevant,
        statuses=ChannelImportException.STATUSES,
    )


@channel_exceptions_bp.route('/<int:exc_id>/status', methods=['POST'])
@login_required
@admin_required
def update_status(exc_id):
    exc = ChannelImportException.query.get(exc_id)
    if exc is None:
        abort(404)
    new_status = (request.form.get('status') or '').strip()
    raw_link   = (request.form.get('linked_booking_id') or '').strip()
    linked_booking_id = int(raw_link) if raw_link.isdigit() else None
    notes = (request.form.get('notes') or '').strip() or None

    result = svc.update_exception_status(
        exception=exc, new_status=new_status,
        actor_user_id=current_user.id,
        linked_booking_id=linked_booking_id,
        notes=notes,
    )
    if result['ok']:
        flash(f'Exception #{exc.id} → {new_status}.', 'success')
    else:
        flash(result['error'], 'error')
    return redirect(url_for('channel_exceptions.detail', exc_id=exc_id))
