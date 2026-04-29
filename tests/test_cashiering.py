"""Tests for Cashiering V1 — payment posting + void.

Hard rules covered:
  - Routes admin-gated; anonymous + staff blocked
  - Validation: amount > 0, method whitelist, transaction_type whitelist,
    placeholder text in notes rejected
  - Posting a payment creates BOTH a CashierTransaction AND a linked
    FolioItem with correct signed total
  - Folio balance is reduced by the payment amount (matches existing
    folio_balance() math; no double-counting)
  - Cashier user_id captured from current_user
  - Reference number stored; reference_number_present flag in audit
    metadata (never the value itself)
  - Voiding a transaction sets BOTH txn AND linked folio_item to
    voided; balance recomputes correctly
  - Refund creates a positive folio_item (increases balance)
  - Booking.status NEVER changes
  - Invoice.payment_status / amount_paid NEVER change
  - No WhatsApp / email / Gemini calls
  - Audit metadata is a strict whitelist
  - Migration file shape: parents off e7c1a4b89d62, only creates
    cashier_transactions
"""

from __future__ import annotations

import json
import os
import re
import unittest
from datetime import date, datetime, timedelta
from pathlib import Path
from unittest import mock

for _v in ('DATABASE_URL',
           'AI_DRAFT_PROVIDER', 'GEMINI_API_KEY', 'ANTHROPIC_API_KEY',
           'WHATSAPP_ENABLED', 'WHATSAPP_TOKEN'):
    os.environ.pop(_v, None)
os.environ.setdefault('SECRET_KEY', 'test-secret-do-not-use-in-prod')

from config import Config                                       # noqa: E402
from app import create_app                                      # noqa: E402
from app.models import (                                        # noqa: E402
    db, User, Room, Guest, Booking, Invoice, ActivityLog,
    FolioItem, CashierTransaction,
)
from app.services import cashiering as cashier_svc              # noqa: E402
from app.services import folio as folio_svc                     # noqa: E402
from app.services import whatsapp as wa                         # noqa: E402
from app.services import ai_drafts                              # noqa: E402


_REPO_ROOT = Path(__file__).resolve().parent.parent


class _TestConfig(Config):
    TESTING = True
    SQLALCHEMY_DATABASE_URI = 'sqlite:///:memory:'
    WTF_CSRF_ENABLED = False
    WHATSAPP_ENABLED = False


def _make_app():
    return create_app(_TestConfig)


def _seed_users():
    admin = User(username='cashier_admin', email='a@x', role='admin')
    admin.set_password('aaaaaaaaaa1')
    staff = User(username='cashier_staff', email='s@x', role='staff')
    staff.set_password('aaaaaaaaaa1')
    db.session.add_all([admin, staff])
    db.session.commit()
    return admin.id, staff.id


def _seed_booking_with_invoice(*, total=2400.0, paid=0.0,
                               payment_status='unpaid'):
    g = Guest(first_name='Hassan', last_name='Demo',
              phone='+9607000001', email='h@x')
    db.session.add(g)
    room = Room(number='99', name='Test', room_type='Test',
                floor=0, capacity=2, price_per_night=600.0)
    db.session.add(room)
    db.session.commit()
    b = Booking(
        booking_ref='BKCASH001',
        room_id=room.id, guest_id=g.id,
        check_in_date=date.today() + timedelta(days=2),
        check_out_date=date.today() + timedelta(days=6),
        num_guests=1, total_amount=total,
        status='confirmed',
    )
    db.session.add(b)
    db.session.commit()
    inv = Invoice(
        booking_id=b.id, invoice_number='INV-CASH001',
        total_amount=total, payment_status=payment_status,
        amount_paid=paid,
    )
    db.session.add(inv)
    db.session.commit()
    return b


# ─────────────────────────────────────────────────────────────────────
# 1) Pure helpers
# ─────────────────────────────────────────────────────────────────────

class CashierValidationTests(unittest.TestCase):

    def test_payment_method_normalization(self):
        self.assertEqual(cashier_svc.normalize_payment_method('CASH'), 'cash')
        self.assertEqual(cashier_svc.normalize_payment_method(' Bank_Transfer '),
                         'bank_transfer')
        self.assertIsNone(cashier_svc.normalize_payment_method('crypto'))
        self.assertIsNone(cashier_svc.normalize_payment_method(None))

    def test_amount_required_and_positive(self):
        r = cashier_svc.validate_payment_input(
            amount='', payment_method='cash')
        self.assertTrue(any('amount' in e for e in r['errors']))
        r = cashier_svc.validate_payment_input(
            amount='-50', payment_method='cash')
        self.assertTrue(any('amount' in e for e in r['errors']))
        r = cashier_svc.validate_payment_input(
            amount='100', payment_method='cash')
        self.assertEqual(r['errors'], [])

    def test_invalid_payment_method_rejected(self):
        r = cashier_svc.validate_payment_input(
            amount='100', payment_method='crypto')
        self.assertTrue(any('payment_method' in e for e in r['errors']))

    def test_invalid_transaction_type_rejected(self):
        r = cashier_svc.validate_payment_input(
            amount='100', payment_method='cash',
            transaction_type='hack')
        self.assertTrue(any('transaction_type' in e for e in r['errors']))

    def test_admin_placeholder_in_notes_rejected(self):
        r = cashier_svc.validate_payment_input(
            amount='100', payment_method='cash',
            notes='Card last-4 [admin: insert here]')
        self.assertTrue(any('placeholder' in e for e in r['errors']))


# ─────────────────────────────────────────────────────────────────────
# 2) Route auth
# ─────────────────────────────────────────────────────────────────────

class _RouteBase(unittest.TestCase):

    def setUp(self):
        self.app = _make_app()
        self.ctx = self.app.app_context()
        self.ctx.push()
        db.create_all()
        self.admin_id, self.staff_id = _seed_users()
        self.booking = _seed_booking_with_invoice()
        self.client = self.app.test_client()

    def tearDown(self):
        db.session.remove()
        db.drop_all()
        self.ctx.pop()

    def _login(self, uid):
        with self.client.session_transaction() as sess:
            sess['_user_id'] = str(uid)
            sess['_fresh'] = True


class CashieringAuthTests(_RouteBase):

    def test_post_payment_anonymous_blocked(self):
        r = self.client.post(
            f'/bookings/{self.booking.id}/cashier/post-payment',
            data={'amount': '100', 'payment_method': 'cash'},
        )
        self.assertIn(r.status_code, (301, 302, 401))
        self.assertEqual(CashierTransaction.query.count(), 0)

    def test_post_payment_staff_blocked(self):
        self._login(self.staff_id)
        r = self.client.post(
            f'/bookings/{self.booking.id}/cashier/post-payment',
            data={'amount': '100', 'payment_method': 'cash'},
        )
        self.assertIn(r.status_code, (302, 401, 403))
        self.assertEqual(CashierTransaction.query.count(), 0)

    def test_void_anonymous_blocked(self):
        r = self.client.post(
            f'/bookings/{self.booking.id}/cashier/void-transaction/1',
        )
        self.assertIn(r.status_code, (301, 302, 401))


# ─────────────────────────────────────────────────────────────────────
# 3) Payment posting — happy path
# ─────────────────────────────────────────────────────────────────────

class PostPaymentTests(_RouteBase):

    def setUp(self):
        super().setUp()
        self._login(self.admin_id)

    def test_clean_payment_creates_txn_and_folio_item(self):
        r = self.client.post(
            f'/bookings/{self.booking.id}/cashier/post-payment',
            data={
                'amount':           '600',
                'payment_method':   'bank_transfer',
                'reference_number': 'SLIP-12345',
                'notes':            'Cash on arrival',
            },
            follow_redirects=False,
        )
        self.assertIn(r.status_code, (301, 302))

        # Exactly one txn + one folio item
        self.assertEqual(CashierTransaction.query.count(), 1)
        self.assertEqual(
            FolioItem.query.filter_by(booking_id=self.booking.id).count(),
            1,
        )
        txn = CashierTransaction.query.first()
        item = FolioItem.query.filter_by(booking_id=self.booking.id).first()

        # Cross-link
        self.assertEqual(txn.folio_item_id, item.id)
        # Cashier identity captured
        self.assertEqual(txn.received_by_user_id, self.admin_id)
        # Reference number stored
        self.assertEqual(txn.reference_number, 'SLIP-12345')
        # Method + status
        self.assertEqual(txn.payment_method, 'bank_transfer')
        self.assertEqual(txn.status, 'posted')
        # Folio item is signed-negative (reduces balance)
        self.assertEqual(item.total_amount, -600.0)
        self.assertEqual(item.item_type, 'payment')

    def test_payment_reduces_folio_balance(self):
        # Pre-seed a charge so balance starts non-zero
        charge = FolioItem(
            booking_id=self.booking.id,
            guest_id=self.booking.guest_id,
            item_type='room_charge', description='Room night',
            quantity=1.0, unit_price=600.0, amount=600.0,
            total_amount=600.0, status='open', source_module='manual',
        )
        db.session.add(charge); db.session.commit()
        self.assertEqual(folio_svc.folio_balance(self.booking), 600.0)

        # Post a 400 payment
        self.client.post(
            f'/bookings/{self.booking.id}/cashier/post-payment',
            data={'amount': '400', 'payment_method': 'cash'},
        )
        self.assertEqual(folio_svc.folio_balance(self.booking), 200.0)

    def test_partial_payments_aggregate(self):
        # Pre-seed a 1200 charge
        charge = FolioItem(
            booking_id=self.booking.id,
            guest_id=self.booking.guest_id,
            item_type='room_charge', description='Room',
            quantity=1.0, unit_price=1200.0, amount=1200.0,
            total_amount=1200.0, status='open', source_module='manual',
        )
        db.session.add(charge); db.session.commit()

        # Three partial payments
        for amt in ('300', '400', '200'):
            self.client.post(
                f'/bookings/{self.booking.id}/cashier/post-payment',
                data={'amount': amt, 'payment_method': 'cash'},
            )
        self.assertEqual(CashierTransaction.query.count(), 3)
        # Balance: 1200 - 900 = 300 outstanding
        self.assertEqual(folio_svc.folio_balance(self.booking), 300.0)

    def test_invalid_amount_rejected(self):
        r = self.client.post(
            f'/bookings/{self.booking.id}/cashier/post-payment',
            data={'amount': '-50', 'payment_method': 'cash'},
        )
        self.assertIn(r.status_code, (301, 302))
        self.assertEqual(CashierTransaction.query.count(), 0)

    def test_invalid_method_rejected(self):
        r = self.client.post(
            f'/bookings/{self.booking.id}/cashier/post-payment',
            data={'amount': '100', 'payment_method': 'crypto'},
        )
        self.assertIn(r.status_code, (301, 302))
        self.assertEqual(CashierTransaction.query.count(), 0)


# ─────────────────────────────────────────────────────────────────────
# 4) Refund support
# ─────────────────────────────────────────────────────────────────────

class RefundTests(_RouteBase):

    def setUp(self):
        super().setUp()
        self._login(self.admin_id)

    def test_refund_creates_positive_folio_item(self):
        r = self.client.post(
            f'/bookings/{self.booking.id}/cashier/post-payment',
            data={
                'amount':           '200',
                'payment_method':   'bank_transfer',
                'transaction_type': 'refund',
                'reference_number': 'REFUND-1',
            },
        )
        self.assertIn(r.status_code, (301, 302))
        txn = CashierTransaction.query.first()
        self.assertEqual(txn.transaction_type, 'refund')
        item = FolioItem.query.first()
        # Refund increases balance → folio_item is positive
        self.assertEqual(item.total_amount, 200.0)


# ─────────────────────────────────────────────────────────────────────
# 5) Voiding
# ─────────────────────────────────────────────────────────────────────

class VoidTransactionTests(_RouteBase):

    def setUp(self):
        super().setUp()
        self._login(self.admin_id)
        # Pre-seed a charge + payment
        charge = FolioItem(
            booking_id=self.booking.id,
            guest_id=self.booking.guest_id,
            item_type='room_charge', description='Room',
            quantity=1.0, unit_price=600.0, amount=600.0,
            total_amount=600.0, status='open', source_module='manual',
        )
        db.session.add(charge); db.session.commit()
        self.client.post(
            f'/bookings/{self.booking.id}/cashier/post-payment',
            data={'amount': '600', 'payment_method': 'cash'},
        )
        self.txn = CashierTransaction.query.first()

    def test_void_marks_both_txn_and_folio_item_voided(self):
        # Balance is 0 before void (charge + payment cancel)
        self.assertEqual(folio_svc.folio_balance(self.booking), 0.0)

        r = self.client.post(
            f'/bookings/{self.booking.id}/cashier/void-transaction/{self.txn.id}',
            data={'void_reason': 'guest paid in cash instead'},
        )
        self.assertIn(r.status_code, (301, 302))

        txn = CashierTransaction.query.get(self.txn.id)
        self.assertEqual(txn.status, 'voided')
        self.assertIsNotNone(txn.voided_at)
        self.assertEqual(txn.voided_by_user_id, self.admin_id)
        self.assertEqual(txn.void_reason, 'guest paid in cash instead')

        # Linked folio item also voided → balance jumps back to 600
        item = FolioItem.query.get(txn.folio_item_id)
        self.assertEqual(item.status, 'voided')
        self.assertEqual(folio_svc.folio_balance(self.booking), 600.0)

    def test_double_void_rejected(self):
        self.client.post(
            f'/bookings/{self.booking.id}/cashier/void-transaction/{self.txn.id}',
        )
        r = self.client.post(
            f'/bookings/{self.booking.id}/cashier/void-transaction/{self.txn.id}',
        )
        self.assertIn(r.status_code, (301, 302))
        # Still only ONE void audit row
        rows = (ActivityLog.query
                .filter(ActivityLog.action == 'cashier.payment_voided').all())
        self.assertEqual(len(rows), 1)


# ─────────────────────────────────────────────────────────────────────
# 6) Audit log + safety
# ─────────────────────────────────────────────────────────────────────

class CashieringAuditTests(_RouteBase):

    def setUp(self):
        super().setUp()
        self._login(self.admin_id)

    def test_audit_row_written_with_safe_metadata(self):
        self.client.post(
            f'/bookings/{self.booking.id}/cashier/post-payment',
            data={
                'amount':           '600',
                'payment_method':   'card',
                'reference_number': '4242',  # last-4
            },
        )
        rows = (ActivityLog.query
                .filter(ActivityLog.action == 'cashier.payment_posted').all())
        self.assertEqual(len(rows), 1)
        meta = json.loads(rows[0].metadata_json or '{}')

        # Allowed keys
        for k in ('booking_id', 'booking_ref', 'cashier_transaction_id',
                  'payment_method', 'amount', 'status',
                  'reference_number_present'):
            self.assertIn(k, meta, f'missing whitelisted key {k}')

        # Reference flag, NOT the value itself
        self.assertTrue(meta['reference_number_present'])
        self.assertNotIn('4242', json.dumps(meta))

        # No raw notes / sensitive values
        meta_blob = json.dumps(meta).lower()
        self.assertNotIn('passport', meta_blob)
        self.assertNotIn('slip', meta_blob)


class CashieringNoSideEffectTests(_RouteBase):

    def setUp(self):
        super().setUp()
        self._login(self.admin_id)

    def test_payment_post_does_not_change_booking_status(self):
        before = self.booking.status
        self.client.post(
            f'/bookings/{self.booking.id}/cashier/post-payment',
            data={'amount': '600', 'payment_method': 'cash'},
        )
        b = Booking.query.get(self.booking.id)
        self.assertEqual(b.status, before)

    def test_payment_post_does_not_change_invoice_payment_status(self):
        # V1 explicit deferral: cashiering does NOT touch Invoice.amount_paid
        # or Invoice.payment_status. That reconciliation is Phase 4 work.
        inv_before = Invoice.query.filter_by(booking_id=self.booking.id).first()
        before_status = inv_before.payment_status
        before_paid = inv_before.amount_paid

        self.client.post(
            f'/bookings/{self.booking.id}/cashier/post-payment',
            data={'amount': '600', 'payment_method': 'cash'},
        )
        inv_after = Invoice.query.filter_by(booking_id=self.booking.id).first()
        self.assertEqual(inv_after.payment_status, before_status)
        self.assertEqual(inv_after.amount_paid, before_paid)

    def test_payment_post_no_whatsapp_or_gemini(self):
        with mock.patch.object(wa, 'send_text_message') as m_send, \
             mock.patch.object(ai_drafts, '_call_provider') as m_ai:
            self.client.post(
                f'/bookings/{self.booking.id}/cashier/post-payment',
                data={'amount': '600', 'payment_method': 'cash'},
            )
        m_send.assert_not_called()
        m_ai.assert_not_called()


# ─────────────────────────────────────────────────────────────────────
# 7) Booking detail panel renders cashier UI
# ─────────────────────────────────────────────────────────────────────

class BookingDetailReceiptsPanelTests(_RouteBase):

    def setUp(self):
        super().setUp()
        self._login(self.admin_id)

    def test_panel_renders_with_post_form(self):
        r = self.client.get(f'/bookings/{self.booking.id}')
        self.assertEqual(r.status_code, 200)
        self.assertIn(b'Receipts &amp; Payments', r.data)
        self.assertIn(b'Post Payment', r.data)
        self.assertIn(b'Cashiering V1', r.data)

    def test_panel_lists_active_transactions(self):
        self.client.post(
            f'/bookings/{self.booking.id}/cashier/post-payment',
            data={'amount': '600', 'payment_method': 'cash',
                  'reference_number': 'CASH-A1'},
        )
        r = self.client.get(f'/bookings/{self.booking.id}')
        self.assertIn(b'CASH-A1', r.data)
        self.assertIn(b'Cash', r.data)


# ─────────────────────────────────────────────────────────────────────
# 8) Migration shape
# ─────────────────────────────────────────────────────────────────────

class CashieringMigrationTests(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        cls.path = (_REPO_ROOT / 'migrations' / 'versions'
                    / 'f1c5b2a93e80_add_cashier_transactions_table.py')

    def test_file_exists(self):
        self.assertTrue(self.path.exists(), self.path)

    def test_chain(self):
        text = self.path.read_text()
        self.assertIn("revision = 'f1c5b2a93e80'", text)
        self.assertIn("down_revision = 'e7c1a4b89d62'", text)

    def test_creates_only_cashier_transactions(self):
        text = self.path.read_text()
        # Must create cashier_transactions
        self.assertIn("create_table(\n        'cashier_transactions'", text)
        # Must NOT create any other table
        other = re.findall(r"create_table\(\s*'([^']+)'", text)
        self.assertEqual(other, ['cashier_transactions'])
        # Must NOT alter any existing table
        self.assertNotIn('alter_table', text)
        self.assertNotIn('add_column', text)
        self.assertNotIn('drop_column', text)

    def test_downgrade_drops_only_cashier_transactions(self):
        text = self.path.read_text()
        dropped = re.findall(r"drop_table\('([^']+)'", text)
        self.assertEqual(dropped, ['cashier_transactions'])


if __name__ == '__main__':
    unittest.main()
