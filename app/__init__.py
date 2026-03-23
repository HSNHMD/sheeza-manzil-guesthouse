from flask import Flask
from flask_login import LoginManager
from .models import db, User, Room
from config import Config

login_manager = LoginManager()
login_manager.login_view = 'auth.login'
login_manager.login_message_category = 'info'


@login_manager.user_loader
def load_user(user_id):
    return User.query.get(int(user_id))


def create_app(config_class=Config):
    app = Flask(__name__)
    app.config.from_object(config_class)

    db.init_app(app)
    login_manager.init_app(app)

    from .routes.auth import auth_bp
    from .routes.rooms import rooms_bp
    from .routes.bookings import bookings_bp
    from .routes.invoices import invoices_bp
    from .routes.housekeeping import housekeeping_bp
    from .routes.calendar import calendar_bp
    from .routes.guests import guests_bp
    from .routes.public import public_bp
    from .routes.admin import admin_bp

    app.register_blueprint(auth_bp)
    app.register_blueprint(rooms_bp)
    app.register_blueprint(bookings_bp)
    app.register_blueprint(invoices_bp)
    app.register_blueprint(housekeeping_bp)
    app.register_blueprint(calendar_bp)
    app.register_blueprint(guests_bp)
    app.register_blueprint(public_bp)
    app.register_blueprint(admin_bp)

    with app.app_context():
        import os
        upload_dir = os.path.join(app.root_path, 'uploads')
        os.makedirs(upload_dir, exist_ok=True)
        db.create_all()
        _migrate_columns(app)
        _seed_admin(app)
        _seed_rooms(app)

    return app


def _migrate_columns(app):
    """Add columns that were added after initial db.create_all() — idempotent."""
    try:
        with db.engine.connect() as conn:
            conn.execute(db.text(
                'ALTER TABLE bookings ADD COLUMN IF NOT EXISTS payment_slip_drive_url VARCHAR(500)'
            ))
            conn.execute(db.text('COMMIT'))
        app.logger.info('DB migration: payment_slip_drive_url column ensured.')
    except Exception as exc:
        app.logger.warning('DB migration skipped (may be SQLite or already applied): %s', exc)


def _seed_admin(app):
    """Create default admin if none exists."""
    if not User.query.filter_by(role='admin').first():
        admin = User(username='admin', email='admin@guesthouse.com', role='admin')
        admin.set_password('admin123')
        db.session.add(admin)
        db.session.commit()
        app.logger.info('Default admin created: admin / admin123')


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
