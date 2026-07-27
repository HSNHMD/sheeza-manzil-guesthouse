"""Public site root.

The public booking flow is the Booking Engine V2 portal (see routes/portal.py).
The old single-room root flow (`/availability`, `/submit`) is RETIRED — it
bypassed the Phase 1 inventory/holds engine (an overbooking side door) and its
`/availability` JSON even leaked room numbers. The site root now serves the
portal; `/confirmation/<ref>` is kept for existing guest links.

`_save_file` lives here and is reused by the portal for slip uploads.
"""

import os
import uuid

from flask import Blueprint, render_template, current_app

from ..models import Booking

public_bp = Blueprint('public', __name__, url_prefix='')


def _save_file(file, prefix, folder_type):
    """Save an uploaded file locally AND to Cloudflare R2 (services.drive).

    Returns (filename, drive_id). drive_id is None when R2 is not configured or
    the upload fails — the app falls back to local serving (same semantics for
    booking/receipt/slip uploads).
    """
    from ..services.drive import upload_file as drive_upload
    ext = file.filename.rsplit('.', 1)[1].lower()
    name = f'{prefix}_{uuid.uuid4().hex[:10]}.{ext}'
    upload_dir = os.path.join(current_app.root_path, 'uploads')
    os.makedirs(upload_dir, exist_ok=True)
    file_bytes = file.read()
    with open(os.path.join(upload_dir, name), 'wb') as fh:
        fh.write(file_bytes)
    return name, drive_upload(file_bytes, name, folder_type)


@public_bp.route('/')
def index():
    """Site root IS the portal (guests need not know a path). `/book` is the
    same portal and renders identically; the root delegates to it (no dup, no
    redirect hop) so the URL bar stays clean."""
    from .portal import index as portal_index
    return portal_index()


@public_bp.route('/confirmation/<booking_ref>')
def confirmation(booking_ref):
    """Kept for existing guests' saved links — backed by the bookings table, as-is."""
    booking = Booking.query.filter_by(booking_ref=booking_ref).first_or_404()
    return render_template('public/confirmation.html', booking=booking)
