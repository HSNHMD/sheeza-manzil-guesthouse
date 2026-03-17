from flask import Blueprint, render_template, redirect, url_for, flash, request
from flask_login import login_user, logout_user, login_required, current_user
from ..models import db, User

auth_bp = Blueprint('auth', __name__)


@auth_bp.route('/')
@login_required
def index():
    return redirect(url_for('rooms.index'))


@auth_bp.route('/login', methods=['GET', 'POST'])
def login():
    if current_user.is_authenticated:
        return redirect(url_for('auth.index'))

    if request.method == 'POST':
        username = request.form.get('username', '').strip()
        password = request.form.get('password', '')
        remember = bool(request.form.get('remember'))

        user = User.query.filter_by(username=username).first()
        if user and user.is_active and user.check_password(password):
            login_user(user, remember=remember)
            next_page = request.args.get('next')
            return redirect(next_page or url_for('auth.index'))
        flash('Invalid username or password.', 'error')

    return render_template('auth/login.html')


@auth_bp.route('/logout')
@login_required
def logout():
    logout_user()
    return redirect(url_for('auth.login'))


@auth_bp.route('/staff', methods=['GET', 'POST'])
@login_required
def staff():
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
                return redirect(url_for('auth.staff'))

        elif action == 'toggle':
            user_id = request.form.get('user_id')
            user = User.query.get_or_404(user_id)
            if user.id != current_user.id:
                user.is_active = not user.is_active
                db.session.commit()
                flash(f'User {user.username} {"activated" if user.is_active else "deactivated"}.', 'success')
            return redirect(url_for('auth.staff'))

    return render_template('auth/staff.html', users=users)
