"""Reports & Analytics V1 — admin-only dashboard.

Four pages, all admin-required:

    GET /reports/             — overview (operations + headline numbers)
    GET /reports/revenue      — revenue tab (charges, payments, by-day)
    GET /reports/occupancy    — occupancy tab (per-day occupancy %)
    GET /reports/outstanding  — outstanding balances list

All four accept the same date-range query string:
    ?range=today | yesterday | week | month | custom
    ?start=YYYY-MM-DD&end=YYYY-MM-DD     (only used when range=custom)

No ActivityLog rows are written for page views — per spec, only
exports / important management actions warrant audit rows, and V1
has neither.

No WhatsApp / email / Gemini side effects — pure DB reads.
"""

from __future__ import annotations

from datetime import date

from flask import Blueprint, render_template, request
from flask_login import login_required

from ..decorators import admin_required
from ..services.reports import (
    resolve_range,
    operations_summary, pending_payment_summary,
    revenue_summary, revenue_by_day,
    outstanding_balances,
    occupancy_for_day, occupancy_summary,
)


reports_bp = Blueprint('reports', __name__, url_prefix='/reports')


def _range_from_request():
    return resolve_range(
        preset=request.args.get('range'),
        start_str=request.args.get('start'),
        end_str=request.args.get('end'),
    )


# ── GET /reports/ ───────────────────────────────────────────────────

@reports_bp.route('/', methods=['GET'])
@login_required
@admin_required
def overview():
    rng = _range_from_request()
    ops    = operations_summary()
    pend   = pending_payment_summary()
    rev    = revenue_summary(rng)
    occ_today = occupancy_for_day(date.today())

    return render_template(
        'reports/overview.html',
        active_tab='overview', rng=rng,
        ops=ops, pend=pend, rev=rev, occ_today=occ_today,
    )


# ── GET /reports/revenue ────────────────────────────────────────────

@reports_bp.route('/revenue', methods=['GET'])
@login_required
@admin_required
def revenue():
    rng = _range_from_request()
    rev = revenue_summary(rng)
    series = revenue_by_day(rng)

    # Y-axis max for the SVG bar chart
    max_y = max(
        [r['room_revenue'] + r['ancillary'] for r in series] + [1.0]
    )

    return render_template(
        'reports/revenue.html',
        active_tab='revenue', rng=rng, rev=rev,
        series=series, max_y=max_y,
    )


# ── GET /reports/occupancy ──────────────────────────────────────────

@reports_bp.route('/occupancy', methods=['GET'])
@login_required
@admin_required
def occupancy():
    rng = _range_from_request()
    summary = occupancy_summary(rng)

    return render_template(
        'reports/occupancy.html',
        active_tab='occupancy', rng=rng, summary=summary,
    )


# ── GET /reports/outstanding ────────────────────────────────────────

@reports_bp.route('/outstanding', methods=['GET'])
@login_required
@admin_required
def outstanding():
    rng = _range_from_request()  # not used for the list, but kept
                                   # for tab-state consistency
    rows = outstanding_balances(limit=200)
    pend = pending_payment_summary()
    grand_total = round(sum(r['balance'] for r in rows), 2)

    return render_template(
        'reports/outstanding.html',
        active_tab='outstanding', rng=rng,
        rows=rows, pend=pend, grand_total=grand_total,
    )
