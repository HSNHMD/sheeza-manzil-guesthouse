from datetime import datetime, date
from flask_sqlalchemy import SQLAlchemy
from flask_login import UserMixin
from werkzeug.security import generate_password_hash, check_password_hash

db = SQLAlchemy()


class User(UserMixin, db.Model):
    __tablename__ = 'users'
    id = db.Column(db.Integer, primary_key=True)
    username = db.Column(db.String(64), unique=True, nullable=False)
    email = db.Column(db.String(120), unique=True, nullable=False)
    password_hash = db.Column(db.String(256), nullable=False)
    role = db.Column(db.String(20), nullable=False, default='staff')  # 'admin' or 'staff'
    is_active = db.Column(db.Boolean, default=True)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

    def set_password(self, password):
        self.password_hash = generate_password_hash(password)

    def check_password(self, password):
        return check_password_hash(self.password_hash, password)

    @property
    def is_admin(self):
        return self.role == 'admin'

    def __repr__(self):
        return f'<User {self.username}>'


class Room(db.Model):
    __tablename__ = 'rooms'
    id = db.Column(db.Integer, primary_key=True)
    number = db.Column(db.String(10), unique=True, nullable=False)
    name = db.Column(db.String(100))
    room_type = db.Column(db.String(50), nullable=False)  # single, double, suite, etc.
    floor = db.Column(db.Integer, default=1)
    capacity = db.Column(db.Integer, default=1)
    price_per_night = db.Column(db.Float, nullable=False)
    status = db.Column(db.String(20), default='available')  # available, occupied, maintenance, cleaning
    housekeeping_status = db.Column(db.String(20), default='clean')  # clean, dirty, in_progress
    description = db.Column(db.Text)
    amenities = db.Column(db.String(500))
    is_active = db.Column(db.Boolean, default=True)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

    bookings = db.relationship('Booking', backref='room', lazy='dynamic')

    def __repr__(self):
        return f'<Room {self.number}>'

    @property
    def current_booking(self):
        today = date.today()
        return Booking.query.filter(
            Booking.room_id == self.id,
            Booking.status == 'checked_in',
            Booking.check_in_date <= today,
            Booking.check_out_date >= today
        ).first()


class Guest(db.Model):
    __tablename__ = 'guests'
    id = db.Column(db.Integer, primary_key=True)
    first_name = db.Column(db.String(64), nullable=False)
    last_name = db.Column(db.String(64), nullable=False)
    email = db.Column(db.String(120))
    phone = db.Column(db.String(20))
    id_type = db.Column(db.String(30))  # passport, national_id, driver_license
    id_number = db.Column(db.String(50))
    nationality = db.Column(db.String(50))
    address = db.Column(db.Text)
    notes = db.Column(db.Text)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

    bookings = db.relationship('Booking', backref='guest', lazy='dynamic')

    @property
    def full_name(self):
        return f'{self.first_name} {self.last_name}'

    def __repr__(self):
        return f'<Guest {self.full_name}>'


class Booking(db.Model):
    __tablename__ = 'bookings'
    id = db.Column(db.Integer, primary_key=True)
    booking_ref = db.Column(db.String(20), unique=True, nullable=False)
    room_id = db.Column(db.Integer, db.ForeignKey('rooms.id'), nullable=False)
    guest_id = db.Column(db.Integer, db.ForeignKey('guests.id'), nullable=False)
    check_in_date = db.Column(db.Date, nullable=False)
    check_out_date = db.Column(db.Date, nullable=False)
    actual_check_in = db.Column(db.DateTime)
    actual_check_out = db.Column(db.DateTime)
    num_guests = db.Column(db.Integer, default=1)
    status = db.Column(db.String(20), default='confirmed')  # unconfirmed, pending_verification, confirmed, checked_in, checked_out, cancelled
    special_requests = db.Column(db.Text)
    total_amount = db.Column(db.Float, default=0.0)
    created_by = db.Column(db.Integer, db.ForeignKey('users.id'))
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    updated_at = db.Column(db.DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)
    id_card_filename   = db.Column(db.String(255))
    id_card_drive_url  = db.Column(db.String(500))
    payment_slip_filename = db.Column(db.String(255))

    invoice = db.relationship('Invoice', backref='booking', uselist=False)
    creator = db.relationship('User', foreign_keys=[created_by])

    @property
    def nights(self):
        return (self.check_out_date - self.check_in_date).days

    def calculate_total(self):
        self.total_amount = self.nights * self.room.price_per_night
        return self.total_amount

    def __repr__(self):
        return f'<Booking {self.booking_ref}>'


class Invoice(db.Model):
    __tablename__ = 'invoices'
    id = db.Column(db.Integer, primary_key=True)
    invoice_number = db.Column(db.String(20), unique=True, nullable=False)
    booking_id = db.Column(db.Integer, db.ForeignKey('bookings.id'), nullable=False)
    issue_date = db.Column(db.Date, default=date.today)
    subtotal = db.Column(db.Float, default=0.0)
    tax_rate = db.Column(db.Float, default=0.0)
    tax_amount = db.Column(db.Float, default=0.0)
    total_amount = db.Column(db.Float, default=0.0)
    payment_status = db.Column(db.String(20), default='unpaid')  # unpaid, partial, paid
    payment_method = db.Column(db.String(30))  # cash, card, bank_transfer, online
    amount_paid = db.Column(db.Float, default=0.0)
    invoice_to = db.Column(db.String(150))    # defaults to guest name if blank
    company_name = db.Column(db.String(150))
    billing_address = db.Column(db.Text)
    notes = db.Column(db.Text)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

    @property
    def bill_to_name(self):
        """Display name for 'Invoice To' — falls back to guest full name."""
        return self.invoice_to or self.booking.guest.full_name

    @property
    def balance_due(self):
        return self.total_amount - self.amount_paid

    def __repr__(self):
        return f'<Invoice {self.invoice_number}>'


class HousekeepingLog(db.Model):
    __tablename__ = 'housekeeping_logs'
    id = db.Column(db.Integer, primary_key=True)
    room_id = db.Column(db.Integer, db.ForeignKey('rooms.id'), nullable=False)
    staff_id = db.Column(db.Integer, db.ForeignKey('users.id'))
    action = db.Column(db.String(50), nullable=False)  # started_cleaning, completed, inspected, maintenance_request
    notes = db.Column(db.Text)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

    room = db.relationship('Room', backref='housekeeping_logs')
    staff = db.relationship('User', foreign_keys=[staff_id])

    def __repr__(self):
        return f'<HousekeepingLog room={self.room_id} action={self.action}>'
