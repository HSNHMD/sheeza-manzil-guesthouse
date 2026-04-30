import os
from flask import Blueprint, render_template, redirect, url_for, flash, request
from flask_login import login_user, logout_user, login_required, current_user
from ..models import db, User, Room

auth_bp = Blueprint('auth', __name__)


# ── Admin login (/appadmin) ──────────────────────────────────────────────────

# Allow-list of post-login `?next=` paths. flask-login auto-appends
# ?next=<original-path> when an unauthenticated user hits a protected
# route, but if the original path was /rooms/ (the legacy default
# landing) the user ends up RIGHT BACK on /rooms/ after login. We
# ignore /rooms/ specifically (and the empty string) so the post-
# login experience is consistent and role-aware.
_BANNED_NEXT_PATHS = ('', '/', '/rooms/', '/rooms', '/staff/dashboard')


def _post_login_target(user=None) -> str:
    """Return the URL the user should land on after successful login.

    Resolution order:
      1. Honour an explicit `?next=` query param IF it's a relative
         same-origin path AND not in the banned-defaults list.
      2. Otherwise dispatch via services.landing.landing_url_for
         (admin → Dashboard, dept staff → dept home, fallback →
         Dashboard).
    """
    from ..services.landing import landing_url_for

    nxt = (request.args.get('next') or '').strip()
    if nxt and nxt.startswith('/') and not nxt.startswith('//') \
            and nxt not in _BANNED_NEXT_PATHS:
        return nxt
    return landing_url_for(user)


@auth_bp.route('/appadmin', methods=['GET', 'POST'])
def admin_login():
    # IA cleanup: both admin and staff land on the unified Dashboard
    # after login. The previous behaviour bounced admins to
    # rooms.index (Rooms list) which is not a real dashboard. Staff
    # can still reach /staff/dashboard via direct URL but the
    # post-login default is the cross-department command center.
    if current_user.is_authenticated:
        return redirect(_post_login_target(current_user))

    if request.method == 'POST':
        username = request.form.get('username', '').strip()
        password = request.form.get('password', '')
        remember = bool(request.form.get('remember'))

        user = User.query.filter_by(username=username).first()
        if user and user.is_active and user.check_password(password):
            login_user(user, remember=remember)
            return redirect(_post_login_target(user))
        flash('Invalid username or password.', 'error')

    return render_template('auth/login.html')


# ── Staff login (/console) ───────────────────────────────────────────────────

@auth_bp.route('/console', methods=['GET', 'POST'])
def console_login():
    if current_user.is_authenticated:
        return redirect(_post_login_target(current_user))

    if request.method == 'POST':
        username = request.form.get('username', '').strip()
        password = request.form.get('password', '')
        user = User.query.filter_by(username=username).first()
        if user and user.is_active and user.check_password(password):
            login_user(user)
            return redirect(_post_login_target(user))
        flash('Invalid username or password.', 'error')

    return render_template('staff/login.html')


# ── Shared logout ────────────────────────────────────────────────────────────

@auth_bp.route('/logout')
@login_required
def logout():
    logout_user()
    return redirect(url_for('auth.admin_login'))


# ── User management (/admin/users) ───────────────────────────────────────────

@auth_bp.route('/admin/users', methods=['GET', 'POST'])
@login_required
def admin_users():
    if not current_user.is_admin:
        flash('Admin access required.', 'error')
        return redirect(url_for('auth.admin_login'))

    users = User.query.order_by(User.created_at.desc()).all()

    if request.method == 'POST':
        action = request.form.get('action')

        if action == 'create':
            username = request.form.get('username', '').strip()
            email = request.form.get('email', '').strip()
            password = request.form.get('password', '')
            role = request.form.get('role', 'staff')
            department = (request.form.get('department') or '').strip() or None
            allowed = {slug for slug, _ in User.DEPARTMENTS}
            if department and department not in allowed:
                department = None  # silently ignore unknown values

            if User.query.filter_by(username=username).first():
                flash('Username already taken.', 'error')
            elif User.query.filter_by(email=email).first():
                flash('Email already in use.', 'error')
            else:
                user = User(username=username, email=email, role=role,
                            department=department)
                user.set_password(password)
                db.session.add(user)
                db.session.commit()
                flash(f'Staff member {username} created.', 'success')
            return redirect(url_for('auth.admin_users'))

        elif action == 'set_department':
            user_id = request.form.get('user_id')
            user = User.query.get_or_404(user_id)
            new_dept = (request.form.get('department') or '').strip() or None
            allowed = {slug for slug, _ in User.DEPARTMENTS}
            if new_dept and new_dept not in allowed:
                flash('Unknown department.', 'error')
            else:
                user.department = new_dept
                db.session.commit()
                label = next((l for s, l in User.DEPARTMENTS
                              if s == new_dept), '— none —')
                flash(f'{user.username} department: {label}.', 'success')
            return redirect(url_for('auth.admin_users'))

        elif action == 'toggle':
            user_id = request.form.get('user_id')
            user = User.query.get_or_404(user_id)
            if user.id != current_user.id:
                user.is_active = not user.is_active
                db.session.commit()
                flash(f'User {user.username} {"activated" if user.is_active else "deactivated"}.', 'success')
            return redirect(url_for('auth.admin_users'))

        elif action == 'delete':
            user_id = request.form.get('user_id')
            user = User.query.get_or_404(user_id)
            if user.id == current_user.id:
                flash('You cannot delete your own account.', 'error')
            else:
                db.session.delete(user)
                db.session.commit()
                flash(f'User {user.username} deleted.', 'success')
            return redirect(url_for('auth.admin_users'))

        elif action == 'set_password':
            user_id = request.form.get('user_id')
            new_password = request.form.get('new_password', '')
            user = User.query.get_or_404(user_id)
            if len(new_password) < 6:
                flash('Password must be at least 6 characters.', 'error')
            else:
                user.set_password(new_password)
                db.session.commit()
                flash(f'Password updated for {user.username}.', 'success')
            return redirect(url_for('auth.admin_users'))

    return render_template('auth/staff.html', users=users)


# ── Change own password (/account/change-password) ──────────────────────────

@auth_bp.route('/account/change-password', methods=['GET', 'POST'])
@login_required
def change_password():
    if request.method == 'POST':
        current_pw = request.form.get('current_password', '')
        new_pw = request.form.get('new_password', '')
        confirm_pw = request.form.get('confirm_password', '')

        if not current_user.check_password(current_pw):
            flash('Current password is incorrect.', 'error')
        elif len(new_pw) < 6:
            flash('New password must be at least 6 characters.', 'error')
        elif new_pw != confirm_pw:
            flash('New passwords do not match.', 'error')
        else:
            current_user.set_password(new_pw)
            db.session.commit()
            flash('Password changed successfully.', 'success')
            return redirect(_post_login_target(current_user))

    return render_template('auth/change_password.html')


# ── Seed rooms (/admin/seed) ─────────────────────────────────────────────────

SEED_ROOMS = [
    ('1', 'Deluxe Double', 'Deluxe',   0,     2,        600.0),
    ('2', 'Deluxe Double', 'Deluxe',   0,     2,        600.0),
    ('3', 'Deluxe Double', 'Deluxe',   0,     2,        600.0),
    ('4', 'Deluxe Double', 'Deluxe',   0,     2,        600.0),
    ('5', 'Deluxe Double', 'Deluxe',   1,     2,        600.0),
    ('6', 'Deluxe Double', 'Deluxe',   1,     2,        600.0),
    ('7', 'Twin Room',     'Twin',     1,     2,        600.0),
    ('8', 'Twin Room',     'Twin',     0,     2,        600.0),
]


@auth_bp.route('/admin/seed', methods=['GET', 'POST'])
@login_required
def seed():
    if not current_user.is_admin:
        flash('Admin access required.', 'error')
        return redirect(url_for('auth.admin_login'))

    existing = Room.query.count()
    seeded = []
    skipped = []

    if request.method == 'POST':
        for number, name, rtype, floor, cap, price in SEED_ROOMS:
            if Room.query.filter_by(number=number).first():
                skipped.append(number)
            else:
                room = Room(
                    number=number, name=name, room_type=rtype,
                    floor=floor, capacity=cap, price_per_night=price,
                    amenities='WiFi, AC, TV, En-suite Bathroom'
                )
                db.session.add(room)
                seeded.append(number)
        db.session.commit()

        if seeded:
            flash(f'Seeded {len(seeded)} room(s): {", ".join(seeded)}.', 'success')
        if skipped:
            flash(f'Skipped {len(skipped)} already-existing room(s): {", ".join(skipped)}.', 'info')
        if not seeded and not skipped:
            flash('Nothing to seed.', 'info')
        return redirect(url_for('auth.seed'))

    return render_template('auth/seed.html', existing=existing, seed_rooms=SEED_ROOMS)


# ── WhatsApp test (/admin/test-whatsapp) ─────────────────────────────────────

@auth_bp.route('/admin/test-whatsapp')
@login_required
def test_whatsapp():
    if not current_user.is_admin:
        flash('Admin access required.', 'error')
        return redirect(url_for('auth.admin_login'))

    from ..services.whatsapp import _send, _send_template, _config_status, STAFF_PHONE

    config  = _config_status()
    result  = None
    action  = request.args.get('action', '')

    if action == 'send_text':
        result = _send(STAFF_PHONE, 'Test message from Sheeza Manzil Guesthouse system ✅ WhatsApp integration is working.')
        result['_action'] = 'Free-form text to ' + STAFF_PHONE

    elif action == 'send_template':
        tpl = request.args.get('tpl', 'booking_confirmed')
        result = _send_template(
            STAFF_PHONE, tpl,
            ['Test Guest', 'BKTEST01', 'Room 1 — Deluxe',
             '25 March 2026', '27 March 2026', '1200'],
            pending_approval=False,
        )
        result['_action'] = f'Template "{tpl}" to {STAFF_PHONE}'

    env_debug = {
        'WHATSAPP_ENABLED':         os.environ.get('WHATSAPP_ENABLED', '(not set)'),
        'WHATSAPP_TOKEN':           ('set (' + os.environ.get('WHATSAPP_TOKEN','')[:8] + '…)') if os.environ.get('WHATSAPP_TOKEN') else '(not set)',
        'WHATSAPP_PHONE_NUMBER_ID': os.environ.get('WHATSAPP_PHONE_NUMBER_ID', '(not set)'),
        'WHATSAPP_PHONE_ID':        os.environ.get('WHATSAPP_PHONE_ID', '(not set)'),
    }

    btn = 'background:#2563eb;color:white;padding:0.5rem 1rem;border-radius:6px;text-decoration:none;margin-right:8px;display:inline-block'
    html  = '<div style="font-family:monospace;max-width:760px;margin:2rem auto;padding:1.5rem">'
    html += '<h2 style="margin-bottom:1rem">WhatsApp Integration Test</h2>'

    html += '<h3>Environment Variables</h3>'
    html += '<pre style="background:#f3f4f6;padding:1rem;border-radius:8px;font-size:13px">'
    for k, v in env_debug.items():
        html += f'{k} = {v}\n'
    html += '</pre>'

    html += '<h3>Service Config</h3>'
    html += '<pre style="background:#f3f4f6;padding:1rem;border-radius:8px;font-size:13px">'
    for k, v in config.items():
        html += f'{k} = {v}\n'
    html += '</pre>'

    if result:
        color = '#d1fae5' if result.get('success') else '#fee2e2'
        html += f'<h3>Result: {result.get("_action","")}</h3>'
        html += f'<pre style="background:{color};padding:1rem;border-radius:8px;font-size:13px">'
        for k, v in result.items():
            if k != '_action':
                html += f'{k}: {v}\n'
        html += '</pre>'

    html += '<h3>Actions</h3><div style="margin-bottom:1rem">'
    html += f'<a href="?action=send_text" style="{btn}">Send free-form text</a>'
    html += f'<a href="?action=send_template&tpl=booking_confirmed" style="{btn}">Test booking_confirmed</a>'
    html += f'<a href="?action=send_template&tpl=booking_received" style="{btn}">Test booking_received</a>'
    html += f'<a href="?action=send_template&tpl=staff_new_booking" style="{btn}">Test staff_new_booking</a>'
    html += '</div>'
    html += f'<p><a href="{url_for("dashboard.index")}">← Back to dashboard</a></p></div>'
    return html
