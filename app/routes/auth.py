import os
from flask import Blueprint, render_template, redirect, url_for, flash, request
from flask_login import login_user, logout_user, login_required, current_user
from ..models import db, User, Room

auth_bp = Blueprint('auth', __name__)


@auth_bp.route('/')
@login_required
def index():
    if not current_user.is_admin:
        return redirect(url_for('staff.dashboard'))
    return redirect(url_for('rooms.index'))


@auth_bp.route('/login', methods=['GET', 'POST'])
def login():
    if current_user.is_authenticated:
        if not current_user.is_admin:
            return redirect(url_for('staff.dashboard'))
        return redirect(url_for('rooms.index'))

    if request.method == 'POST':
        username = request.form.get('username', '').strip()
        password = request.form.get('password', '')
        remember = bool(request.form.get('remember'))

        user = User.query.filter_by(username=username).first()
        if user and user.is_active and user.check_password(password):
            login_user(user, remember=remember)
            next_page = request.args.get('next')
            if not next_page:
                next_page = url_for('staff.dashboard') if not user.is_admin else url_for('rooms.index')
            return redirect(next_page)
        flash('Invalid username or password.', 'error')

    return render_template('auth/login.html')


@auth_bp.route('/logout')
@login_required
def logout():
    logout_user()
    return redirect(url_for('auth.login'))


@auth_bp.route('/admin/users', methods=['GET', 'POST'])
@login_required
def admin_users():
    if not current_user.is_admin:
        flash('Admin access required.', 'error')
        return redirect(url_for('auth.index'))

    users = User.query.order_by(User.created_at.desc()).all()

    if request.method == 'POST':
        action = request.form.get('action')
        if action == 'create':
            username = request.form.get('username', '').strip()
            email = request.form.get('email', '').strip()
            password = request.form.get('password', '')
            role = request.form.get('role', 'staff')

            if User.query.filter_by(username=username).first():
                flash('Username already taken.', 'error')
            elif User.query.filter_by(email=email).first():
                flash('Email already in use.', 'error')
            else:
                user = User(username=username, email=email, role=role)
                user.set_password(password)
                db.session.add(user)
                db.session.commit()
                flash(f'Staff member {username} created.', 'success')
                return redirect(url_for('auth.admin_users'))

        elif action == 'toggle':
            user_id = request.form.get('user_id')
            user = User.query.get_or_404(user_id)
            if user.id != current_user.id:
                user.is_active = not user.is_active
                db.session.commit()
                flash(f'User {user.username} {"activated" if user.is_active else "deactivated"}.', 'success')
            return redirect(url_for('auth.admin_users'))

    return render_template('auth/staff.html', users=users)


SEED_ROOMS = [
    # (number, name,          type,    floor, capacity, price)
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
        return redirect(url_for('auth.index'))

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


@auth_bp.route('/admin/test-whatsapp')
@login_required
def test_whatsapp():
    if not current_user.is_admin:
        flash('Admin access required.', 'error')
        return redirect(url_for('auth.index'))

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
    html += f'<p><a href="{url_for("auth.index")}">← Back to dashboard</a></p></div>'
    return html
