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

# Upload hardening (Pepper Phase 0). Slips/IDs are images or PDFs; nothing else
# is accepted. The bot's photo path amplifies this surface, so validate here —
# the single choke point every upload passes through.
ALLOWED_UPLOAD_EXTS = frozenset(
    {'jpg', 'jpeg', 'png', 'gif', 'webp', 'avif', 'heic', 'heif', 'pdf'})
MAX_UPLOAD_BYTES = 10 * 1024 * 1024  # 10 MB; mirrors config.MAX_CONTENT_LENGTH


class UploadRejected(ValueError):
    """An uploaded file failed validation (bad/missing extension, too large,
    or empty). Carries a guest-safe message; callers surface it, never 500."""


def _upload_ext(filename):
    """Lower-cased extension, or None when the filename is missing or has no
    extension (guards the old `rsplit('.',1)[1]` IndexError)."""
    if not filename or '.' not in filename:
        return None
    return filename.rsplit('.', 1)[1].strip().lower() or None


def _save_file(file, prefix, folder_type):
    """Validate then save an uploaded file locally AND to Cloudflare R2.

    Returns (filename, drive_id). drive_id is None when R2 is not configured or
    the upload fails — the app falls back to local serving (same semantics for
    booking/receipt/slip uploads). Raises ``UploadRejected`` when the file's
    extension is missing/disallowed, or the file is empty/over the size cap;
    the happy path (a valid image/PDF ≤10 MB) is unchanged.
    """
    ext = _upload_ext(getattr(file, 'filename', None))
    if ext not in ALLOWED_UPLOAD_EXTS:
        raise UploadRejected(
            'Unsupported file type — upload an image (JPG/PNG/…) or PDF.')
    file_bytes = file.read()
    if not file_bytes:
        raise UploadRejected('That file was empty — please re-upload.')
    if len(file_bytes) > MAX_UPLOAD_BYTES:
        raise UploadRejected('File too large — the maximum is 10 MB.')

    from ..services.drive import upload_file as drive_upload
    name = f'{prefix}_{uuid.uuid4().hex[:10]}.{ext}'
    upload_dir = os.path.join(current_app.root_path, 'uploads')
    os.makedirs(upload_dir, exist_ok=True)
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
