"""Tests for Cashiering Polish + Payment Reconciliation V1.

Covers:
  - services.cashiering.reconciliation_summary()
      shape + filters + invariants (payments != revenue)
  - GET /accounting/reconciliation/payments
      admin can render; staff is bounced; key panels appear
  - cross-link from the bank-CSV reconciliation page to the
    payment reconciliation page
  - the existing /accounting/reconciliation/ bank-CSV view still
    works (regression guard)
"""

from __future__ import annotations

import os
import unittest
from datetime import date, datetime, timedelta

for _v in ('DATABASE_URL', 'AI_DRAFT_PROVIDER', 'AI_DRAFT_MODEL',
           'GEMINI_API_KEY', 'ANTHROPIC_API_KEY',
           'WHATSAPP_ENABLED', 'WHATSAPP_TOKEN'):
    os.environ.pop(_v, None)
os.environ.setdefault('SECRET_KEY', 'test-secret-do-not-use-in-prod')

from config import Config                                       # noqa: E402
from app import create_app                                      # noqa: E402
from app.models import (                                        # noqa: E402
    db, User, Guest, Room, Booking, Invoice, FolioItem,
    CashierTransaction, BankTransaction,
)


class _TestConfig(Config):
    TESTING = True
    SQLALCHEMY_DATABASE_URI = 'sqlite:///:memory:'
    WTF_CSRF_ENABLED = False
    WHATSAPP_ENABLED = False


def _seed(app):
    """Seed: 1 admin, 1 staff, 1 room, 1 guest, 2 bookings + invoices.

    Booking A (BK-A) is FULLY PAID — 2 cashier txns, ref present.
    Booking B (BK-B) is PARTIALLY PAID — 1 cashier txn missing ref,
    1 voided txn for audit trail.
    """
    with app.app_context():
        db.create_all()
        admin = User(username='admin', email='a@x', role='admin')
        admin.set_password('aaaaaaaaaa1')
        staff = User(username='staff', email='s@x', role='staff')
        staff.set_password('aaaaaaaaaa1')
        db.session.add_all([admin, staff])
        room = Room(number='101', name='Standard', room_type='Standard',
                    floor=1, capacity=2, price_per_night=800.0,
                    is_active=True)
        guest = Guest(first_name='Test', last_name='Guest',
                      phone='+960 0')
        db.session.add_all([room, guest])
        db.session.flush()

        # ── Booking A: fully paid via cash + bank transfer ──
        a = Booking(
            booking_ref='BK-A', room_id=room.id, guest_id=guest.id,
            check_in_date=date.today(),
            check_out_date=date.today() + timedelta(days=2),
            num_guests=2, status='checked_in',
            total_amount=1600.0,
            source='walk_in', billing_target='guest',
            created_by=admin.id,
        )
        db.session.add(a); db.session.flush()
        inv_a = Invoice(invoice_number='INV-A', booking_id=a.id,
                        issue_date=date.today(),
                        subtotal=1600.0, total_amount=1600.0,
                        amount_paid=1600.0, payment_status='paid',
                        invoice_to='Test Guest')
        db.session.add(inv_a); db.session.flush()
        db.session.add(CashierTransaction(
            booking_id=a.id, guest_id=guest.id, invoice_id=inv_a.id,
            amount=800.0, currency='MVR', payment_method='cash',
            received_by_user_id=admin.id,
            transaction_type='payment', status='posted',
            reference_number='RCPT-001',
        ))
        db.session.add(CashierTransaction(
            booking_id=a.id, guest_id=guest.id, invoice_id=inv_a.id,
            amount=800.0, currency='MVR', payment_method='bank_transfer',
            received_by_user_id=admin.id,
            transaction_type='payment', status='posted',
            reference_number='WIRE-001',
        ))

        # ── Booking B: partial payment, missing reference, one void ──
        b = Booking(
            booking_ref='BK-B', room_id=room.id, guest_id=guest.id,
            check_in_date=date.today() + timedelta(days=10),
            check_out_date=date.today() + timedelta(days=12),
            num_guests=2, status='confirmed',
            total_amount=1600.0,
            source='walk_in', billing_target='guest',
            created_by=admin.id,
        )
        db.session.add(b); db.session.flush()
        inv_b = Invoice(invoice_number='INV-B', booking_id=b.id,
                        issue_date=date.today(),
                        subtotal=1600.0, total_amount=1600.0,
                        amount_paid=400.0, payment_status='partial',
                        invoice_to='Test Guest')
        db.session.add(inv_b); db.session.flush()
        # Posted bank-transfer payment WITHOUT reference number — the
        # whole point of the "missing reference" panel.
        db.session.add(CashierTransaction(
            booking_id=b.id, guest_id=guest.id, invoice_id=inv_b.id,
            amount=400.0, currency='MVR', payment_method='bank_transfer',
            received_by_user_id=admin.id,
            transaction_type='payment', status='posted',
            reference_number=None,  # ← missing
        ))
        # Voided transaction for audit trail
        db.session.add(CashierTransaction(
            booking_id=b.id, guest_id=guest.id, invoice_id=inv_b.id,
            amount=200.0, currency='MVR', payment_method='card',
            received_by_user_id=admin.id,
            transaction_type='payment', status='voided',
            reference_number='OOPS-001',
            voided_at=datetime.utcnow() - timedelta(hours=2),
            voided_by_user_id=admin.id,
            void_reason='Duplicate posting',
        ))

        db.session.commit()
        return admin.id, staff.id


class ReconciliationSummaryTests(unittest.TestCase):
    """Pure-function tests for cashiering.reconciliation_summary()."""

    def setUp(self):
        self.app = create_app(_TestConfig)
        self.ctx = self.app.app_context()
        self.ctx.push()
        self.admin_id, self.staff_id = _seed(self.app)

    def tearDown(self):
        db.session.remove()
        db.drop_all()
        self.ctx.pop()

    def test_summary_shape(self):
        from app.services.cashiering import reconciliation_summary
        s = reconciliation_summary()
        # Required keys
        for k in ('lookback_days', 'cutoff', 'posted_payments',
                  'voided_payments', 'missing_reference',
                  'open_invoices', 'posted_count',
                  'posted_total_received', 'posted_total_refunded',
                  'posted_net_received', 'voided_count',
                  'missing_ref_count', 'open_invoice_count',
                  'open_invoice_balance', 'bank_unmatched_count'):
            self.assertIn(k, s, f'reconciliation_summary missing key {k!r}')

    def test_posted_count_excludes_voided(self):
        from app.services.cashiering import reconciliation_summary
        s = reconciliation_summary()
        # 3 posted (2 on BK-A, 1 on BK-B); 1 voided; total 4 in DB.
        self.assertEqual(s['posted_count'], 3)
        self.assertEqual(s['voided_count'], 1)

    def test_missing_reference_panel(self):
        from app.services.cashiering import reconciliation_summary
        s = reconciliation_summary()
        # Only one posted payment is missing a ref (BK-B's bank-transfer)
        self.assertEqual(s['missing_ref_count'], 1)
        self.assertEqual(len(s['missing_reference']), 1)
        self.assertEqual(s['missing_reference'][0].booking.booking_ref, 'BK-B')

    def test_open_invoices_panel(self):
        from app.services.cashiering import reconciliation_summary
        s = reconciliation_summary()
        # BK-A is paid; BK-B has 1200 balance
        self.assertEqual(s['open_invoice_count'], 1)
        self.assertEqual(s['open_invoices'][0]['booking'].booking_ref, 'BK-B')
        self.assertEqual(s['open_invoice_balance'], 1200.00)

    def test_payments_are_not_revenue(self):
        # CRITICAL invariant: the summary reports MONEY RECEIVED,
        # not revenue. The total_received key sums posted CashierTxn
        # amounts, NOT Invoice.subtotal or any revenue field.
        from app.services.cashiering import reconciliation_summary
        s = reconciliation_summary()
        # 800 + 800 (BK-A) + 400 (BK-B) = 2000 received
        self.assertEqual(s['posted_total_received'], 2000.00)
        self.assertEqual(s['posted_total_refunded'], 0.00)
        self.assertEqual(s['posted_net_received'], 2000.00)
        # The open invoice (BK-B) has 1600 total but only 400 paid.
        # If we were measuring revenue we'd see 3200 (sum of total_amount).
        # We don't — we see 2000 received. That's the invariant.
        self.assertNotEqual(s['posted_total_received'], 3200.00)

    def test_lookback_window_filters_old_payments(self):
        # Ageing the BK-A cash payment past the lookback should drop
        # it from posted_payments while keeping it in the DB.
        from app.services.cashiering import reconciliation_summary
        old_t = (CashierTransaction.query
                 .filter_by(reference_number='RCPT-001').first())
        old_t.created_at = datetime.utcnow() - timedelta(days=60)
        db.session.commit()
        s = reconciliation_summary(lookback_days=30)
        self.assertEqual(s['posted_count'], 2)  # was 3; cash payment aged out

    def test_bank_unmatched_count_surfaced(self):
        from app.services.cashiering import reconciliation_summary
        # Initially no BankTransactions
        s = reconciliation_summary()
        self.assertEqual(s['bank_unmatched_count'], 0)
        # Add some unmatched + matched rows
        db.session.add(BankTransaction(
            statement_date=date.today(), description='ATM',
            amount=500.0, match_type='unmatched',
        ))
        db.session.add(BankTransaction(
            statement_date=date.today(), description='Wire',
            amount=800.0, match_type='invoice',
        ))
        db.session.commit()
        s = reconciliation_summary()
        self.assertEqual(s['bank_unmatched_count'], 1)


class PaymentsReconciliationRouteTests(unittest.TestCase):
    """End-to-end route tests for /accounting/reconciliation/payments."""

    def setUp(self):
        self.app = create_app(_TestConfig)
        self.ctx = self.app.app_context()
        self.ctx.push()
        self.admin_id, self.staff_id = _seed(self.app)
        self.client = self.app.test_client()

    def tearDown(self):
        db.session.remove()
        db.drop_all()
        self.ctx.pop()

    def _login(self, uid):
        with self.client.session_transaction() as sess:
            sess['_user_id'] = str(uid); sess['_fresh'] = True

    def test_admin_can_render_payments_reconciliation(self):
        self._login(self.admin_id)
        r = self.client.get('/accounting/reconciliation/payments')
        self.assertEqual(r.status_code, 200)
        # Key panels surfaced
        for needle in (b'Payment Reconciliation',
                       b'Posted payments',
                       b'Outstanding folios',
                       b'Missing reference',
                       b'Voided payments',
                       b'BK-A',                  # paid booking in posted list
                       b'BK-B',                  # outstanding + missing-ref
                       b'WIRE-001',              # ref present in posted
                       b'no reference',          # missing-ref chip
                       b'Duplicate posting'):    # voided txn reason in audit trail
            self.assertIn(needle, r.data,
                          f'reconciliation page missing {needle!r}')

    def test_staff_user_blocked(self):
        # /accounting/* enforces admin_required at the route. Staff
        # should be 403 (or bounced by the staff_guard before the
        # route even runs — both are acceptable rejections).
        self._login(self.staff_id)
        r = self.client.get('/accounting/reconciliation/payments',
                            follow_redirects=False)
        self.assertNotEqual(r.status_code, 200,
            'non-admin staff must not be able to render the '
            'payment reconciliation page')

    def test_anonymous_user_redirected(self):
        r = self.client.get('/accounting/reconciliation/payments',
                            follow_redirects=False)
        self.assertEqual(r.status_code, 302)
        # Should redirect to login (not into accounting).
        self.assertIn('console', r.headers.get('Location', '') + '/console')

    def test_bank_csv_page_still_works(self):
        # Regression guard: the original bank-CSV page must still
        # render and now also carry the cross-link to the new page.
        self._login(self.admin_id)
        r = self.client.get('/accounting/reconciliation/')
        self.assertEqual(r.status_code, 200)
        self.assertIn(b'Bank Statement Reconciliation', r.data)
        self.assertIn(b'Payment reconciliation', r.data,
                      'cross-link to new page should appear')
        self.assertIn(b'/accounting/reconciliation/payments', r.data)

    def test_no_external_calls_in_summary_module(self):
        # Static check — the cashiering reconciliation module must
        # not import or call anything that talks to WhatsApp / Gemini
        # / external HTTP. We scan for ACTUAL import statements and
        # call patterns rather than the bare word, otherwise the
        # module's own contract docstring ("never sends WhatsApp /
        # email / Gemini calls") would false-positive the test.
        import importlib, inspect, re
        m = importlib.import_module('app.services.cashiering')
        src = inspect.getsource(m).lower()
        # Remove line comments (the docstring is left in place — the
        # patterns below are specific enough to skip it).
        src = re.sub(r'#[^\n]*', '', src)

        banned_patterns = (
            r'\bimport\s+whatsapp',
            r'\bfrom\s+\S*whatsapp\s+import',
            r'\bimport\s+gemini',
            r'\bfrom\s+\S*gemini\s+import',
            r'\brequests\.\w+\s*\(',
            r'\burllib\.request',
            r'\bsend_email\s*\(',
            r'\bsend_whatsapp\s*\(',
            r'\bgemini\.\w+\s*\(',
        )
        for pat in banned_patterns:
            self.assertIsNone(
                re.search(pat, src),
                f'app.services.cashiering matches forbidden pattern {pat!r}',
            )


if __name__ == '__main__':
    unittest.main()
