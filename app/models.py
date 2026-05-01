from datetime import datetime, date
from flask_sqlalchemy import SQLAlchemy
from flask_login import UserMixin
from werkzeug.security import generate_password_hash, check_password_hash

db = SQLAlchemy()


# Note: `property_id` columns on wave-1 models do NOT carry a Python
# `default=` callable. Instead the migration sets `server_default='1'`
# at the DB level, so any INSERT that doesn't supply `property_id`
# transparently lands on the singleton property in V1. This keeps the
# default resolution OUT of SQLAlchemy's flush hot-path (where calling
# db.session.flush() recursively triggers "Session is already
# flushing" errors). Multi-property V2 will replace the server_default
# with explicit property_id assignment in route handlers, sourced from
# `services.property.current_property_id()`.


class User(UserMixin, db.Model):
    __tablename__ = 'users'
    id = db.Column(db.Integer, primary_key=True)
    username = db.Column(db.String(64), unique=True, nullable=False)
    email = db.Column(db.String(120), unique=True, nullable=False)
    password_hash = db.Column(db.String(256), nullable=False)
    role = db.Column(db.String(20), nullable=False, default='staff')  # 'admin' or 'staff'
    # Department for role-based landing. Drives where login lands the
    # user. Nullable for back-compat — existing users default to NULL
    # and fall through to the Dashboard. Whitelisted slugs are kept
    # tight so the dropdown stays scannable; admin/users UI exposes
    # exactly the values in DEPARTMENTS below.
    department = db.Column(db.String(40), nullable=True)
    is_active = db.Column(db.Boolean, default=True)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

    def set_password(self, password):
        self.password_hash = generate_password_hash(password)

    def check_password(self, password):
        return check_password_hash(self.password_hash, password)

    # Whitelisted department slugs for the role-based landing
    # dispatcher. Adding a new department needs three coordinated
    # edits: this tuple, services.landing._DEPT_LANDING, and the
    # admin/users UI dropdown. Anything not in this list falls
    # through to the Dashboard.
    DEPARTMENTS = (
        ('front_office', 'Front Office'),
        ('housekeeping', 'Housekeeping'),
        ('restaurant',   'Restaurant'),
        ('accounting',   'Accounting'),
    )

    @property
    def is_admin(self):
        return self.role == 'admin'

    @property
    def department_label(self):
        """Return the human-readable department name (or '—')."""
        for slug, label in self.DEPARTMENTS:
            if slug == (self.department or ''):
                return label
        return '—'

    def __repr__(self):
        return f'<User {self.username}>'


class Room(db.Model):
    __tablename__ = 'rooms'
    id = db.Column(db.Integer, primary_key=True)
    # Multi-Property V1 — every Room belongs to one Property. Default
    # resolves to the singleton in single-property environments.
    property_id = db.Column(
        db.Integer, db.ForeignKey('properties.id', ondelete='RESTRICT'),
        nullable=False, index=True,
        server_default='1',
    )
    number = db.Column(db.String(10), unique=True, nullable=False)
    name = db.Column(db.String(100))
    # Legacy free-text column. Kept for backwards compat with existing
    # templates / queries. The Rates & Inventory V1 migration backfills
    # `room_type_id` (FK below) from the distinct values found here.
    room_type = db.Column(db.String(50), nullable=False)
    # Rates & Inventory V1 — optional FK to room_types catalog row.
    # Nullable for now so booking flows can ignore it until migrated.
    room_type_id = db.Column(
        db.Integer, db.ForeignKey('room_types.id', ondelete='SET NULL'),
        nullable=True,
    )
    floor = db.Column(db.Integer, default=1)
    capacity = db.Column(db.Integer, default=1)
    price_per_night = db.Column(db.Float, nullable=False)
    status = db.Column(db.String(20), default='available')  # available, occupied, maintenance, cleaning
    # Housekeeping vocabulary (V1): clean, dirty, in_progress, inspected, out_of_order.
    # Operational `status` and `housekeeping_status` are intentionally distinct:
    # an occupied room can still be dirty/inspected, a vacant room can be clean
    # or dirty. See app/services/housekeeping.py for the canonical state set.
    housekeeping_status = db.Column(db.String(20), default='clean')
    description = db.Column(db.Text)
    amenities = db.Column(db.String(500))
    notes = db.Column(db.Text)
    is_active = db.Column(db.Boolean, default=True)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

    # Housekeeping V1 — assignment + audit columns (added by migration
    # b4c1f2d6e892). All four are nullable; pre-existing rooms simply
    # render with no assignee and no last-updated stamp.
    assigned_to_user_id = db.Column(
        db.Integer, db.ForeignKey('users.id', ondelete='SET NULL'),
        nullable=True,
    )
    assigned_at = db.Column(db.DateTime, nullable=True)
    housekeeping_updated_at = db.Column(db.DateTime, nullable=True)
    housekeeping_updated_by_user_id = db.Column(
        db.Integer, db.ForeignKey('users.id', ondelete='SET NULL'),
        nullable=True,
    )

    assigned_to = db.relationship(
        'User', foreign_keys=[assigned_to_user_id])
    housekeeping_updated_by = db.relationship(
        'User', foreign_keys=[housekeeping_updated_by_user_id])

    bookings = db.relationship('Booking', backref='room', lazy='dynamic')
    type_ref = db.relationship(
        'RoomType', foreign_keys=[room_type_id],
        backref=db.backref('rooms', lazy='dynamic'),
    )

    def __repr__(self):
        return f'<Room {self.number}>'

    @property
    def current_booking(self):
        from .utils import hotel_date
        today = hotel_date()
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
    # Multi-Property V1 — denormalized for query speed in reports +
    # board views. Always equal to the Room's property_id for the
    # bound room.
    property_id = db.Column(
        db.Integer, db.ForeignKey('properties.id', ondelete='RESTRICT'),
        nullable=False, index=True,
        server_default='1',
    )
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
    id_card_filename      = db.Column(db.String(255))
    payment_slip_filename = db.Column(db.String(255))
    id_card_drive_id      = db.Column(db.String(255))
    payment_slip_drive_id = db.Column(db.String(255))

    # Group Bookings V1 — both nullable so standalone bookings stay
    # unchanged. `booking_group_id` is set when the booking is
    # attached to a group; `billing_target` controls whether ad-hoc
    # folio items default to this booking ('individual') or to the
    # group's master_booking ('master'). V1 never auto-rolls existing
    # rows — operators still pick the target per charge.
    booking_group_id = db.Column(
        db.Integer, db.ForeignKey('booking_groups.id', ondelete='SET NULL'),
        nullable=True, index=True,
    )
    billing_target = db.Column(db.String(20), nullable=False,
                                default='individual')

    # Channel Manager Foundation V1 — booking source tracking. `source`
    # records WHERE this booking originated (direct vs OTA vs walk-in
    # vs WhatsApp etc.). `external_source` + `external_reservation_ref`
    # are populated only for OTA-originated bookings; the unique
    # composite index `(external_source, external_reservation_ref)`
    # prevents duplicate imports of the same OTA reservation.
    # Existing bookings are backfilled to source='direct' by the
    # migration. See app/services/channels.py for the canonical
    # vocabulary in BOOKING_SOURCES.
    source = db.Column(db.String(30), nullable=False,
                        server_default='direct', index=True)
    external_source = db.Column(db.String(30), nullable=True, index=True)
    external_reservation_ref = db.Column(db.String(120),
                                          nullable=True, index=True)

    invoice = db.relationship('Invoice', backref='booking', uselist=False)
    creator = db.relationship('User', foreign_keys=[created_by])
    booking_group = db.relationship(
        'BookingGroup', foreign_keys=[booking_group_id],
        backref=db.backref('bookings', lazy='dynamic'),
    )

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
    # Multi-Property V1 — denormalized.
    property_id = db.Column(
        db.Integer, db.ForeignKey('properties.id', ondelete='RESTRICT'),
        nullable=False, index=True,
        server_default='1',
    )
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


EXPENSE_CATEGORIES = [
    'Utilities', 'Staff Salaries', 'Cleaning Supplies', 'Maintenance',
    'Food & Beverages', 'Marketing', 'Platform Fees', 'Bank Charges',
    'Taxes', 'Petty Cash', 'Other',
]


class Expense(db.Model):
    __tablename__ = 'expenses'
    id = db.Column(db.Integer, primary_key=True)
    date = db.Column(db.Date, nullable=False)
    category = db.Column(db.String(50), nullable=False)
    amount = db.Column(db.Float, nullable=False)
    description = db.Column(db.Text)
    receipt_filename = db.Column(db.String(255))
    receipt_drive_id = db.Column(db.String(255))
    created_by = db.Column(db.Integer, db.ForeignKey('users.id'))
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

    creator = db.relationship('User', foreign_keys=[created_by])

    def __repr__(self):
        return f'<Expense {self.category} {self.amount}>'


class BankTransaction(db.Model):
    __tablename__ = 'bank_transactions'
    id = db.Column(db.Integer, primary_key=True)
    statement_date = db.Column(db.Date, nullable=False)
    description = db.Column(db.Text)
    amount = db.Column(db.Float, nullable=False)
    match_type = db.Column(db.String(20), default='unmatched')  # invoice, expense, unmatched
    match_ref = db.Column(db.String(50))
    uploaded_at = db.Column(db.DateTime, default=datetime.utcnow)

    def __repr__(self):
        return f'<BankTransaction {self.statement_date} {self.amount}>'


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


# ── Audit / Activity Log (append-only) ─────────────────────────────────────
# Records important booking/payment/admin lifecycle events. Rows are written
# only via app.services.audit.log_activity() — no UPDATE or DELETE routes are
# exposed. See docs/admin_dashboard_plan.md and the helper module for the
# privacy contract (no secrets, no passport/slip contents, no message bodies).
class ActivityLog(db.Model):
    __tablename__ = 'activity_logs'

    id            = db.Column(db.Integer, primary_key=True)
    created_at    = db.Column(db.DateTime, default=datetime.utcnow,
                              nullable=False, index=True)
    actor_type    = db.Column(db.String(20), nullable=False)
    # 'guest' | 'admin' | 'system' | 'ai_agent'
    actor_user_id = db.Column(db.Integer,
                              db.ForeignKey('users.id', ondelete='SET NULL'),
                              nullable=True)
    booking_id    = db.Column(db.Integer,
                              db.ForeignKey('bookings.id', ondelete='SET NULL'),
                              nullable=True)
    invoice_id    = db.Column(db.Integer,
                              db.ForeignKey('invoices.id', ondelete='SET NULL'),
                              nullable=True)
    action        = db.Column(db.String(64), nullable=False)
    old_value     = db.Column(db.String(64), nullable=True)
    new_value     = db.Column(db.String(64), nullable=True)
    description   = db.Column(db.String(500), nullable=False, default='')
    metadata_json = db.Column(db.Text, nullable=True)
    ip_address    = db.Column(db.String(45), nullable=True)   # IPv4 or IPv6
    user_agent    = db.Column(db.String(255), nullable=True)

    actor   = db.relationship('User', foreign_keys=[actor_user_id])
    booking = db.relationship('Booking', foreign_keys=[booking_id])
    invoice = db.relationship('Invoice', foreign_keys=[invoice_id])

    __table_args__ = (
        db.Index('ix_activity_logs_booking_created', 'booking_id', 'created_at'),
        db.Index('ix_activity_logs_invoice_created', 'invoice_id', 'created_at'),
        db.Index('ix_activity_logs_action_created',  'action',     'created_at'),
    )

    def __repr__(self):
        return f'<ActivityLog id={self.id} action={self.action}>'


# ── Inbound / outbound WhatsApp message log ────────────────────────────────
# Stores a normalized record of every WhatsApp message that touches the
# system. Inbound messages arrive via Meta's webhook (POST /webhooks/whatsapp)
# and are persisted by app.routes.whatsapp_webhook. Outbound messages may be
# logged here by future code paths — V1 only writes inbound.
#
# Privacy:
#   - The full sender phone number is NOT stored. Instead we keep an
#     HMAC-SHA256 hash (keyed by SECRET_KEY) for cross-message correlation
#     plus the last-4 digits for human-friendly display. This means an
#     attacker who exfiltrates this table cannot recover guest phone
#     numbers without also breaching SECRET_KEY.
#   - The full message body IS stored (`body_text`) because the admin
#     needs to read the guest's message. It is exposed only via
#     admin-gated routes; ActivityLog rows linked to inbound events
#     never include the body or its preview.
class WhatsAppMessage(db.Model):
    __tablename__ = 'whatsapp_messages'

    id            = db.Column(db.Integer, primary_key=True)
    # Multi-Property V1 — every message belongs to one property's
    # inbox. Future: webhook routes by destination phone number.
    property_id   = db.Column(
        db.Integer, db.ForeignKey('properties.id', ondelete='RESTRICT'),
        nullable=False, index=True,
        server_default='1',
    )
    created_at    = db.Column(db.DateTime, default=datetime.utcnow,
                              nullable=False, index=True)
    direction     = db.Column(db.String(10), nullable=False)
                    # 'inbound' | 'outbound'

    # Meta's wamid.… — UNIQUE so re-deliveries from Meta dedupe naturally.
    # Nullable to allow future outbound rows that pre-date a Meta ID.
    wa_message_id = db.Column(db.String(128), unique=True, nullable=True)
    wa_timestamp  = db.Column(db.DateTime, nullable=True)

    # Privacy-preserving phone storage. See module docstring above.
    from_phone_hash  = db.Column(db.String(64), nullable=True)
    from_phone_last4 = db.Column(db.String(8),  nullable=True)
    to_phone_last4   = db.Column(db.String(8),  nullable=True)

    profile_name  = db.Column(db.String(100), nullable=True)

    booking_id    = db.Column(db.Integer,
                              db.ForeignKey('bookings.id', ondelete='SET NULL'),
                              nullable=True)
    guest_id      = db.Column(db.Integer,
                              db.ForeignKey('guests.id', ondelete='SET NULL'),
                              nullable=True)

    message_type  = db.Column(db.String(30), nullable=False)
                    # 'text' | 'image' | 'audio' | 'video' | 'document' |
                    # 'location' | 'sticker' | 'unsupported_<type>'
    body_text     = db.Column(db.Text, nullable=True)
                    # Full body for admin display; never copied to ActivityLog
    body_preview  = db.Column(db.String(120), nullable=True)
                    # First 120 chars for inbox list view
    status        = db.Column(db.String(20), default='received')

    metadata_json = db.Column(db.Text, nullable=True)

    booking = db.relationship('Booking', foreign_keys=[booking_id])
    guest   = db.relationship('Guest',   foreign_keys=[guest_id])

    __table_args__ = (
        db.Index('ix_wa_messages_booking_created',
                 'booking_id', 'created_at'),
        db.Index('ix_wa_messages_guest_created',
                 'guest_id', 'created_at'),
        db.Index('ix_wa_messages_direction_created',
                 'direction', 'created_at'),
        db.Index('ix_wa_messages_from_phone_hash',
                 'from_phone_hash'),
    )

    def __repr__(self):
        return (f'<WhatsAppMessage id={self.id} '
                f'direction={self.direction} type={self.message_type}>')


# ── Guest Folio (V1) ─────────────────────────────────────────────────────
#
# A FolioItem is a single line on a booking's running account. The folio
# is the per-stay ledger of charges, credits, payments, and adjustments.
#
# Privacy / accounting contract (binding):
#   - V1 is ADDITIVE — it does NOT auto-post room nights. Booking.total_amount
#     remains the source of truth for room revenue. Folio is for EXTRAS only
#     (laundry, restaurant, transfer, fee, discount, payment, adjustment, …).
#     Avoids double-counting room revenue.
#   - total_amount is stored SIGNED. Charges are positive; payments and
#     discounts are negative. The sign is applied server-side at post time
#     (forms accept positive amounts only).
#   - Items are NEVER hard-deleted. Voiding marks status='voided' and
#     populates voided_at, voided_by_user_id, void_reason.
#   - Float (not Numeric) is used for amount columns to match the existing
#     Booking/Invoice precedent. A future migration to Numeric is tracked
#     in docs/guest_folio_accounting_pos_roadmap.md.
class FolioItem(db.Model):
    __tablename__ = 'folio_items'

    id          = db.Column(db.Integer, primary_key=True)
    # Multi-Property V1 — denormalized for report scoping speed.
    # Always equal to Booking.property_id at write time.
    property_id = db.Column(
        db.Integer, db.ForeignKey('properties.id', ondelete='RESTRICT'),
        nullable=False, index=True,
        server_default='1',
    )
    created_at  = db.Column(db.DateTime, default=datetime.utcnow,
                            nullable=False, index=True)
    updated_at  = db.Column(db.DateTime, default=datetime.utcnow,
                            onupdate=datetime.utcnow, nullable=False)

    # ── Foreign keys ─────────────────────────────────────────────
    # booking_id is required (V1 binds every folio item to a booking).
    # guest_id is denormalized for portfolio queries (sum across bookings).
    # invoice_id is set when the item is rolled into a closed invoice
    # (status transitions open → invoiced).
    booking_id = db.Column(db.Integer,
                           db.ForeignKey('bookings.id', ondelete='CASCADE'),
                           nullable=False)
    guest_id   = db.Column(db.Integer,
                           db.ForeignKey('guests.id', ondelete='SET NULL'),
                           nullable=True)
    invoice_id = db.Column(db.Integer,
                           db.ForeignKey('invoices.id', ondelete='SET NULL'),
                           nullable=True)

    # ── Type taxonomy ────────────────────────────────────────────
    # See app/services/folio.ITEM_TYPES for the canonical enum + labels.
    item_type   = db.Column(db.String(30), nullable=False)

    description = db.Column(db.String(255), nullable=False)
    quantity    = db.Column(db.Float, nullable=False, default=1.0)
    unit_price  = db.Column(db.Float, nullable=False, default=0.0)
    amount      = db.Column(db.Float, nullable=False, default=0.0)
    tax_amount  = db.Column(db.Float, nullable=False, default=0.0)
    service_charge_amount = db.Column(db.Float, nullable=False, default=0.0)
    total_amount = db.Column(db.Float, nullable=False, default=0.0)

    # ── Lifecycle ────────────────────────────────────────────────
    # 'open'     — posted, not yet invoiced
    # 'invoiced' — rolled into a closed invoice (Phase 4 territory)
    # 'paid'     — invoiced + payment matched (Phase 4 territory)
    # 'voided'   — voided by admin; excluded from balances
    status        = db.Column(db.String(20), nullable=False, default='open')

    # ── Provenance ───────────────────────────────────────────────
    # 'manual'     — admin form post
    # 'booking'    — system-posted from booking flow (V1 unused)
    # 'accounting' — bulk import / accounting tool (V1 unused)
    # 'pos'        — restaurant POS (Phase 6+)
    # 'system'     — internal automation (V1 unused)
    source_module = db.Column(db.String(20), nullable=False, default='manual')

    posted_by_user_id = db.Column(db.Integer,
                                  db.ForeignKey('users.id', ondelete='SET NULL'),
                                  nullable=True)

    # ── Void tracking ────────────────────────────────────────────
    voided_at         = db.Column(db.DateTime, nullable=True)
    voided_by_user_id = db.Column(db.Integer,
                                  db.ForeignKey('users.id', ondelete='SET NULL'),
                                  nullable=True)
    void_reason       = db.Column(db.String(255), nullable=True)

    # ── Free-form metadata ───────────────────────────────────────
    # Used by future POS / accounting modules to attach refs without
    # schema changes. NEVER write secrets, IDs, or guest documents here.
    metadata_json     = db.Column(db.Text, nullable=True)

    # ── Relationships ────────────────────────────────────────────
    booking = db.relationship(
        'Booking',
        foreign_keys=[booking_id],
        backref=db.backref('folio_items',
                           lazy='dynamic',
                           order_by='FolioItem.created_at'),
    )
    guest   = db.relationship('Guest',   foreign_keys=[guest_id])
    invoice = db.relationship('Invoice', foreign_keys=[invoice_id])
    posted_by = db.relationship('User', foreign_keys=[posted_by_user_id])
    voided_by = db.relationship('User', foreign_keys=[voided_by_user_id])

    __table_args__ = (
        db.Index('ix_folio_items_booking_created',
                 'booking_id', 'created_at'),
        db.Index('ix_folio_items_guest_created',
                 'guest_id', 'created_at'),
        db.Index('ix_folio_items_invoice',
                 'invoice_id'),
        db.Index('ix_folio_items_status',
                 'status'),
    )

    @property
    def is_voided(self) -> bool:
        return self.status == 'voided'

    @property
    def is_open(self) -> bool:
        return self.status == 'open'

    def __repr__(self):
        return (f'<FolioItem id={self.id} booking_id={self.booking_id} '
                f'type={self.item_type} status={self.status} '
                f'total={self.total_amount}>')


# ── Room blocks (out-of-order / owner-hold periods) ──────────────────
#
# A RoomBlock marks a date range during which a room is unavailable
# for guest bookings. Used for maintenance, deep cleaning, owner holds,
# or any other "do not place a guest here" reason.
#
# Design rules:
#   - Blocks coexist with the existing Room.status flag. Room.status is
#     a current-time state ('available' / 'out_of_order' / etc.).
#     RoomBlock is a date-range record. Both can be present.
#   - end_date is EXCLUSIVE — same convention as Booking.check_out_date.
#     A block from 2026-04-30 to 2026-05-02 covers nights for the 30th
#     and 1st, but the 2nd is free.
#   - Blocks are NEVER hard-deleted. Removal sets removed_at / removed_by
#     so the audit trail survives. Active blocks have removed_at IS NULL.
class RoomBlock(db.Model):
    __tablename__ = 'room_blocks'

    id          = db.Column(db.Integer, primary_key=True)
    # Multi-Property V1 — equal to Room.property_id of the bound room.
    property_id = db.Column(
        db.Integer, db.ForeignKey('properties.id', ondelete='RESTRICT'),
        nullable=False, index=True,
        server_default='1',
    )
    created_at  = db.Column(db.DateTime, default=datetime.utcnow,
                            nullable=False, index=True)

    room_id     = db.Column(db.Integer,
                            db.ForeignKey('rooms.id', ondelete='CASCADE'),
                            nullable=False)
    start_date  = db.Column(db.Date, nullable=False)
    end_date    = db.Column(db.Date, nullable=False)

    reason      = db.Column(db.String(40), nullable=False, default='maintenance')
                  # 'maintenance' | 'owner_hold' | 'deep_cleaning' |
                  # 'damage_repair' | 'other'
    notes       = db.Column(db.String(500), nullable=True)

    created_by_user_id = db.Column(
        db.Integer, db.ForeignKey('users.id', ondelete='SET NULL'),
        nullable=True,
    )
    removed_at  = db.Column(db.DateTime, nullable=True)
    removed_by_user_id = db.Column(
        db.Integer, db.ForeignKey('users.id', ondelete='SET NULL'),
        nullable=True,
    )

    room       = db.relationship('Room', foreign_keys=[room_id])
    created_by = db.relationship('User', foreign_keys=[created_by_user_id])
    removed_by = db.relationship('User', foreign_keys=[removed_by_user_id])

    __table_args__ = (
        db.Index('ix_room_blocks_room_dates',
                 'room_id', 'start_date', 'end_date'),
        db.Index('ix_room_blocks_active',
                 'room_id', 'removed_at'),
    )

    @property
    def is_active(self) -> bool:
        return self.removed_at is None

    @property
    def nights(self) -> int:
        return (self.end_date - self.start_date).days

    def __repr__(self):
        return (f'<RoomBlock id={self.id} room_id={self.room_id} '
                f'{self.start_date}→{self.end_date} reason={self.reason} '
                f'active={self.is_active}>')


# ── Cashiering V1 ────────────────────────────────────────────────────
#
# A CashierTransaction is a cash-event ledger entry — it captures HOW
# a payment was received (method, reference, cashier) separate from
# the FolioItem ledger that tracks WHAT the guest owes.
#
# Design rules (binding, mirrored from
# docs/accounts_business_date_night_audit_plan.md §2):
#
#   - Posting a payment creates BOTH a CashierTransaction row AND a
#     FolioItem row with item_type='payment'. They are linked via
#     CashierTransaction.folio_item_id.
#   - Folio balance math is unchanged — it still reads folio_items
#     only. CashierTransaction is the cash-flow / audit record.
#   - Reports about cash flow read cashier_transactions (filter by
#     method, by cashier_user_id). Reports about guest balance read
#     folio_items. They reconcile via folio_item_id.
#   - Voiding a transaction soft-removes (status='voided') AND voids
#     the linked folio_item (status='voided'). Both rows preserved
#     for audit; balance math excludes both.
#   - Refunds create a NEW transaction with transaction_type='refund'
#     (and a new positive folio_item, not a void of the original).
#     Tax / GST stays correct because the original payment row is
#     never deleted.
#   - V1 does NOT modify Invoice.amount_paid or Invoice.payment_status.
#     The legacy invoice payment flow stays untouched. Reconciling
#     cashiering with invoices is Phase 4 (post-Night-Audit V1).
#   - amount is always stored POSITIVE. Direction is on transaction_type.
class CashierTransaction(db.Model):
    __tablename__ = 'cashier_transactions'

    id          = db.Column(db.Integer, primary_key=True)
    # Multi-Property V1 — denormalized so cashier reports filter by
    # property without joining bookings (some txns have NULL booking).
    property_id = db.Column(
        db.Integer, db.ForeignKey('properties.id', ondelete='RESTRICT'),
        nullable=False, index=True,
        server_default='1',
    )
    created_at  = db.Column(db.DateTime, default=datetime.utcnow,
                            nullable=False, index=True)

    # ── Foreign keys ────────────────────────────────────────────
    booking_id    = db.Column(db.Integer,
                              db.ForeignKey('bookings.id', ondelete='SET NULL'),
                              nullable=True)
    guest_id      = db.Column(db.Integer,
                              db.ForeignKey('guests.id', ondelete='SET NULL'),
                              nullable=True)
    folio_item_id = db.Column(db.Integer,
                              db.ForeignKey('folio_items.id', ondelete='SET NULL'),
                              nullable=True)
    invoice_id    = db.Column(db.Integer,
                              db.ForeignKey('invoices.id', ondelete='SET NULL'),
                              nullable=True)

    # ── Money ────────────────────────────────────────────────────
    # Always positive — direction is on transaction_type.
    amount   = db.Column(db.Float, nullable=False, default=0.0)
    currency = db.Column(db.String(3), nullable=False, default='MVR')

    # ── How / who ───────────────────────────────────────────────
    payment_method = db.Column(db.String(20), nullable=False)
                     # cash | bank_transfer | card | wallet | other
    reference_number = db.Column(db.String(80), nullable=True)
    received_by_user_id = db.Column(
        db.Integer, db.ForeignKey('users.id', ondelete='SET NULL'),
        nullable=True,
    )

    # ── Type / lifecycle ────────────────────────────────────────
    transaction_type = db.Column(db.String(20), nullable=False, default='payment')
                       # payment | refund | adjustment
    status = db.Column(db.String(20), nullable=False, default='posted')
             # posted | voided | refunded

    notes = db.Column(db.String(500), nullable=True)
    metadata_json = db.Column(db.Text, nullable=True)

    # ── Void tracking ───────────────────────────────────────────
    voided_at         = db.Column(db.DateTime, nullable=True)
    voided_by_user_id = db.Column(
        db.Integer, db.ForeignKey('users.id', ondelete='SET NULL'),
        nullable=True,
    )
    void_reason = db.Column(db.String(255), nullable=True)

    # ── Relationships ───────────────────────────────────────────
    booking     = db.relationship('Booking', foreign_keys=[booking_id])
    guest       = db.relationship('Guest',   foreign_keys=[guest_id])
    folio_item  = db.relationship('FolioItem', foreign_keys=[folio_item_id])
    invoice     = db.relationship('Invoice', foreign_keys=[invoice_id])
    received_by = db.relationship('User', foreign_keys=[received_by_user_id])
    voided_by   = db.relationship('User', foreign_keys=[voided_by_user_id])

    __table_args__ = (
        db.Index('ix_cashier_txn_booking_created',
                 'booking_id', 'created_at'),
        db.Index('ix_cashier_txn_user_created',
                 'received_by_user_id', 'created_at'),
        db.Index('ix_cashier_txn_status',
                 'status'),
        db.Index('ix_cashier_txn_method',
                 'payment_method'),
    )

    @property
    def is_voided(self) -> bool:
        return self.status == 'voided'

    @property
    def is_refund(self) -> bool:
        return self.transaction_type == 'refund'

    def __repr__(self):
        return (f'<CashierTransaction id={self.id} '
                f'booking_id={self.booking_id} '
                f'{self.transaction_type} {self.amount} {self.currency} '
                f'via {self.payment_method} status={self.status}>')


# ── Business Date State (Night Audit V1) ─────────────────────────────
#
# Single-row table holding the property's current "business date" —
# the operator-controlled date that does NOT auto-follow server clock
# midnight. Business date advances only when Night Audit completes.
#
# Why this matters: hospitality operations span midnight. A 02:30 late
# checkout on June 1 server time is still operationally "May 31" until
# the operator runs Night Audit. Reports filter on business_date, NOT
# created_at, so the day's revenue stays whole.
#
# See docs/accounts_business_date_night_audit_plan.md §3.
class BusinessDateState(db.Model):
    __tablename__ = 'business_date_state'

    id          = db.Column(db.Integer, primary_key=True)
    created_at  = db.Column(db.DateTime, default=datetime.utcnow,
                            nullable=False)
    updated_at  = db.Column(db.DateTime, default=datetime.utcnow,
                            onupdate=datetime.utcnow, nullable=False)

    current_business_date = db.Column(db.Date, nullable=False)

    # Last completed Night Audit (None on fresh install)
    last_audit_run_at         = db.Column(db.DateTime, nullable=True)
    last_audit_run_by_user_id = db.Column(
        db.Integer, db.ForeignKey('users.id', ondelete='SET NULL'),
        nullable=True,
    )

    # Concurrent-run guard. Set True at the start of a Night Audit;
    # cleared on completion OR explicit abort. Prevents two operators
    # from racing the close.
    audit_in_progress         = db.Column(db.Boolean, nullable=False,
                                          default=False)
    audit_started_at          = db.Column(db.DateTime, nullable=True)
    audit_started_by_user_id  = db.Column(
        db.Integer, db.ForeignKey('users.id', ondelete='SET NULL'),
        nullable=True,
    )

    last_audit_run_by  = db.relationship('User', foreign_keys=[last_audit_run_by_user_id])
    audit_started_by   = db.relationship('User', foreign_keys=[audit_started_by_user_id])

    def __repr__(self):
        return (f'<BusinessDateState business_date={self.current_business_date} '
                f'audit_in_progress={self.audit_in_progress}>')


# ── Night Audit Run (immutable history) ──────────────────────────────
#
# One row per Night Audit run (started, blocked, completed, failed).
# Completed rows are immutable — corrections happen via adjustment
# folio items in the next business date, never by editing this row.
#
# Reports about "what happened on day X" eventually read from
# daily_revenue_snapshots (Phase 6 of the planning doc). For V1, this
# table is the audit trail of every close attempt.
class NightAuditRun(db.Model):
    __tablename__ = 'night_audit_runs'

    id         = db.Column(db.Integer, primary_key=True)
    created_at = db.Column(db.DateTime, default=datetime.utcnow,
                           nullable=False, index=True)
    completed_at = db.Column(db.DateTime, nullable=True)

    business_date_closed = db.Column(db.Date, nullable=False)
    next_business_date   = db.Column(db.Date, nullable=False)

    run_by_user_id = db.Column(
        db.Integer, db.ForeignKey('users.id', ondelete='SET NULL'),
        nullable=True,
    )

    # started → blocked | completed | failed
    status = db.Column(db.String(20), nullable=False, default='started')

    summary_json    = db.Column(db.Text, nullable=True)
    exception_count = db.Column(db.Integer, nullable=False, default=0)
    warning_count   = db.Column(db.Integer, nullable=False, default=0)

    notes = db.Column(db.String(500), nullable=True)

    run_by = db.relationship('User', foreign_keys=[run_by_user_id])

    __table_args__ = (
        db.Index('ix_night_audit_runs_business_date',
                 'business_date_closed'),
        db.Index('ix_night_audit_runs_status',
                 'status'),
    )

    def __repr__(self):
        return (f'<NightAuditRun id={self.id} '
                f'closed={self.business_date_closed} → {self.next_business_date} '
                f'status={self.status}>')


# ── Rates & Inventory V1 ────────────────────────────────────────────
#
# Lightweight room-type catalog + rate plans + dated overrides +
# per-day restrictions. Designed to be ADDITIVE — existing booking
# flows continue to read Room.room_type (string) and Room.price_per_night
# unchanged. The new layer exposes optional helpers that booking code
# can adopt later without forcing migration of legacy data.
#
# See docs/accounts_business_date_night_audit_plan.md for the broader
# operational context. Channel manager / OTA sync / booking engine are
# explicitly out of V1 scope.


class RoomType(db.Model):
    """Catalog row for a sellable room category (Deluxe, Twin, etc.).

    Existing Room rows carry a free-text `room_type` string. The
    Rates & Inventory V1 migration backfills this table from the
    distinct existing strings and links Room.room_type_id. New rooms
    should reference room_type_id; the legacy string column is kept
    for backwards-compat templates and gradually phased out.
    """
    __tablename__ = 'room_types'

    id          = db.Column(db.Integer, primary_key=True)
    # Multi-Property V1.
    property_id = db.Column(
        db.Integer, db.ForeignKey('properties.id', ondelete='RESTRICT'),
        nullable=False, index=True,
        server_default='1',
    )
    code        = db.Column(db.String(20), unique=True, nullable=False)
    name        = db.Column(db.String(100), nullable=False)
    max_occupancy = db.Column(db.Integer, nullable=False, default=2)
    base_capacity = db.Column(db.Integer, nullable=False, default=2)
    description = db.Column(db.Text)
    is_active   = db.Column(db.Boolean, nullable=False, default=True)
    created_at  = db.Column(db.DateTime, default=datetime.utcnow,
                            nullable=False)
    updated_at  = db.Column(db.DateTime, default=datetime.utcnow,
                            onupdate=datetime.utcnow, nullable=False)

    rate_plans   = db.relationship('RatePlan', backref='room_type',
                                   lazy='dynamic')
    overrides    = db.relationship('RateOverride', backref='room_type',
                                   lazy='dynamic')
    restrictions = db.relationship('RateRestriction', backref='room_type',
                                   lazy='dynamic')

    def __repr__(self):
        return f'<RoomType {self.code}: {self.name}>'


class RatePlan(db.Model):
    """A sellable rate plan attached to a RoomType.

    base_rate is the property-default nightly price for this plan; date
    overrides (RateOverride) supersede it on specific date ranges. V1
    keeps currency at the property level (no FX); set via env or the
    `currency` column for explicit display.
    """
    __tablename__ = 'rate_plans'

    id           = db.Column(db.Integer, primary_key=True)
    # Multi-Property V1.
    property_id  = db.Column(
        db.Integer, db.ForeignKey('properties.id', ondelete='RESTRICT'),
        nullable=False, index=True,
        server_default='1',
    )
    code         = db.Column(db.String(30), unique=True, nullable=False)
    name         = db.Column(db.String(100), nullable=False)
    room_type_id = db.Column(
        db.Integer, db.ForeignKey('room_types.id', ondelete='CASCADE'),
        nullable=False,
    )
    base_rate    = db.Column(db.Float, nullable=False, default=0.0)
    currency     = db.Column(db.String(8), nullable=False, default='USD')
    is_refundable = db.Column(db.Boolean, nullable=False, default=True)
    is_active    = db.Column(db.Boolean, nullable=False, default=True)
    notes        = db.Column(db.Text)
    created_at   = db.Column(db.DateTime, default=datetime.utcnow,
                             nullable=False)
    updated_at   = db.Column(db.DateTime, default=datetime.utcnow,
                             onupdate=datetime.utcnow, nullable=False)

    def __repr__(self):
        return f'<RatePlan {self.code} rate={self.base_rate}>'


class RateOverride(db.Model):
    """Date-range nightly-rate override for a RoomType (and optional plan).

    On any given night the effective rate is computed as:

        most-recently-created active override whose [start, end] covers
        the date AND scope (room_type_id, optional rate_plan_id)
        → falls back to RatePlan.base_rate
        → falls back to property default (Room.price_per_night for now)

    end_date is INCLUSIVE — an override with start=2026-06-01 end=2026-06-07
    applies to seven nights.
    """
    __tablename__ = 'rate_overrides'

    id           = db.Column(db.Integer, primary_key=True)
    # Multi-Property V1 — denormalized; equal to RoomType.property_id.
    property_id  = db.Column(
        db.Integer, db.ForeignKey('properties.id', ondelete='RESTRICT'),
        nullable=False, index=True,
        server_default='1',
    )
    room_type_id = db.Column(
        db.Integer, db.ForeignKey('room_types.id', ondelete='CASCADE'),
        nullable=False,
    )
    rate_plan_id = db.Column(
        db.Integer, db.ForeignKey('rate_plans.id', ondelete='CASCADE'),
        nullable=True,
    )
    start_date   = db.Column(db.Date, nullable=False)
    end_date     = db.Column(db.Date, nullable=False)
    nightly_rate = db.Column(db.Float, nullable=False)
    is_active    = db.Column(db.Boolean, nullable=False, default=True)
    notes        = db.Column(db.Text)
    created_at   = db.Column(db.DateTime, default=datetime.utcnow,
                             nullable=False)

    rate_plan = db.relationship('RatePlan', foreign_keys=[rate_plan_id])

    def covers(self, d):
        return self.start_date <= d <= self.end_date

    def __repr__(self):
        return (f'<RateOverride rt={self.room_type_id} '
                f'{self.start_date}→{self.end_date} @{self.nightly_rate}>')


class RateRestriction(db.Model):
    """Per-room-type restrictions over a date range.

    All four flags + min/max stay are nullable / optional. A row with
    only `stop_sell=True` is the simplest shape — it blocks new
    availability for that room type on those dates. Mixing flags on a
    single row is fine; multiple overlapping rows compose with the
    most-restrictive value winning (see services/inventory.py).
    """
    __tablename__ = 'rate_restrictions'

    id           = db.Column(db.Integer, primary_key=True)
    # Multi-Property V1 — denormalized.
    property_id  = db.Column(
        db.Integer, db.ForeignKey('properties.id', ondelete='RESTRICT'),
        nullable=False, index=True,
        server_default='1',
    )
    room_type_id = db.Column(
        db.Integer, db.ForeignKey('room_types.id', ondelete='CASCADE'),
        nullable=False,
    )
    start_date   = db.Column(db.Date, nullable=False)
    end_date     = db.Column(db.Date, nullable=False)

    min_stay     = db.Column(db.Integer, nullable=True)
    max_stay     = db.Column(db.Integer, nullable=True)
    closed_to_arrival   = db.Column(db.Boolean, nullable=False, default=False)
    closed_to_departure = db.Column(db.Boolean, nullable=False, default=False)
    stop_sell    = db.Column(db.Boolean, nullable=False, default=False)
    is_active    = db.Column(db.Boolean, nullable=False, default=True)

    notes        = db.Column(db.Text)
    created_at   = db.Column(db.DateTime, default=datetime.utcnow,
                             nullable=False)

    def covers(self, d):
        return self.start_date <= d <= self.end_date

    def __repr__(self):
        return (f'<RateRestriction rt={self.room_type_id} '
                f'{self.start_date}→{self.end_date} '
                f'stop_sell={self.stop_sell}>')


# ── POS / F&B V1 ────────────────────────────────────────────────────
#
# Light catalog: categories (e.g. "Drinks", "Mains") + items (e.g.
# "Espresso") with a default unit price. A POS sale never invents its
# own accounting truth: every sale becomes one or more FolioItem rows
# (and optionally a CashierTransaction for "pay now"). See
# app/services/pos.py for the canonical sale flow.
#
# `default_item_type` mirrors the FolioItem.item_type vocabulary
# (services/folio.ITEM_TYPES) — typically 'restaurant', 'goods',
# 'service'. It's the type stamped on each FolioItem the sale creates.


class PosCategory(db.Model):
    __tablename__ = 'pos_categories'

    id          = db.Column(db.Integer, primary_key=True)
    name        = db.Column(db.String(80), unique=True, nullable=False)
    sort_order  = db.Column(db.Integer, nullable=False, default=100)
    is_active   = db.Column(db.Boolean, nullable=False, default=True)
    created_at  = db.Column(db.DateTime, default=datetime.utcnow,
                            nullable=False)
    updated_at  = db.Column(db.DateTime, default=datetime.utcnow,
                            onupdate=datetime.utcnow, nullable=False)

    items = db.relationship('PosItem', backref='category',
                            lazy='dynamic',
                            cascade='all, delete-orphan')

    def __repr__(self):
        return f'<PosCategory {self.name}>'


class PosItem(db.Model):
    __tablename__ = 'pos_items'

    id          = db.Column(db.Integer, primary_key=True)
    category_id = db.Column(
        db.Integer, db.ForeignKey('pos_categories.id', ondelete='CASCADE'),
        nullable=False,
    )
    name        = db.Column(db.String(120), nullable=False)
    description = db.Column(db.String(500), nullable=True)
    price       = db.Column(db.Float, nullable=False, default=0.0)
    # FolioItem.item_type to stamp on each created folio row.
    # Whitelisted in services.pos against folio.ITEM_TYPES.
    default_item_type = db.Column(db.String(30), nullable=False,
                                  default='restaurant')
    is_active   = db.Column(db.Boolean, nullable=False, default=True)
    sort_order  = db.Column(db.Integer, nullable=False, default=100)
    created_at  = db.Column(db.DateTime, default=datetime.utcnow,
                            nullable=False)
    updated_at  = db.Column(db.DateTime, default=datetime.utcnow,
                            onupdate=datetime.utcnow, nullable=False)

    def __repr__(self):
        return f'<PosItem {self.name} @{self.price}>'


# ── Online Menu / QR Ordering V1 ────────────────────────────────────
#
# Guests open /menu on a phone, build a cart, optionally enter their
# room number + last name to attach the order to an in-house booking,
# and submit. Staff see new orders in a queue, confirm them, and
# explicitly decide whether to post the order to the folio. NO auto-
# posting, NO payment online (V1).
#
# `public_token` is a short URL-safe token stored on the order so the
# guest can refresh /menu/order/<token> to track status without auth.
# Item rows snapshot the POS item's name + price at submit time so
# later menu edits don't rewrite history.


class GuestOrder(db.Model):
    __tablename__ = 'guest_orders'

    id          = db.Column(db.Integer, primary_key=True)
    created_at  = db.Column(db.DateTime, default=datetime.utcnow,
                            nullable=False, index=True)
    updated_at  = db.Column(db.DateTime, default=datetime.utcnow,
                            onupdate=datetime.utcnow, nullable=False)

    # URL-safe random token used for the public /menu/order/<token>
    # status page. ~22 chars (token_urlsafe(16)).
    public_token = db.Column(db.String(40), unique=True, nullable=False,
                             index=True)

    # Optional link to a real booking. Populated if the guest's room
    # number + last-name combo matched an active in-house booking at
    # submit time. Otherwise None — the order is still recorded.
    booking_id = db.Column(
        db.Integer, db.ForeignKey('bookings.id', ondelete='SET NULL'),
        nullable=True, index=True,
    )
    # Whatever the guest typed, kept verbatim for staff reference.
    room_number_input = db.Column(db.String(20), nullable=True)
    guest_name_input  = db.Column(db.String(120), nullable=True)
    contact_phone     = db.Column(db.String(40), nullable=True)
    notes             = db.Column(db.String(500), nullable=True)

    # Lifecycle. V1 keeps it small.
    # new → confirmed → delivered  (or cancelled at any point)
    status = db.Column(db.String(20), nullable=False, default='new',
                       index=True)

    # Snapshot total at submit time (sum of item line totals).
    # Re-computed when items are mutated (V1 forbids item mutation
    # after submit, so this is effectively immutable).
    total_amount = db.Column(db.Float, nullable=False, default=0.0)

    # Where the order came from. 'guest_menu' (typed URL) or
    # 'qr_menu' (scanned QR). Set by the route based on a query
    # param. Reports may eventually filter on this.
    source = db.Column(db.String(20), nullable=False, default='guest_menu')

    # Lifecycle stamps (who clicked the button, when).
    confirmed_at = db.Column(db.DateTime, nullable=True)
    confirmed_by_user_id = db.Column(
        db.Integer, db.ForeignKey('users.id', ondelete='SET NULL'),
        nullable=True,
    )
    delivered_at = db.Column(db.DateTime, nullable=True)
    delivered_by_user_id = db.Column(
        db.Integer, db.ForeignKey('users.id', ondelete='SET NULL'),
        nullable=True,
    )
    cancelled_at = db.Column(db.DateTime, nullable=True)
    cancelled_by_user_id = db.Column(
        db.Integer, db.ForeignKey('users.id', ondelete='SET NULL'),
        nullable=True,
    )
    cancel_reason = db.Column(db.String(255), nullable=True)

    # Folio link. Populated when staff clicks "Post to room" on the
    # admin queue. Stores a comma-joined string of FolioItem ids; the
    # actual rows live in the folio_items table.
    posted_to_folio_at = db.Column(db.DateTime, nullable=True)
    posted_by_user_id = db.Column(
        db.Integer, db.ForeignKey('users.id', ondelete='SET NULL'),
        nullable=True,
    )
    folio_item_ids = db.Column(db.String(255), nullable=True)

    metadata_json = db.Column(db.Text, nullable=True)

    items = db.relationship('GuestOrderItem', backref='order',
                            lazy='dynamic',
                            cascade='all, delete-orphan',
                            order_by='GuestOrderItem.id')

    booking = db.relationship('Booking',
                              foreign_keys=[booking_id],
                              backref='guest_orders')

    @property
    def is_open(self):
        return self.status in ('new', 'confirmed')

    @property
    def is_posted_to_folio(self):
        return self.posted_to_folio_at is not None

    def __repr__(self):
        return (f'<GuestOrder #{self.id} {self.status} '
                f'token={self.public_token[:6]}…>')


class GuestOrderItem(db.Model):
    __tablename__ = 'guest_order_items'

    id        = db.Column(db.Integer, primary_key=True)
    order_id  = db.Column(
        db.Integer, db.ForeignKey('guest_orders.id', ondelete='CASCADE'),
        nullable=False,
    )
    # Reference is preserved so the admin can see which menu item the
    # order originally pointed at. SET NULL on delete because the menu
    # owner may safely retire an item without rewriting history; the
    # snapshot fields below carry the displayed name + price.
    pos_item_id = db.Column(
        db.Integer, db.ForeignKey('pos_items.id', ondelete='SET NULL'),
        nullable=True,
    )
    # Snapshots — frozen at submit time.
    item_name_snapshot  = db.Column(db.String(120), nullable=False)
    item_type_snapshot  = db.Column(db.String(30), nullable=False,
                                    default='restaurant')
    unit_price          = db.Column(db.Float, nullable=False, default=0.0)
    quantity            = db.Column(db.Float, nullable=False, default=1.0)
    line_total          = db.Column(db.Float, nullable=False, default=0.0)
    note                = db.Column(db.String(255), nullable=True)

    pos_item = db.relationship('PosItem', foreign_keys=[pos_item_id])

    def __repr__(self):
        return (f'<GuestOrderItem {self.item_name_snapshot}× {self.quantity} '
                f'@{self.unit_price}>')


# ── Group Bookings / Master Folios V1 ───────────────────────────────
#
# A `BookingGroup` ties multiple `Booking` rows together under one
# operational identity (a wedding party, a tour, a corporate stay).
# Each member booking can opt in to MASTER billing (`billing_target=
# 'master'`) or stay on its own folio (`billing_target='individual'`).
#
# Master billing is implemented by designating ONE member booking as
# the group's billing account (`group.master_booking_id`). Operators
# explicitly choose where to post each ad-hoc charge — V1 NEVER auto-
# rolls items between folios. Each FolioItem has exactly one booking_id,
# so a charge can never be counted twice.
#
# Mixed billing (some charges to master, others to individual) IS
# supported by virtue of the explicit-target rule; what is DEFERRED is
# automatic split-billing of room revenue (Phase 2). For V1, room
# revenue (Booking.total_amount) stays tied to its booking; only ad-
# hoc folio items can flow to the master account.


class BookingGroup(db.Model):
    """A group of linked bookings with optional master-folio billing."""
    __tablename__ = 'booking_groups'

    id          = db.Column(db.Integer, primary_key=True)
    # Multi-Property V1 — a group lives entirely on one property.
    # All member bookings must share this property_id.
    property_id = db.Column(
        db.Integer, db.ForeignKey('properties.id', ondelete='RESTRICT'),
        nullable=False, index=True,
        server_default='1',
    )
    created_at  = db.Column(db.DateTime, default=datetime.utcnow,
                            nullable=False)
    updated_at  = db.Column(db.DateTime, default=datetime.utcnow,
                            onupdate=datetime.utcnow, nullable=False)

    # Short, unique, operator-friendly code (e.g. "MARWED-2026").
    group_code  = db.Column(db.String(40), unique=True, nullable=False,
                            index=True)
    group_name  = db.Column(db.String(160), nullable=False)

    # Optional contact guest — usually the trip organizer / wedding
    # planner. Not necessarily a member booking's guest.
    primary_contact_guest_id = db.Column(
        db.Integer, db.ForeignKey('guests.id', ondelete='SET NULL'),
        nullable=True,
    )

    # The "billing account" booking. NULL means no master-folio
    # billing is in effect; all members bill to themselves regardless
    # of `billing_target`. When set, member bookings whose
    # `billing_target='master'` post their ad-hoc charges to this row.
    master_booking_id = db.Column(
        db.Integer, db.ForeignKey('bookings.id', ondelete='SET NULL'),
        nullable=True,
    )

    # 'individual' (V1 default), 'master', 'mixed'. The column is a
    # high-level intent indicator; the per-charge routing is decided
    # by Booking.billing_target + operator UI.
    billing_mode = db.Column(db.String(20), nullable=False,
                             default='individual')

    # 'active' | 'cancelled' | 'completed'
    status      = db.Column(db.String(20), nullable=False, default='active')

    notes       = db.Column(db.Text, nullable=True)
    metadata_json = db.Column(db.Text, nullable=True)

    primary_contact = db.relationship(
        'Guest', foreign_keys=[primary_contact_guest_id])
    master_booking  = db.relationship(
        'Booking', foreign_keys=[master_booking_id],
        post_update=True,
    )

    # Member bookings — backref defined on Booking below.

    def __repr__(self):
        return f'<BookingGroup {self.group_code}: {self.group_name}>'


# ── Property Settings / Branding Foundation V1 ──────────────────────
#
# A SINGLETON row that holds property-wide branding + operational
# settings (name, contact, currency, bank details, tax rates,
# policies). Replaces a scattered web of env vars and hardcoded
# constants with one editable source of truth.
#
# V1 deliberately stays single-property — there is at most one
# `PropertySettings` row per environment. The migration seeds the
# row at upgrade time so every read path always finds something.
# Future multi-property work will extend this with a property_id
# foreign key everywhere; for now, the single-row pattern keeps the
# blast radius small.
#
# Sensitive note on bank details:
#   `bank_account_number` is moderately sensitive. The audit row
#   written by `services.property_settings.update_settings` records
#   ONLY the list of changed field names, never the values.


class PropertySettings(db.Model):
    __tablename__ = 'property_settings'

    id          = db.Column(db.Integer, primary_key=True)
    created_at  = db.Column(db.DateTime, default=datetime.utcnow,
                            nullable=False)
    updated_at  = db.Column(db.DateTime, default=datetime.utcnow,
                            onupdate=datetime.utcnow, nullable=False)

    # ── Branding ───────────────────────────────────────────────
    property_name        = db.Column(db.String(160), nullable=False)
    short_name           = db.Column(db.String(80),  nullable=True)
    tagline              = db.Column(db.String(255), nullable=True)
    logo_path            = db.Column(db.String(255), nullable=True)
    primary_color        = db.Column(db.String(16),  nullable=True)
    website_url          = db.Column(db.String(255), nullable=True)

    # ── Contact ────────────────────────────────────────────────
    email                = db.Column(db.String(120), nullable=True)
    phone                = db.Column(db.String(40),  nullable=True)
    whatsapp_number      = db.Column(db.String(40),  nullable=True)
    address              = db.Column(db.String(255), nullable=True)
    city                 = db.Column(db.String(80),  nullable=True)
    country              = db.Column(db.String(80),  nullable=True)

    # ── Operational ────────────────────────────────────────────
    currency_code        = db.Column(db.String(8),   nullable=False,
                                     default='USD')
    timezone             = db.Column(db.String(64),  nullable=False,
                                     default='Indian/Maldives')
    check_in_time        = db.Column(db.String(8),   nullable=True)
                            # e.g. '14:00', stored as string for V1
    check_out_time       = db.Column(db.String(8),   nullable=True)

    # ── Billing ────────────────────────────────────────────────
    invoice_display_name = db.Column(db.String(160), nullable=True)
    payment_instructions_text = db.Column(db.Text,   nullable=True)
    bank_name            = db.Column(db.String(120), nullable=True)
    bank_account_name    = db.Column(db.String(120), nullable=True)
    bank_account_number  = db.Column(db.String(60),  nullable=True)

    # ── Tax / service charge (basics) ─────────────────────────
    # Coarse fields for V1 — see docs/channel_manager_architecture.md
    # §2 for the full TaxRule design that comes later.
    tax_name             = db.Column(db.String(40),  nullable=True)
    tax_rate             = db.Column(db.Float,       nullable=True)
                            # percentage e.g. 12.0 for 12%
    service_charge_rate  = db.Column(db.Float,       nullable=True)

    # ── Policies (free-form for V1) ───────────────────────────
    booking_terms        = db.Column(db.Text, nullable=True)
    cancellation_policy  = db.Column(db.Text, nullable=True)
    wifi_info            = db.Column(db.Text, nullable=True)

    # ── Lifecycle ─────────────────────────────────────────────
    is_active            = db.Column(db.Boolean, nullable=False,
                                     default=True)

    def __repr__(self):
        return f'<PropertySettings id={self.id} name={self.property_name!r}>'


# ── Multi-Property Foundation V1 ────────────────────────────────────
#
# `Property` is the canonical lightweight identity row that every
# operational model in the platform points at via `property_id`.
# `PropertySettings` (introduced in Property Settings V1) is the
# rich config singleton — branding text, bank account, payment
# instructions, policies. The two are linked 1:1 via
# `Property.settings_id`.
#
# WHY TWO TABLES:
#   - `Property` is short, indexable, FK-pointed-at by ~12 tables.
#     Keeping it lean keeps the FK indexes small and queries fast.
#   - `PropertySettings` is wide and edited only via /admin/property-
#     settings/. Decoupling avoids "every booking's FK target row
#     gets bumped every time someone edits the wifi password."
#
# V1 STATIC: there is exactly one Property row per environment. The
# migration seeds it. Multi-property activation (a second row + UI
# switcher + per-property roles) is a deliberate Phase 6 effort
# documented in docs/multi_property_migration_strategy.md.
#
# The two tables MAY be merged later, but only after multi-property
# is fully shipped and we have signal on edit-frequency vs
# query-load.


class Property(db.Model):
    __tablename__ = 'properties'

    id          = db.Column(db.Integer, primary_key=True)
    created_at  = db.Column(db.DateTime, default=datetime.utcnow,
                            nullable=False)
    updated_at  = db.Column(db.DateTime, default=datetime.utcnow,
                            onupdate=datetime.utcnow, nullable=False)

    # Short URL-safe identifier (e.g. 'sheeza', 'maakanaa'). Reserved
    # for future /p/<code>/* URL routing. V1 always 'default'.
    code        = db.Column(db.String(40), unique=True, nullable=False,
                            index=True)

    # Display name + short name. These mirror PropertySettings at seed
    # time so we have a name even if settings_id is somehow NULL. The
    # PropertySettings row is the canonical source for the UI.
    name        = db.Column(db.String(160), nullable=False)
    short_name  = db.Column(db.String(80),  nullable=True)

    # Operational defaults. Mirror PropertySettings on seed so a
    # Property without settings still has answers.
    timezone      = db.Column(db.String(64), nullable=False,
                              default='Indian/Maldives')
    currency_code = db.Column(db.String(8), nullable=False, default='USD')

    # Soft-disable lever. Disabled properties keep their data but
    # block writes (Phase 6 enforcement; harmless V1).
    is_active   = db.Column(db.Boolean, nullable=False, default=True)

    notes       = db.Column(db.Text, nullable=True)

    # 1:1 link to the PropertySettings row that holds the rich
    # branding/billing/policy config. Nullable for safety during
    # migration — the seed migration sets it to PropertySettings #1.
    settings_id = db.Column(
        db.Integer, db.ForeignKey('property_settings.id',
                                  ondelete='SET NULL'),
        nullable=True, index=True,
    )

    settings = db.relationship('PropertySettings',
                                foreign_keys=[settings_id],
                                lazy='joined')

    def __repr__(self):
        return f'<Property id={self.id} code={self.code!r} name={self.name!r}>'


# ── Channel Manager Foundation V1 ───────────────────────────────────
#
# Internal data + workflow scaffolding for FUTURE OTA integration.
# V1 makes ZERO real OTA API calls. The tables exist so we can:
#   - record which channels we've configured (per-property)
#   - map our internal RoomType + RatePlan → external IDs
#   - write a sync-job log even when nothing actually syncs
#   - prove the leak / duplicate-import guards work in tests
#
# When real sync ships (channel manager Phase 4 in
# docs/channel_manager_build_phases.md), the sync workers read from
# these tables and write back via ChannelSyncJob/ChannelSyncLog
# rows. Until then, these tables are read-only from the operator's
# perspective and write-only via the admin "test sync" button which
# logs no-op events.
#
# Sensitive note on credentials:
#   `ChannelConnection.config_json` is intentionally NOT used for
#   real API secrets in V1. The Phase 4 design documented in
#   docs/channel_manager_architecture.md §8 stores credentials
#   in environment variables / secret manager via a `secret_ref`
#   column added later — it is NOT in this V1 schema.


class ChannelConnection(db.Model):
    """One per (Property × external channel) configuration row."""
    __tablename__ = 'channel_connections'

    id          = db.Column(db.Integer, primary_key=True)
    created_at  = db.Column(db.DateTime, default=datetime.utcnow,
                            nullable=False)
    updated_at  = db.Column(db.DateTime, default=datetime.utcnow,
                            onupdate=datetime.utcnow, nullable=False)

    property_id = db.Column(
        db.Integer, db.ForeignKey('properties.id', ondelete='RESTRICT'),
        nullable=False, index=True, server_default='1',
    )

    # Whitelisted in services.channels.CHANNEL_NAMES. Examples:
    # booking_com / expedia / agoda / airbnb / other.
    channel_name = db.Column(db.String(40), nullable=False, index=True)

    # 'inactive' | 'sandbox' | 'active' | 'error'.
    status      = db.Column(db.String(20), nullable=False,
                            default='inactive', index=True)

    # Operator-facing label so multi-property owners can disambiguate
    # (e.g. "Booking.com — Sheeza Manzil HotelID 12345").
    account_label = db.Column(db.String(160), nullable=True)

    # JSON blob of NON-SECRET config (display preferences, mapping
    # hints, etc.). Real API keys NEVER go here — they belong in
    # env vars / a secret manager. V1 enforces this by convention.
    config_json = db.Column(db.Text, nullable=True)

    notes       = db.Column(db.Text, nullable=True)

    # Convenience back-refs used by mapping pages.
    room_maps = db.relationship('ChannelRoomMap',
                                backref='channel_connection',
                                lazy='dynamic',
                                cascade='all, delete-orphan')
    rate_maps = db.relationship('ChannelRatePlanMap',
                                 backref='channel_connection',
                                 lazy='dynamic',
                                 cascade='all, delete-orphan')

    __table_args__ = (
        db.UniqueConstraint('property_id', 'channel_name',
                             name='uq_channel_connection_property_channel'),
    )

    def __repr__(self):
        return (f'<ChannelConnection id={self.id} '
                f'channel={self.channel_name!r} status={self.status!r}>')


class ChannelRoomMap(db.Model):
    """Maps an internal RoomType to one channel's external room id."""
    __tablename__ = 'channel_room_maps'

    id          = db.Column(db.Integer, primary_key=True)
    created_at  = db.Column(db.DateTime, default=datetime.utcnow,
                            nullable=False)

    channel_connection_id = db.Column(
        db.Integer, db.ForeignKey('channel_connections.id',
                                  ondelete='CASCADE'),
        nullable=False, index=True,
    )
    room_type_id = db.Column(
        db.Integer, db.ForeignKey('room_types.id',
                                  ondelete='CASCADE'),
        nullable=False, index=True,
    )

    # Channel-side identifier. Channel-specific format — opaque to us.
    external_room_id = db.Column(db.String(80), nullable=False)
    # Snapshot of what the channel called it the last time the mapping
    # was edited — purely human-readable.
    external_room_name_snapshot = db.Column(db.String(160), nullable=True)

    # Optional inventory-count override (cap how many of this type we
    # publish). NULL = use full physical count.
    inventory_count_override = db.Column(db.Integer, nullable=True)

    is_active = db.Column(db.Boolean, nullable=False, default=True)
    notes     = db.Column(db.Text, nullable=True)

    __table_args__ = (
        db.UniqueConstraint('channel_connection_id', 'room_type_id',
                             name='uq_channel_room_map_conn_type'),
        db.UniqueConstraint('channel_connection_id', 'external_room_id',
                             name='uq_channel_room_map_conn_external'),
    )

    room_type = db.relationship('RoomType', foreign_keys=[room_type_id])

    def __repr__(self):
        return (f'<ChannelRoomMap conn={self.channel_connection_id} '
                f'type={self.room_type_id} ext={self.external_room_id!r}>')


class ChannelRatePlanMap(db.Model):
    """Maps an internal RatePlan to one channel's external rate plan id."""
    __tablename__ = 'channel_rate_plan_maps'

    id          = db.Column(db.Integer, primary_key=True)
    created_at  = db.Column(db.DateTime, default=datetime.utcnow,
                            nullable=False)

    channel_connection_id = db.Column(
        db.Integer, db.ForeignKey('channel_connections.id',
                                  ondelete='CASCADE'),
        nullable=False, index=True,
    )
    rate_plan_id = db.Column(
        db.Integer, db.ForeignKey('rate_plans.id',
                                  ondelete='CASCADE'),
        nullable=False, index=True,
    )

    external_rate_plan_id = db.Column(db.String(80), nullable=False)
    external_rate_plan_name_snapshot = db.Column(
        db.String(160), nullable=True)

    # Channel-side meal-plan / cancellation-policy refs. Channel-specific.
    meal_plan_external_id = db.Column(db.String(40), nullable=True)
    cancellation_policy_external_id = db.Column(db.String(40), nullable=True)

    is_active = db.Column(db.Boolean, nullable=False, default=True)
    notes     = db.Column(db.Text, nullable=True)

    __table_args__ = (
        db.UniqueConstraint('channel_connection_id', 'rate_plan_id',
                             name='uq_channel_rate_map_conn_plan'),
        db.UniqueConstraint('channel_connection_id',
                             'external_rate_plan_id',
                             name='uq_channel_rate_map_conn_external'),
    )

    rate_plan = db.relationship('RatePlan', foreign_keys=[rate_plan_id])

    def __repr__(self):
        return (f'<ChannelRatePlanMap conn={self.channel_connection_id} '
                f'plan={self.rate_plan_id} ext={self.external_rate_plan_id!r}>')


class ChannelSyncJob(db.Model):
    """One row per scheduled / triggered sync work item.

    V1 jobs are no-ops created by the admin "test sync" button — they
    update status to 'success' or 'skipped' immediately and write a
    matching ChannelSyncLog row. When real sync ships, a worker
    consumes queued jobs.
    """
    __tablename__ = 'channel_sync_jobs'

    id          = db.Column(db.Integer, primary_key=True)
    created_at  = db.Column(db.DateTime, default=datetime.utcnow,
                            nullable=False, index=True)

    channel_connection_id = db.Column(
        db.Integer, db.ForeignKey('channel_connections.id',
                                  ondelete='CASCADE'),
        nullable=False, index=True,
    )

    # Whitelisted in services.channels.SYNC_JOB_TYPES.
    # availability_push / rate_push / restriction_push /
    # reservation_import / reservation_update /
    # cancellation_import / full_resync / test_noop.
    job_type = db.Column(db.String(40), nullable=False)
    # outbound | inbound
    direction = db.Column(db.String(10), nullable=False)
    # queued | running | success | failed | skipped | dead_lettered
    status = db.Column(db.String(20), nullable=False, default='queued',
                        index=True)

    payload_summary = db.Column(db.String(500), nullable=True)
    error_summary   = db.Column(db.String(500), nullable=True)
    attempt_count   = db.Column(db.Integer, nullable=False, default=0)

    started_at   = db.Column(db.DateTime, nullable=True)
    completed_at = db.Column(db.DateTime, nullable=True)

    requested_by_user_id = db.Column(
        db.Integer, db.ForeignKey('users.id', ondelete='SET NULL'),
        nullable=True,
    )

    requested_by = db.relationship(
        'User', foreign_keys=[requested_by_user_id])

    def __repr__(self):
        return (f'<ChannelSyncJob id={self.id} type={self.job_type!r} '
                f'status={self.status!r}>')


class ChannelSyncLog(db.Model):
    """Append-only event log for sync activity. One row per API call
    (real or simulated). V1 only writes test-noop rows."""
    __tablename__ = 'channel_sync_logs'

    id          = db.Column(db.Integer, primary_key=True)
    created_at  = db.Column(db.DateTime, default=datetime.utcnow,
                            nullable=False, index=True)

    channel_connection_id = db.Column(
        db.Integer, db.ForeignKey('channel_connections.id',
                                  ondelete='CASCADE'),
        nullable=False, index=True,
    )
    sync_job_id = db.Column(
        db.Integer, db.ForeignKey('channel_sync_jobs.id',
                                  ondelete='SET NULL'),
        nullable=True, index=True,
    )

    # 'room_map' | 'rate_plan_map' | 'reservation' | 'availability'
    # | 'rate' | 'restriction' | 'connection' | 'test_noop'.
    entity_type = db.Column(db.String(30), nullable=False)
    # Internal id for the entity if applicable (booking.id,
    # room_type.id, etc.). NULL for connection-level events.
    entity_id   = db.Column(db.Integer, nullable=True)

    # outbound (push) | inbound (pull)
    direction   = db.Column(db.String(10), nullable=False)
    # Human-readable verb: e.g. 'pushed', 'imported', 'skipped'.
    action      = db.Column(db.String(40), nullable=False)
    # success | failed | skipped | warning
    status      = db.Column(db.String(20), nullable=False)

    # Short sanitized message (≤500 chars). NEVER stores raw API
    # bodies — those would contain PII / credentials.
    message     = db.Column(db.String(500), nullable=True)

    def __repr__(self):
        return (f'<ChannelSyncLog id={self.id} '
                f'entity={self.entity_type}#{self.entity_id} '
                f'action={self.action!r} status={self.status!r}>')


# ── Mid-stay room change / Stay Segments (foundation) ──────────────
#
# A booking can occupy Room A for part of its stay, then Room B for
# the rest. We model this as one Booking row plus a sequence of
# StaySegment rows, each describing which room hosts the guest for
# which sub-range of the stay. The Booking remains the single source
# of truth for guest, total dates, folio, payments, history.
#
# V1 deliberately stops at the foundation: every segment-aware code
# path is gated behind `Booking.has_segments` (which is False for
# every existing booking until split_stay() is called). The board
# continues to render by Booking.room_id; segment-aware rendering is
# scheduled for the next sprint. This keeps the schema additive and
# safe to ship to staging without UX regressions.
class StaySegment(db.Model):
    __tablename__ = 'stay_segments'

    id          = db.Column(db.Integer, primary_key=True)
    booking_id  = db.Column(db.Integer,
                            db.ForeignKey('bookings.id', ondelete='CASCADE'),
                            nullable=False, index=True)
    room_id     = db.Column(db.Integer,
                            db.ForeignKey('rooms.id', ondelete='RESTRICT'),
                            nullable=False, index=True)
    # Half-open interval matching Booking.check_in_date /
    # check_out_date convention: end_date is the morning the guest
    # leaves this segment (i.e. the next segment's start_date).
    start_date  = db.Column(db.Date, nullable=False)
    end_date    = db.Column(db.Date, nullable=False)
    # Optional human note (e.g. "moved due to AC issue"). Stays
    # internal — never surfaced to the guest.
    notes       = db.Column(db.String(255), nullable=True)
    created_at  = db.Column(db.DateTime, default=datetime.utcnow,
                            nullable=False)
    created_by_user_id = db.Column(db.Integer,
                                    db.ForeignKey('users.id'),
                                    nullable=True)

    booking = db.relationship('Booking', backref=db.backref(
        'stay_segments', lazy='dynamic',
        cascade='all, delete-orphan',
        order_by='StaySegment.start_date.asc()',
    ))
    room    = db.relationship('Room')

    @property
    def nights(self):
        return (self.end_date - self.start_date).days

    def __repr__(self):
        return (f'<StaySegment booking={self.booking_id} '
                f'room={self.room_id} '
                f'{self.start_date}→{self.end_date}>')


# ── Maintenance / Work Orders V1 ────────────────────────────────────
#
# Tracks room/property issues from "reported" through "resolved" so
# operational problems no longer live only in housekeeping notes or
# memory. Integrates with the existing room state machinery:
#   - Marking a work order severe enough flips
#     Room.housekeeping_status='out_of_order' + Room.status='maintenance'
#   - Operators can also create a date-ranged RoomBlock from the work
#     order detail page (existing /board/rooms/<id>/blocks endpoint).
#   - The Reservation Board's conflict-check service already considers
#     RoomBlock + Room.housekeeping_status when validating moves, so
#     OOO rooms are automatically protected from new bookings.
#
# Allowed values (mirrored as tuples on the class so the form handler,
# tests, and template dropdowns share one source of truth):
#   category : plumbing / electrical / hvac / cleaning / furniture /
#              appliance / safety / general
#   priority : low / medium / high / urgent
#   status   : new / assigned / in_progress / waiting / resolved /
#              cancelled
class WorkOrder(db.Model):
    __tablename__ = 'work_orders'

    CATEGORIES = (
        ('plumbing',   'Plumbing'),
        ('electrical', 'Electrical'),
        ('hvac',       'HVAC / climate'),
        ('cleaning',   'Cleaning / linen'),
        ('furniture',  'Furniture / fittings'),
        ('appliance',  'Appliance / electronics'),
        ('safety',     'Safety / security'),
        ('general',    'General'),
    )
    PRIORITIES = (
        ('low',     'Low'),
        ('medium',  'Medium'),
        ('high',    'High'),
        ('urgent',  'Urgent'),
    )
    STATUSES = (
        ('new',         'New'),
        ('assigned',    'Assigned'),
        ('in_progress', 'In progress'),
        ('waiting',     'Waiting on parts / vendor'),
        ('resolved',    'Resolved'),
        ('cancelled',   'Cancelled'),
    )

    id          = db.Column(db.Integer, primary_key=True)
    created_at  = db.Column(db.DateTime, default=datetime.utcnow,
                            nullable=False, index=True)
    updated_at  = db.Column(db.DateTime, default=datetime.utcnow,
                            onupdate=datetime.utcnow, nullable=False)

    # Optional links — many work orders are room-scoped, some are
    # property-wide, and a few are tied to a specific guest stay.
    room_id     = db.Column(db.Integer,
                            db.ForeignKey('rooms.id',
                                          ondelete='SET NULL'),
                            nullable=True, index=True)
    booking_id  = db.Column(db.Integer,
                            db.ForeignKey('bookings.id',
                                          ondelete='SET NULL'),
                            nullable=True, index=True)

    title       = db.Column(db.String(160), nullable=False)
    description = db.Column(db.Text, nullable=True)

    # Whitelisted enums — see CATEGORIES / PRIORITIES / STATUSES above.
    category    = db.Column(db.String(20), nullable=False, default='general')
    priority    = db.Column(db.String(10), nullable=False, default='medium')
    status      = db.Column(db.String(20), nullable=False, default='new',
                            index=True)

    assigned_to_user_id  = db.Column(db.Integer,
                                     db.ForeignKey('users.id'),
                                     nullable=True, index=True)
    reported_by_user_id  = db.Column(db.Integer,
                                     db.ForeignKey('users.id'),
                                     nullable=True)

    due_date          = db.Column(db.Date, nullable=True)
    resolved_at       = db.Column(db.DateTime, nullable=True)
    resolution_notes  = db.Column(db.String(1000), nullable=True)

    metadata_json     = db.Column(db.Text, nullable=True)

    # Relationships — read-only convenience; cascades stay null-safe.
    room        = db.relationship('Room',     foreign_keys=[room_id])
    booking     = db.relationship('Booking',  foreign_keys=[booking_id])
    assigned_to = db.relationship('User',     foreign_keys=[assigned_to_user_id])
    reported_by = db.relationship('User',     foreign_keys=[reported_by_user_id])

    @property
    def is_open(self) -> bool:
        return self.status not in ('resolved', 'cancelled')

    @property
    def category_label(self) -> str:
        for slug, label in self.CATEGORIES:
            if slug == self.category:
                return label
        return self.category or '—'

    @property
    def priority_label(self) -> str:
        for slug, label in self.PRIORITIES:
            if slug == self.priority:
                return label
        return self.priority or '—'

    @property
    def status_label(self) -> str:
        for slug, label in self.STATUSES:
            if slug == self.status:
                return label
        return self.status or '—'

    def __repr__(self):
        return (f'<WorkOrder id={self.id} room_id={self.room_id} '
                f'priority={self.priority!r} status={self.status!r}>')
