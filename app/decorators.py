"""Reusable view decorators.

Currently provides `@admin_required` which gates an endpoint to logged-in
users whose `role == 'admin'`. Staff (and any other non-admin role) get
a 403. Anonymous users get the standard `@login_required` redirect-to-login.

Usage:
    from app.decorators import admin_required

    @bookings_bp.route('/<int:booking_id>/payment/verify', methods=['POST'])
    @login_required
    @admin_required
    def verify_payment(booking_id):
        ...
"""

from functools import wraps

from flask import abort
from flask_login import current_user


def admin_required(f):
    """Refuse non-admins with 403. Use IN ADDITION TO @login_required."""

    @wraps(f)
    def wrapper(*args, **kwargs):
        if not current_user.is_authenticated:
            abort(401)
        if not getattr(current_user, 'is_admin', False):
            abort(403)
        return f(*args, **kwargs)

    return wrapper
