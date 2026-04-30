"""Role-based landing dispatcher.

Single source of truth for "where does this user land after login?".
Called from auth.admin_login, auth.console_login, and any other place
that wants to land a user on their preferred home (e.g. the Cancel
buttons inside change_password). The dispatcher is pure with respect
to the request — it only inspects the user object, not Flask's
request/session — so it's trivially unit-testable.

Resolution order (first hit wins):

  1. Admin / manager-equivalent role → /dashboard/  (cross-department
     command center; admins always see the bird's-eye view).
  2. Non-admin user with a whitelisted department → that department's
     home (Front Office / Housekeeping / Restaurant / Accounting).
  3. Anything else → /dashboard/  (safe fallback that every
     authenticated user can render).

Adding a new department needs three coordinated edits:
  - app/models.py User.DEPARTMENTS tuple
  - this module's `_DEPT_LANDING`
  - admin/users UI dropdown in templates/auth/staff.html
"""

from __future__ import annotations

from typing import Optional, Tuple


# Department slug → endpoint name. The endpoint MUST exist in the
# Flask URL map; the dispatcher will fall back to the Dashboard if
# url_for raises (e.g. during tests with a partial blueprint set).
_DEPT_LANDING = {
    'front_office': 'front_office.index',
    'housekeeping': 'housekeeping.index',
    'restaurant':   'pos.terminal',
    'accounting':   'accounting.dashboard',
}

# Roles that always land on the cross-department command center,
# regardless of department setting. Includes 'admin' (today's only
# privileged role) plus 'manager' as a forward-compat slot.
_DASHBOARD_ROLES = frozenset(('admin', 'manager'))


def landing_endpoint_for(user) -> str:
    """Return the Flask endpoint name where this user should land.

    Pure function. No url_for call — the caller does the URL build
    so we can also use this from tests / diag without a request
    context.
    """
    if user is None:
        return 'dashboard.index'

    role = (getattr(user, 'role', '') or '').strip()
    if role in _DASHBOARD_ROLES:
        return 'dashboard.index'

    dept = (getattr(user, 'department', '') or '').strip()
    return _DEPT_LANDING.get(dept, 'dashboard.index')


def landing_url_for(user) -> str:
    """Return the URL where this user should land after login.

    Wraps `landing_endpoint_for` with `flask.url_for`. If the resolved
    endpoint is missing from the app (rare — usually a misconfigured
    blueprint), we fall back to `/dashboard/` rather than 500ing the
    login flow.
    """
    from flask import url_for

    endpoint = landing_endpoint_for(user)
    try:
        return url_for(endpoint)
    except Exception:
        # Missing endpoint? Fall back to Dashboard. Don't crash login.
        try:
            return url_for('dashboard.index')
        except Exception:
            return '/dashboard/'


def describe_landing(user) -> Tuple[str, str]:
    """Return (endpoint, human-readable label) for the resolved landing.

    Used by /admin/diag and /healthz so operators can see at a glance
    where a given account would land. Doesn't need a request context.
    """
    endpoint = landing_endpoint_for(user)
    return (endpoint, _LANDING_LABELS.get(endpoint, endpoint))


_LANDING_LABELS = {
    'dashboard.index':       'Dashboard',
    'front_office.index':    'Front Office',
    'housekeeping.index':    'Housekeeping Board',
    'pos.terminal':          'POS Terminal',
    'accounting.dashboard':  'Accounting Overview',
}
