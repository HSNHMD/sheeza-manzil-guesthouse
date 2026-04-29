"""Night Audit V1 — admin-only screen + run route.

Two endpoints:

    GET  /admin/night-audit          — pre-check screen + history
    POST /admin/night-audit/run      — confirm + execute the close

Hard rules enforced:
    - login_required + admin_required
    - No auto-run; the human clicks the Run button after seeing the
      pre-check report.
    - Concurrent run guard via BusinessDateState.audit_in_progress
    - Blocking issues abort the run with NO state change.
    - V1 close ONLY rolls the business date and records the run row.
      It does NOT mutate booking.status, payment.status, or folio
      items. Phase 4+ work will add room-charge auto-posting and
      daily revenue snapshots.
    - No WhatsApp / email / Gemini side-effects.
"""

from __future__ import annotations

import json as _json
from datetime import datetime, timedelta

from flask import (
    Blueprint, render_template, request, redirect, url_for, flash, abort,
)
from flask_login import login_required, current_user

from ..models import db, BusinessDateState, NightAuditRun
from ..decorators import admin_required
from ..services.audit import log_activity
from ..services.night_audit import (
    get_business_date_state,
    run_pre_checks,
    split_by_severity,
    serialize_issues,
    SEVERITY_BLOCKING,
    SEVERITY_WARNING,
)


night_audit_bp = Blueprint('night_audit', __name__,
                            url_prefix='/admin/night-audit')


# ── GET /admin/night-audit — screen ─────────────────────────────────

@night_audit_bp.route('/', methods=['GET'])
@login_required
@admin_required
def index():
    """Show current business date, pre-check results, and run history."""
    state = get_business_date_state()
    business_date = state.current_business_date if state else None
    server_date   = datetime.utcnow().date()
    server_now    = datetime.utcnow()

    issues = run_pre_checks(business_date) if business_date else []
    blocking, warning = split_by_severity(issues)

    # Recent audit history — last 10
    history = (
        NightAuditRun.query
        .order_by(NightAuditRun.created_at.desc())
        .limit(10)
        .all()
    )

    next_business_date = (business_date + timedelta(days=1)) if business_date else None

    return render_template(
        'night_audit/index.html',
        state=state,
        business_date=business_date,
        next_business_date=next_business_date,
        server_date=server_date,
        server_now=server_now,
        clock_skew_days=(business_date - server_date).days if business_date else 0,
        issues=issues,
        blocking_issues=blocking,
        warning_issues=warning,
        can_run=(state is not None and len(blocking) == 0),
        history=history,
    )


# ── POST /admin/night-audit/run — execute the close ─────────────────

@night_audit_bp.route('/run', methods=['POST'])
@login_required
@admin_required
def run():
    """Execute the Night Audit close.

    Flow:
        1. Re-run pre-checks SERVER-SIDE (don't trust the screen).
        2. If any blocking issue → record a 'blocked' NightAuditRun
           row and refuse.
        3. Verify the operator typed the closing business date into
           the confirm field (defense against muscle-memory clicks).
        4. Set audit_in_progress=True (concurrent guard).
        5. Roll BusinessDateState.current_business_date forward by 1.
        6. Stamp last_audit_run_at + last_audit_run_by_user_id.
        7. Clear audit_in_progress.
        8. Create completed NightAuditRun row with summary snapshot.
        9. Two audit rows: night_audit.completed + business_date.rolled.

    Form input:
        confirm_date  — required; must equal current business date as ISO
        notes         — optional, ≤500 chars
    """
    state = get_business_date_state()
    if state is None:
        flash('Cannot run audit: business date state is missing.', 'error')
        return redirect(url_for('night_audit.index'))

    closing_date = state.current_business_date
    next_date    = closing_date + timedelta(days=1)

    # ── Re-run pre-checks server-side ──
    issues = run_pre_checks(closing_date)
    blocking, warning = split_by_severity(issues)

    if blocking:
        # Record the blocked attempt for audit trail
        blocked_run = NightAuditRun(
            business_date_closed=closing_date,
            next_business_date=next_date,
            run_by_user_id=getattr(current_user, 'id', None),
            status='blocked',
            summary_json=_json.dumps({
                'issues': serialize_issues(issues),
                'blocking_count': len(blocking),
                'warning_count':  len(warning),
            }),
            exception_count=len(blocking),
            warning_count=len(warning),
        )
        db.session.add(blocked_run)
        db.session.flush()

        log_activity(
            'night_audit.blocked',
            description=(
                f'Night Audit for {closing_date} blocked by '
                f'{len(blocking)} blocking issue'
                f'{"s" if len(blocking) != 1 else ""}.'
            ),
            metadata={
                'business_date_closed': closing_date.isoformat(),
                'next_business_date':   next_date.isoformat(),
                'blocking_issue_count': len(blocking),
                'warning_count':        len(warning),
                'run_by_user_id':       getattr(current_user, 'id', None),
                'audit_run_id':         blocked_run.id,
            },
        )
        db.session.commit()

        for issue in blocking:
            flash(f'Blocked: {issue.title} — {issue.detail}', 'error')
        return redirect(url_for('night_audit.index'))

    # ── Confirm-by-date guard ──
    confirm_raw = (request.form.get('confirm_date') or '').strip()
    if confirm_raw != closing_date.isoformat():
        flash(
            f'Confirmation date mismatch. Type "{closing_date.isoformat()}" '
            f'into the confirm field to close that day.',
            'error',
        )
        return redirect(url_for('night_audit.index'))

    notes = (request.form.get('notes') or '').strip() or None
    if notes and len(notes) > 500:
        notes = notes[:500]

    # ── Concurrent guard + roll forward ──
    if state.audit_in_progress:
        flash('Another Night Audit is already in progress.', 'error')
        return redirect(url_for('night_audit.index'))

    state.audit_in_progress = True
    state.audit_started_at = datetime.utcnow()
    state.audit_started_by_user_id = getattr(current_user, 'id', None)
    db.session.flush()

    # Audit-started log row
    log_activity(
        'night_audit.started',
        description=(
            f'Night Audit for {closing_date} started by '
            f'{getattr(current_user, "username", "unknown")}.'
        ),
        metadata={
            'business_date_closed': closing_date.isoformat(),
            'next_business_date':   next_date.isoformat(),
            'warning_count':        len(warning),
            'run_by_user_id':       getattr(current_user, 'id', None),
        },
    )

    # ── Commit the close ──
    state.current_business_date     = next_date
    state.last_audit_run_at         = datetime.utcnow()
    state.last_audit_run_by_user_id = getattr(current_user, 'id', None)
    state.audit_in_progress         = False
    state.audit_started_at          = None
    state.audit_started_by_user_id  = None

    completed_run = NightAuditRun(
        business_date_closed=closing_date,
        next_business_date=next_date,
        run_by_user_id=getattr(current_user, 'id', None),
        status='completed',
        completed_at=datetime.utcnow(),
        summary_json=_json.dumps({
            'issues': serialize_issues(issues),
            'blocking_count': 0,
            'warning_count':  len(warning),
        }),
        exception_count=0,
        warning_count=len(warning),
        notes=notes,
    )
    db.session.add(completed_run)
    db.session.flush()

    log_activity(
        'night_audit.completed',
        description=(
            f'Night Audit completed: {closing_date} closed → {next_date}. '
            f'{len(warning)} warning'
            f'{"s" if len(warning) != 1 else ""} acknowledged.'
        ),
        metadata={
            'business_date_closed': closing_date.isoformat(),
            'next_business_date':   next_date.isoformat(),
            'blocking_issue_count': 0,
            'warning_count':        len(warning),
            'run_by_user_id':       getattr(current_user, 'id', None),
            'audit_run_id':         completed_run.id,
        },
    )

    log_activity(
        'business_date.rolled',
        description=(
            f'Business date advanced from {closing_date} to {next_date}.'
        ),
        metadata={
            'business_date_closed': closing_date.isoformat(),
            'next_business_date':   next_date.isoformat(),
            'audit_run_id':         completed_run.id,
            'run_by_user_id':       getattr(current_user, 'id', None),
        },
    )
    db.session.commit()

    flash(
        f'Night Audit complete. Business date is now '
        f'{next_date.strftime("%a %b %-d, %Y")}.',
        'success',
    )
    return redirect(url_for('night_audit.index'))
