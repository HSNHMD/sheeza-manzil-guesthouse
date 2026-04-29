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
