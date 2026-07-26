"""Booking Engine V2 — admin holds panel (spec §4).

Lists active selection holds + pending holds with time remaining and guest/
contact where known. Per-row audited actions: release-now (reason required),
extend, and confirm (pending → confirmed + auto-assignment). Admin-only.
"""

from __future__ import annotations

from datetime import datetime

from flask import (Blueprint, render_template, redirect, url_for, flash,
                   request)
from flask_login import login_required, current_user

from ..decorators import admin_required
from ..services import holds

holds_admin_bp = Blueprint('holds_admin', __name__, url_prefix='/admin/holds')


@holds_admin_bp.route('/')
@login_required
@admin_required
def index():
    now = datetime.utcnow()
    rows = []
    for h in holds.active_holds(now=now):
        remaining = int((h.expires_at - now).total_seconds())
        rows.append({
            'hold': h,
            'remaining_seconds': max(0, remaining),
            'remaining_label': _fmt_remaining(remaining),
            'type_name': (h.room_type.name if h.room_type else h.room_type_id),
            'guest': h.guest_name or (h.lead_guest.full_name
                                      if h.lead_guest else '—'),
            'contact': h.contact or '—',
        })
    return render_template('holds/index.html', rows=rows, now=now)


@holds_admin_bp.route('/<int:hold_id>/release', methods=['POST'])
@login_required
@admin_required
def release(hold_id):
    res = holds.release_hold(hold_id,
                             reason=request.form.get('reason', ''),
                             user_id=current_user.id)
    flash('Hold released.' if res['ok']
          else '; '.join(res.get('reasons', ['Could not release.'])),
          'success' if res['ok'] else 'error')
    return redirect(url_for('holds_admin.index'))


@holds_admin_bp.route('/<int:hold_id>/extend', methods=['POST'])
@login_required
@admin_required
def extend(hold_id):
    minutes = request.form.get('minutes', type=int)
    res = holds.extend_hold(hold_id, minutes=minutes, user_id=current_user.id)
    flash('Hold extended.' if res['ok']
          else '; '.join(res.get('reasons', ['Could not extend.'])),
          'success' if res['ok'] else 'error')
    return redirect(url_for('holds_admin.index'))


@holds_admin_bp.route('/<int:hold_id>/confirm', methods=['POST'])
@login_required
@admin_required
def confirm(hold_id):
    res = holds.confirm_pending(hold_id, user_id=current_user.id)
    flash('Pending confirmed — rooms auto-assigned.' if res['ok']
          else '; '.join(res.get('reasons', ['Could not confirm.'])),
          'success' if res['ok'] else 'error')
    return redirect(url_for('holds_admin.index'))


def _fmt_remaining(seconds):
    if seconds <= 0:
        return 'expiring…'
    m, s = divmod(seconds, 60)
    h, m = divmod(m, 60)
    if h:
        return f'{h}h {m}m'
    return f'{m}m {s}s'
