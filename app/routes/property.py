"""Multi-Property Foundation V1 — read-only inspect route.

Single admin-only page that surfaces the current Property record and
the count of property-scoped rows attached to it. Useful to confirm
the migration backfilled correctly and as a multi-property "what
property am I in?" indicator for operators.

Editing the rich settings (branding, bank, policies) still happens at
/admin/property-settings/. This page intentionally mirrors only the
property-foundation fields (code, name, timezone, currency, member
counts).
"""

from __future__ import annotations

from flask import Blueprint, render_template
from flask_login import login_required

from ..decorators import admin_required
from ..services.property import current_property, property_member_count


property_bp = Blueprint('property', __name__, url_prefix='/admin/property')


@property_bp.route('/', methods=['GET'])
@login_required
@admin_required
def inspect():
    prop = current_property()
    counts = property_member_count(prop)
    return render_template(
        'property/inspect.html',
        prop=prop,
        counts=counts,
    )
