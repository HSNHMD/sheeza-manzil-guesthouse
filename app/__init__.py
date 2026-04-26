import logging
import sys
from flask import Flask, render_template
from flask_login import LoginManager
from flask_migrate import Migrate
from .models import db, User, Room
from config import Config

# Ensure Python loggers write to stdout so Railway captures them
logging.basicConfig(
    stream=sys.stdout,
    level=logging.INFO,
    format='%(asctime)s %(name)s %(levelname)s %(message)s',
)

login_manager = LoginManager()
login_manager.login_view = 'auth.console_login'
login_manager.login_message_category = 'info'
migrate = Migrate()


@login_manager.user_loader
def load_user(user_id):
    return User.query.get(int(user_id))


def create_app(config_class=Config):
    app = Flask(__name__)
    app.config.from_object(config_class)

    db.init_app(app)
    migrate.init_app(app, db)
    login_manager.init_app(app)

    from .routes.auth import auth_bp
    from .routes.rooms import rooms_bp
    from .routes.bookings import bookings_bp
    from .routes.invoices import invoices_bp
    from .routes.housekeeping import housekeeping_bp
    from .routes.calendar import calendar_bp
    from .routes.guests import guests_bp
    from .routes.public import public_bp
    from .routes.accounting import accounting_bp
    from .routes.staff import staff_bp

    app.register_blueprint(auth_bp)
    app.register_blueprint(rooms_bp)
    app.register_blueprint(bookings_bp)
    app.register_blueprint(invoices_bp)
    app.register_blueprint(housekeeping_bp)
    app.register_blueprint(calendar_bp)
    app.register_blueprint(guests_bp)
    app.register_blueprint(public_bp)
    app.register_blueprint(accounting_bp)
    app.register_blueprint(staff_bp)

    from flask import request, redirect
    from flask_login import current_user

    @app.before_request
    def _staff_guard():
        # Only intercept authenticated non-admin (staff) users
        if not current_user.is_authenticated or current_user.is_admin:
            return
        allowed = ('/staff', '/console', '/appadmin', '/logout', '/static', '/public', '/privacy', '/account')
        if not any(request.path == p or request.path.startswith(p + '/') for p in allowed):
            return redirect('/staff/dashboard')

    @app.route('/privacy')
    def privacy():
        return render_template('privacy.html')

    # Register CLI commands (e.g. `flask admin create`, `flask admin reset-password`).
    # NOTE: there is intentionally NO automatic admin seeding. The initial admin
    # must be created explicitly via the CLI — no default credentials anywhere.
    from .cli import register_cli
    register_cli(app)

    with app.app_context():
        import os
        upload_dir = os.path.join(app.root_path, 'uploads')
        os.makedirs(upload_dir, exist_ok=True)
        try:
            _seed_rooms(app)
        except Exception as e:
            app.logger.warning('Room seeding skipped (tables not ready): %s', e)

    return app


def _seed_rooms(app):
    """Seed the 8 Sheeza Manzil rooms if the rooms table is empty."""
    if Room.query.count() > 0:
        return
    rooms = [
        # (number, name,          type,    floor, capacity, price)
        ('1', 'Deluxe Double', 'Deluxe', 0, 2, 600.0),
        ('2', 'Deluxe Double', 'Deluxe', 0, 2, 600.0),
        ('3', 'Deluxe Double', 'Deluxe', 0, 2, 600.0),
        ('4', 'Deluxe Double', 'Deluxe', 0, 2, 600.0),
        ('5', 'Deluxe Double', 'Deluxe', 1, 2, 600.0),
        ('6', 'Deluxe Double', 'Deluxe', 1, 2, 600.0),
        ('7', 'Twin Room',     'Twin',   1, 2, 600.0),
        ('8', 'Twin Room',     'Twin',   0, 2, 600.0),
    ]
    for number, name, rtype, floor, cap, price in rooms:
        db.session.add(Room(
            number=number, name=name, room_type=rtype,
            floor=floor, capacity=cap, price_per_night=price,
            amenities='WiFi, AC, TV, En-suite Bathroom',
        ))
    db.session.commit()
    app.logger.info('Seeded 8 rooms automatically.')
