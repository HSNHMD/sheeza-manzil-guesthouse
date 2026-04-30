"""Property Settings / Branding Foundation V1 — admin route.

Single admin-only screen that reads + writes the property_settings
singleton row.

  GET  /admin/property-settings   form, prefilled
  POST /admin/property-settings   save (audit row written on change)

This is the only place that mutates property_settings. All read paths
go through services.property_settings (or the brand context
processor for templates).
"""

from __future__ import annotations

from flask import (
    Blueprint, render_template, request, redirect, url_for, flash,
)
from flask_login import login_required, current_user

from ..models import db
from ..decorators import admin_required
from ..services.property_settings import (
    get_settings, settings_to_dict, update_settings,
)


property_settings_bp = Blueprint(
    'property_settings', __name__, url_prefix='/admin/property-settings',
)


@property_settings_bp.route('/', methods=['GET'])
@login_required
@admin_required
def edit():
    s = get_settings()
    return render_template(
        'property_settings/edit.html',
        settings=s,
        as_dict=settings_to_dict(s),
    )


@property_settings_bp.route('/', methods=['POST'])
@login_required
@admin_required
def save():
    result = update_settings(request.form, user=current_user)
    if not result['ok']:
        flash('Property settings: ' + result['error'], 'error')
        return redirect(url_for('property_settings.edit'))
    db.session.commit()
    n = len(result['changed_fields'])
    if n == 0:
        flash('No changes to save.', 'info')
    else:
        flash(
            f'Property settings saved ({n} field'
            f'{"s" if n != 1 else ""} updated).',
            'success')
    return redirect(url_for('property_settings.edit'))
