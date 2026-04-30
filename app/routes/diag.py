"""/admin/diag — admin-only deployment proof.

Tells the operator EXACTLY what's running on the box: the deployed git
SHA, the brand row that drives the displayed property name, the URL
that login redirects to, the sidebar departments, and whether the
design-system stylesheet is present on disk. Designed for the operator
to point at a browser tab and confirm "yes, the sprint landed".

No DB writes, no external calls. Read-only.
"""

from __future__ import annotations

import os
import pathlib

from flask import Blueprint, render_template, abort, current_app, jsonify
from flask_login import login_required, current_user


diag_bp = Blueprint('diag', __name__)


@diag_bp.route('/healthz')
def healthz():
    """Public deploy probe — no auth, returns JSON.

    Designed for `curl https://<staging-host>/healthz` — gives the
    operator instant proof of:
      - which git commit is running
      - what brand name is currently displayed (i.e. the override
        chain has resolved correctly)
      - whether STAGING=1 is set
      - which endpoint login redirects to

    No PII, no credentials, no DB row IDs — safe to expose.
    """
    from ..services.version import deployed_sha, is_staging
    from ..services.branding import get_brand
    from flask import url_for

    b = get_brand()
    try:
        login_redirect = url_for('dashboard.index')
    except Exception:
        login_redirect = None

    return jsonify({
        'status':            'ok',
        'sha':               deployed_sha(short=True),
        'sha_full':          deployed_sha(short=False),
        'staging':           is_staging(),
        'brand_name':        b.get('name'),
        'brand_short_name':  b.get('short_name'),
        'login_redirect':    login_redirect,
        'design_system_css': '/static/css/design-system.css',
    })


@diag_bp.route('/admin/diag')
@login_required
def index():
    if not current_user.is_admin:
        abort(403)

    from ..services.version import deployed_sha, is_staging
    from ..services.property_settings import get_settings

    s = get_settings()
    static_root = pathlib.Path(current_app.root_path) / 'static'
    css_path = static_root / 'css' / 'design-system.css'
    css_present = css_path.exists()
    css_size = css_path.stat().st_size if css_present else 0

    # Sidebar structure expected after the IA cleanup. The template
    # renders this list — operator can verify it visually matches the
    # actual sidebar in the same browser tab.
    sidebar_groups = [
        ('Dashboard',     ['dashboard.index']),
        ('Front Office',  ['board.index', 'front_office.arrivals',
                           'front_office.departures', 'front_office.in_house',
                           'bookings.index', 'guests.index',
                           'groups.index', 'whatsapp.inbox']),
        ('Housekeeping',  ['housekeeping.index', 'rooms.index']),
        ('Restaurant',    ['pos.terminal', 'menu.admin_queue',
                           'pos.admin_overview']),
        ('Accounting',    ['accounting.dashboard', 'invoices.index',
                           'accounting.expenses', 'accounting.pl',
                           'accounting.reconciliation', 'accounting.tax',
                           'accounting.reports', 'reports.overview',
                           'night_audit.index']),
        ('Admin',         ['property.inspect', 'property_settings.edit',
                           'inventory.overview', 'channels.index',
                           'auth.admin_users', 'activity.index',
                           'auth.test_whatsapp', 'auth.seed']),
    ]

    # Resolve each endpoint to a URL — broken endpoints surface clearly.
    from flask import url_for
    sidebar_resolved = []
    for label, eps in sidebar_groups:
        rows = []
        for ep in eps:
            try:
                rows.append((ep, url_for(ep)))
            except Exception as exc:
                rows.append((ep, f'(error: {exc.__class__.__name__})'))
        sidebar_resolved.append((label, rows))

    # Login post-redirect endpoint — what auth.admin_login + console_login
    # send the user to.
    try:
        login_redirect_url = url_for('dashboard.index')
    except Exception:
        login_redirect_url = '(dashboard.index missing)'

    info = {
        'git_sha':              deployed_sha(short=False),
        'git_sha_short':        deployed_sha(short=True),
        'staging':              is_staging(),
        'login_redirect':       login_redirect_url,
        'brand_row_id':         s.id,
        'brand_property_name':  s.property_name,
        'brand_short_name':     s.short_name,
        'brand_tagline':        s.tagline,
        'brand_primary_color':  s.primary_color,
        'brand_updated_at':     s.updated_at,
        'css_present':          css_present,
        'css_path':             str(css_path),
        'css_size_kb':          round(css_size / 1024, 1),
        'sidebar':              sidebar_resolved,
        'env_brand_name':       os.environ.get('BRAND_NAME') or '(unset)',
        'env_app_git_sha':      os.environ.get('APP_GIT_SHA') or '(unset)',
        'env_staging':          os.environ.get('STAGING') or '(unset)',
    }
    return render_template('diag/index.html', info=info)
