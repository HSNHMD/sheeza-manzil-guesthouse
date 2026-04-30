"""Unified Dashboard — post-login landing page.

Single entry point for both admin and staff after login. Surfaces
operational KPIs, recent activity, and quick action links into the
main department modules. See app/services/dashboard.py for the
canonical sources of every number.

The route is `login_required` (anyone signed in can see the
dashboard); fields the user has no permission to act on simply
render with greyed-out quick links. The `_staff_guard` whitelist
in app/__init__.py allows `/dashboard/*` so staff users land here
on login rather than being bounced to /staff/dashboard.
"""

from __future__ import annotations

from flask import Blueprint, render_template
from flask_login import login_required, current_user

from ..services.dashboard import (
    operational_snapshot, recent_activity, new_orders, recent_messages,
    quick_links,
)


dashboard_bp = Blueprint('dashboard', __name__, url_prefix='/dashboard')


@dashboard_bp.route('/', methods=['GET'])
@login_required
def index():
    snap = operational_snapshot()
    activity = recent_activity(limit=12)
    orders = new_orders(limit=5)
    messages = recent_messages(limit=5)
    links = quick_links(is_admin=current_user.is_admin)

    # Group quick links by their `group` key for the template
    links_by_group = {}
    for link in links:
        links_by_group.setdefault(link['group'], []).append(link)

    return render_template(
        'dashboard/index.html',
        snap=snap,
        activity=activity,
        new_orders_list=orders,
        messages=messages,
        links_by_group=links_by_group,
    )
