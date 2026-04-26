"""Admin-only activity / audit log feed.

Mounted at /admin/activity. READ-ONLY. There are deliberately no POST,
PATCH, PUT, or DELETE routes — the activity_logs table is append-only
and the only sanctioned writer is `app.services.audit.log_activity()`.

Filters (all optional, all `?…` query params):
    booking_id   — show entries for one booking only
    invoice_id   — show entries for one invoice only
    action       — exact-match on the action label
    actor_type   — guest | admin | system | ai_agent

Pagination: simplified — we cap the result set at 100 newest rows.
A future iteration can add cursor-based paging or per-month archives.
"""

from __future__ import annotations

import json

from flask import Blueprint, render_template, request
from flask_login import login_required

from ..decorators import admin_required
from ..models import ActivityLog


activity_bp = Blueprint('activity', __name__, url_prefix='/admin/activity')

_RESULT_LIMIT = 100


def _parse_int(value):
    try:
        return int(value) if value not in (None, '') else None
    except (TypeError, ValueError):
        return None


def _decode_metadata(blob):
    """Return parsed metadata dict or None for empty/invalid input."""
    if not blob:
        return None
    try:
        parsed = json.loads(blob)
    except (TypeError, ValueError):
        return None
    return parsed if isinstance(parsed, dict) and parsed else None


@activity_bp.route('/')
@login_required
@admin_required
def index():
    booking_id = _parse_int(request.args.get('booking_id'))
    invoice_id = _parse_int(request.args.get('invoice_id'))
    action     = (request.args.get('action') or '').strip()
    actor_type = (request.args.get('actor_type') or '').strip()

    query = ActivityLog.query
    if booking_id is not None:
        query = query.filter(ActivityLog.booking_id == booking_id)
    if invoice_id is not None:
        query = query.filter(ActivityLog.invoice_id == invoice_id)
    if action:
        query = query.filter(ActivityLog.action == action)
    if actor_type:
        query = query.filter(ActivityLog.actor_type == actor_type)

    entries = (
        query.order_by(ActivityLog.created_at.desc())
             .limit(_RESULT_LIMIT)
             .all()
    )

    return render_template(
        'activity/index.html',
        entries=entries,
        decode_metadata=_decode_metadata,
        booking_id=booking_id,
        invoice_id=invoice_id,
        action_filter=action,
        actor_type_filter=actor_type,
        result_limit=_RESULT_LIMIT,
    )
