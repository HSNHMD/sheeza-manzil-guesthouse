"""Unified Dashboard — operational command summary.

Pure read aggregation. Reuses the trustworthy helpers from
services.reports + services.audit so the dashboard can never drift
from the underlying reports module's canonical sources.

V1 surfaces:
  - operations counts (arrivals / departures / in-house / room states)
  - pending-payment + outstanding-balance KPIs
  - today's room occupancy
  - recent activity feed (last 10 cross-cutting events)
  - new guest orders awaiting confirmation
  - new WhatsApp messages awaiting reply (if module is in use)

The dashboard is the post-login landing for both admin and staff
roles. Quick links from the page push users into Front Office,
Housekeeping, POS, etc. so it functions as a true command center
rather than a feature list.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta


def operational_snapshot():
    """Return ops + occupancy + pending-payment KPIs for today.

    Single dict so the template can render every KPI from one
    namespace.
    """
    from .reports import (
        operations_summary, pending_payment_summary, occupancy_for_day,
    )

    today = date.today()
    ops = operations_summary(today)
    pend = pending_payment_summary()
    occ = occupancy_for_day(today)

    return {
        'as_of':            today,
        # Operations
        'arrivals_today':   ops['arrivals_today'],
        'departures_today': ops['departures_today'],
        'in_house':         ops['in_house'],
        # Rooms
        'rooms_total':      ops['rooms_total'],
        'occupied_rooms':   ops['occupied_rooms'],
        'vacant_rooms':     ops['vacant_rooms'],
        'dirty_rooms':      ops['dirty_rooms'],
        'out_of_order_rooms': ops['out_of_order_rooms'],
        # Occupancy
        'occupancy_pct':    occ['occupancy_pct'],
        # Money
        'pending_count':       pend['pending_count'],
        'outstanding_count':   pend['outstanding_count'],
        'outstanding_total':   pend['outstanding_total'],
    }


def recent_activity(limit: int = 10):
    """Most recent ActivityLog rows for the cross-cutting feed."""
    from ..models import ActivityLog
    return (ActivityLog.query
            .order_by(ActivityLog.created_at.desc())
            .limit(limit)
            .all())


def new_orders(limit: int = 5):
    """Pending guest orders (online menu) awaiting staff confirmation."""
    try:
        from ..models import GuestOrder
        return (GuestOrder.query
                .filter_by(status='new')
                .order_by(GuestOrder.created_at.desc())
                .limit(limit)
                .all())
    except Exception:
        return []


def recent_messages(limit: int = 5):
    """Recent inbound WhatsApp messages, newest first.

    Returns [] if WhatsApp module isn't yet active or the table
    doesn't exist."""
    try:
        from ..models import WhatsAppMessage
        return (WhatsAppMessage.query
                .filter_by(direction='inbound')
                .order_by(WhatsAppMessage.created_at.desc())
                .limit(limit)
                .all())
    except Exception:
        return []


def quick_links(*, is_admin: bool):
    """Return the ordered list of "what to do next" links shown on
    the dashboard. Department-grouped. Staff sees a smaller subset."""

    links = [
        # Front Office (everyone)
        {'group': 'Front Office', 'label': 'Reservation Board',
         'endpoint': 'board.index', 'admin_only': True,
         'icon': 'M4 6h16M4 10h16M4 14h16M4 18h16'},
        {'group': 'Front Office', 'label': 'Arrivals',
         'endpoint': 'front_office.arrivals',
         'icon': 'M11 16l-4-4m0 0l4-4m-4 4h14'},
        {'group': 'Front Office', 'label': 'Departures',
         'endpoint': 'front_office.departures',
         'icon': 'M13 8l4 4m0 0l-4 4m4-4H3'},
        {'group': 'Front Office', 'label': 'In House',
         'endpoint': 'front_office.in_house',
         'icon': 'M5 8h14M5 8a2 2 0 110-4h14a2 2 0 110 4M5 8v10a2 2 0 002 2h10a2 2 0 002-2V8'},
        # Housekeeping
        {'group': 'Housekeeping', 'label': 'Housekeeping Board',
         'endpoint': 'housekeeping.index',
         'icon': 'M5 3v4M3 5h4M6 17v4m-2-2h4m5-16l2.286 6.857L21 12l-5.714 2.143L13 21l-2.286-6.857L5 12l5.714-2.143L13 3z'},
        # Restaurant
        {'group': 'Restaurant', 'label': 'POS Terminal',
         'endpoint': 'pos.terminal',
         'icon': 'M3 10h18M7 14h.01M11 14h.01M15 14h.01M3 6h18'},
        {'group': 'Restaurant', 'label': 'Online Orders',
         'endpoint': 'menu.admin_queue', 'admin_only': True,
         'icon': 'M9 5H7a2 2 0 00-2 2v12a2 2 0 002 2h10a2 2 0 002-2V7a2 2 0 00-2-2h-2'},
        # Accounting (admin)
        {'group': 'Accounting', 'label': 'Night Audit',
         'endpoint': 'night_audit.index', 'admin_only': True,
         'icon': 'M12 8v4l3 3m6-3a9 9 0 11-18 0 9 9 0 0118 0z'},
        {'group': 'Accounting', 'label': 'Invoices',
         'endpoint': 'invoices.index', 'admin_only': True,
         'icon': 'M9 12h6m-6 4h6m2 5H7a2 2 0 01-2-2V5a2 2 0 012-2h5.586a1 1 0 01.707.293l5.414 5.414a1 1 0 01.293.707V19a2 2 0 01-2 2z'},
        {'group': 'Accounting', 'label': 'Reports & Analytics',
         'endpoint': 'reports.overview', 'admin_only': True,
         'icon': 'M9 19v-6a2 2 0 00-2-2H5a2 2 0 00-2 2v6a2 2 0 002 2h2a2 2 0 002-2z'},
    ]

    # Filter by role
    out = []
    for link in links:
        if link.get('admin_only') and not is_admin:
            continue
        out.append(link)
    return out
