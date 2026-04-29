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
    from .routes.activity import activity_bp
    from .routes.whatsapp_webhook import whatsapp_bp
    from .routes.folios import folios_bp
    from .routes.reservation_board import board_bp
    from .routes.front_office import front_office_bp
    from .routes.cashiering import cashiering_bp
    from .routes.night_audit import night_audit_bp

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
    app.register_blueprint(activity_bp)
    app.register_blueprint(whatsapp_bp)
    app.register_blueprint(folios_bp)
    app.register_blueprint(board_bp)
    app.register_blueprint(front_office_bp)
    app.register_blueprint(cashiering_bp)
    app.register_blueprint(night_audit_bp)

    # Register the business-date context processor so every template
    # can read {{ business_date }} without explicit passthrough.
    # Defaults to today's date if the singleton row is missing
    # (defensive — surfaces as a Night Audit blocker).
    @app.context_processor
    def _inject_business_date():
        from .services.night_audit import current_business_date
        try:
            return {'business_date': current_business_date()}
        except Exception:
            return {'business_date': None}

    from flask import request, redirect
    from flask_login import current_user

    @app.before_request
    def _staff_guard():
        # Only intercept authenticated non-admin (staff) users
        if not current_user.is_authenticated or current_user.is_admin:
            return
        allowed = ('/staff', '/console', '/appadmin', '/logout', '/static',
                   '/public', '/privacy', '/account', '/admin/activity',
                   '/admin/whatsapp', '/webhooks/whatsapp')
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

    # Register booking-lifecycle Jinja helpers so templates can call
    # status_label() and status_badge() directly. See app/booking_lifecycle.py
    # for the canonical vocabularies and the (booking, payment) pair matrix.
    from .booking_lifecycle import register_jinja_helpers
    register_jinja_helpers(app)

    # Register the brand context processor so templates can read
    # {{ brand.name }} / {{ brand.short_name }} / etc. without explicit
    # passthrough. Defaults to Sheeza Manzil values; staging/demo
    # environments override via BRAND_* env vars. See app/services/branding.py.
    from .services.branding import register_context_processor as _register_brand_ctx
    _register_brand_ctx(app)

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
