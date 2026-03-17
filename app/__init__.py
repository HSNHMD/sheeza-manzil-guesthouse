from flask import Flask
from flask_login import LoginManager
from .models import db, User
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

    app.register_blueprint(auth_bp)
    app.register_blueprint(rooms_bp)
    app.register_blueprint(bookings_bp)
    app.register_blueprint(invoices_bp)
    app.register_blueprint(housekeeping_bp)
    app.register_blueprint(calendar_bp)

    with app.app_context():
        db.create_all()
        _seed_admin(app)

    return app


def _seed_admin(app):
    """Create default admin if none exists."""
    if not User.query.filter_by(role='admin').first():
        admin = User(username='admin', email='admin@guesthouse.com', role='admin')
        admin.set_password('admin123')
        db.session.add(admin)
        db.session.commit()
        app.logger.info('Default admin created: admin / admin123')
