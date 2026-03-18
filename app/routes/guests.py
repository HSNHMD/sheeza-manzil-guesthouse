from flask import Blueprint, render_template, redirect, url_for, flash, request
from flask_login import login_required
from ..models import db, Guest

guests_bp = Blueprint('guests', __name__, url_prefix='/guests')


@guests_bp.route('/<int:guest_id>/edit', methods=['GET', 'POST'])
@login_required
def edit(guest_id):
    guest = Guest.query.get_or_404(guest_id)

    if request.method == 'POST':
        guest.first_name  = request.form.get('first_name', '').strip()
        guest.last_name   = request.form.get('last_name', '').strip()
        guest.phone       = request.form.get('phone', '').strip()
        guest.email       = request.form.get('email', '').strip()
        guest.nationality = request.form.get('nationality', '').strip()
        guest.id_type     = request.form.get('id_type', '').strip()
        guest.id_number   = request.form.get('id_number', '').strip()
        guest.address     = request.form.get('address', '').strip()
        guest.notes       = request.form.get('notes', '').strip()
        db.session.commit()
        flash(f'Guest {guest.full_name} updated.', 'success')

        # Return to the booking that linked here, if provided
        next_url = request.form.get('next') or request.args.get('next')
        if next_url:
            return redirect(next_url)
        return redirect(url_for('guests.edit', guest_id=guest_id))

    next_url = request.args.get('next', '')
    return render_template('guests/edit.html', guest=guest, next_url=next_url)
