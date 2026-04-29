"""Tests for Reports & Analytics V1.

Covers the 10 requirements from the build spec, section J:

  1. overview metrics compute correctly in simple demo scenarios
  2. payments are NOT counted as revenue (anti-double-count)
  3. discounts reduce totals correctly
  4. outstanding balance logic is correct
  5. date filtering works (today / yesterday / week / month / custom)
  6. occupancy calculation works
  7. category breakdown works
  8. reports pages require login + admin
  9. no WhatsApp / Gemini calls
 10. no production coupling

Plus extra service-level tests for canonical-source guarantees.
"""

from __future__ import annotations

import json
import os
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
    db, User, Room, Guest, Booking, Invoice, FolioItem,
    CashierTransaction, ActivityLog,
)
from app.services import reports as rep                         # noqa: E402
from app.services import whatsapp as wa                         # noqa: E402
from app.services import ai_drafts                              # noqa: E402


_REPO_ROOT = Path(__file__).resolve().parent.parent
_TODAY     = date.today()
_TOMORROW  = _TODAY + timedelta(days=1)
_PLUS_2    = _TODAY + timedelta(days=2)
_PLUS_3    = _TODAY + timedelta(days=3)
_YESTERDAY = _TODAY - timedelta(days=1)

# Mid-day naive datetime used to stamp folio_item / cashier_transaction
# created_at columns. The reports service filters created_at by naive
# datetime range derived from the local date; pinning rows to noon
# avoids any timezone-boundary flakiness when the test runs near
# midnight in the local zone vs. UTC.
def _at_noon(d):
    from datetime import time
    return datetime.combine(d, time(12, 0))


class _TestConfig(Config):
    TESTING = True
    SQLALCHEMY_DATABASE_URI = 'sqlite:///:memory:'
    WTF_CSRF_ENABLED = False
    WHATSAPP_ENABLED = False


def _make_app():
    return create_app(_TestConfig)


_seed_counter = {'n': 0}


def _seed_users():
    _seed_counter['n'] += 1
    n = _seed_counter['n']
    admin = User(username=f'rep_admin_{n}', email=f'a{n}@x', role='admin')
    admin.set_password('aaaaaaaaaa1')
    staff = User(username=f'rep_staff_{n}', email=f's{n}@x', role='staff')
    staff.set_password('aaaaaaaaaa1')
    db.session.add_all([admin, staff])
    db.session.commit()
    return admin.id, staff.id


def _seed_room(number='1', hk='clean', op_status='available'):
    r = Room(number=number, name='Test', room_type='Test',
             floor=0, capacity=2, price_per_night=600.0,
             status=op_status, housekeeping_status=hk)
    db.session.add(r); db.session.commit()
    return r


def _seed_booking(room, check_in, check_out, *,
                  status='confirmed', total=1200.0, guest=None):
    if guest is None:
        guest = Guest(first_name='G', last_name='X',
                      phone='+9607000000', email='g@x')
        db.session.add(guest); db.session.commit()
    b = Booking(
        booking_ref=f'BK-{room.id}-{check_in.isoformat()}-{status}',
        room_id=room.id, guest_id=guest.id,
        check_in_date=check_in, check_out_date=check_out,
        num_guests=1, total_amount=total, status=status,
    )
    db.session.add(b); db.session.commit()
    return b


def _add_folio_item(booking, *, item_type, total, status='open',
                    description='—', created_at=None):
    fi = FolioItem(
        booking_id=booking.id, guest_id=booking.guest_id,
        item_type=item_type, description=description,
        quantity=1.0, unit_price=abs(total), amount=total,
        total_amount=total, status=status, source_module='manual',
    )
    fi.created_at = created_at or _at_noon(_TODAY)
    db.session.add(fi); db.session.commit()
    return fi


def _add_payment(booking, amount, *, transaction_type='payment',
                 status='posted', method='cash', created_at=None):
    t = CashierTransaction(
        booking_id=booking.id, guest_id=booking.guest_id,
        amount=amount, currency='USD',
        payment_method=method, transaction_type=transaction_type,
        status=status,
    )
    t.created_at = created_at or _at_noon(_TODAY)
    db.session.add(t); db.session.commit()
    return t


# ─────────────────────────────────────────────────────────────────────
# 5) Date range resolution (Req 5)
# ─────────────────────────────────────────────────────────────────────

class DateRangeTests(unittest.TestCase):

    def test_today(self):
        rng = rep.resolve_range('today', today=_TODAY)
        self.assertEqual(rng.start, _TODAY)
        self.assertEqual(rng.end_inclusive, _TODAY)
        self.assertEqual(rng.days, 1)

    def test_yesterday(self):
        rng = rep.resolve_range('yesterday', today=_TODAY)
        self.assertEqual(rng.start, _YESTERDAY)
        self.assertEqual(rng.end_inclusive, _YESTERDAY)

    def test_week_starts_monday_ends_today(self):
        # Pick Wednesday for stability
        wed = date(2026, 6, 3)
        rng = rep.resolve_range('week', today=wed)
        self.assertEqual(rng.start, date(2026, 6, 1))   # Monday
        self.assertEqual(rng.end_inclusive, wed)

    def test_month_starts_first(self):
        d = date(2026, 6, 17)
        rng = rep.resolve_range('month', today=d)
        self.assertEqual(rng.start, date(2026, 6, 1))
        self.assertEqual(rng.end_inclusive, d)

    def test_custom(self):
        rng = rep.resolve_range('custom', '2026-05-01', '2026-05-31',
                                 today=_TODAY)
        self.assertEqual(rng.start, date(2026, 5, 1))
        self.assertEqual(rng.end_inclusive, date(2026, 5, 31))

    def test_custom_swaps_inverted(self):
        rng = rep.resolve_range('custom', '2026-05-31', '2026-05-01',
                                 today=_TODAY)
        self.assertEqual(rng.start, date(2026, 5, 1))
        self.assertEqual(rng.end_inclusive, date(2026, 5, 31))

    def test_invalid_falls_back_to_today(self):
        rng = rep.resolve_range('custom', 'bogus', 'also-bogus',
                                 today=_TODAY)
        self.assertEqual(rng.start, _TODAY)
        self.assertEqual(rng.end_inclusive, _TODAY)


# ─────────────────────────────────────────────────────────────────────
# Common base — patches WhatsApp + AI providers (Req 9)
# ─────────────────────────────────────────────────────────────────────

class _BaseAppTest(unittest.TestCase):

    def setUp(self):
        self._patches = []
        self._wa_send = mock.patch.object(
            wa, '_send', side_effect=AssertionError(
                'WhatsApp must not be called by Reports V1'))
        self._patches.append(self._wa_send.start())
        self._wa_template = mock.patch.object(
            wa, '_send_template', side_effect=AssertionError(
                'WhatsApp templates must not be called by Reports V1'))
        self._patches.append(self._wa_template.start())
        self._ai_patch = mock.patch.object(
            ai_drafts, 'generate_draft',
            side_effect=AssertionError(
                'AI drafts must not be called by Reports V1'))
        self._patches.append(self._ai_patch.start())

        self.app = _make_app()
        self.ctx = self.app.app_context()
        self.ctx.push()
        db.create_all()
        self.admin_id, self.staff_id = _seed_users()
        self.client = self.app.test_client()

    def tearDown(self):
        for p in (self._wa_send, self._wa_template, self._ai_patch):
            p.stop()
        db.session.remove()
        db.drop_all()
        self.ctx.pop()


# ─────────────────────────────────────────────────────────────────────
# 1) Operations summary (Req 1)
# ─────────────────────────────────────────────────────────────────────

class OperationsSummaryTests(_BaseAppTest):

    def test_arrivals_departures_in_house_counts(self):
        r1 = _seed_room('1')
        r2 = _seed_room('2')
        r3 = _seed_room('3', hk='dirty')
        # Arriving today (confirmed)
        _seed_booking(r1, _TODAY, _PLUS_2, status='confirmed')
        # Departing today (checked_in stay ends today)
        g2 = Guest(first_name='In', last_name='House',
                   phone='+9607000020', email='ih@x')
        db.session.add(g2); db.session.commit()
        _seed_booking(r2, _YESTERDAY, _TODAY,
                      status='checked_in', guest=g2)
        # In-house mid-stay
        g3 = Guest(first_name='Mid', last_name='Stay',
                   phone='+9607000021', email='ms@x')
        db.session.add(g3); db.session.commit()
        _seed_booking(r3, _YESTERDAY, _PLUS_2,
                      status='checked_in', guest=g3)

        ops = rep.operations_summary(_TODAY)
        self.assertEqual(ops['arrivals_today'], 1)
        self.assertEqual(ops['departures_today'], 1)
        self.assertEqual(ops['in_house'], 1)
        self.assertEqual(ops['rooms_total'], 3)
        # Hotel semantics: a stay [in_date, out_date) — the room held by
        # the booking departing today (out_date == today) is no longer
        # occupied. Only r3 (mid-stay covering today) counts.
        self.assertEqual(ops['occupied_rooms'], 1)
        self.assertEqual(ops['vacant_rooms'], 2)
        self.assertEqual(ops['dirty_rooms'], 1)

    def test_out_of_order_count(self):
        _seed_room('1', hk='out_of_order')
        _seed_room('2', hk='clean')
        ops = rep.operations_summary(_TODAY)
        self.assertEqual(ops['out_of_order_rooms'], 1)


# ─────────────────────────────────────────────────────────────────────
# 7) Revenue + category breakdown (Req 7)
# ─────────────────────────────────────────────────────────────────────

class RevenueTests(_BaseAppTest):

    def setUp(self):
        super().setUp()
        self.room = _seed_room('1')
        self.booking = _seed_booking(self.room, _TODAY, _PLUS_2,
                                      status='confirmed', total=1200.0)

    def test_room_revenue_uses_booking_total(self):
        rng = rep.resolve_range('today', today=_TODAY)
        self.assertEqual(rep.room_revenue(rng), 1200.0)

    def test_cancelled_booking_excluded_from_room_revenue(self):
        # Mark our existing booking cancelled — should drop to 0
        self.booking.status = 'cancelled'
        db.session.commit()
        rng = rep.resolve_range('today', today=_TODAY)
        self.assertEqual(rep.room_revenue(rng), 0.0)

    def test_ancillary_breakdown_excludes_room_charge(self):
        # Add a room_charge folio item — must NOT appear in ancillary
        _add_folio_item(self.booking, item_type='room_charge', total=600.0)
        # And add real ancillary lines
        _add_folio_item(self.booking, item_type='restaurant', total=80.0)
        _add_folio_item(self.booking, item_type='laundry',    total=20.0)
        rng = rep.resolve_range('today', today=_TODAY)
        breakdown = rep.ancillary_revenue_breakdown(rng)
        self.assertEqual(breakdown['restaurant'], 80.0)
        self.assertEqual(breakdown['laundry'],    20.0)
        self.assertEqual(breakdown['total'],     100.0)

    def test_voided_folio_items_excluded(self):
        _add_folio_item(self.booking, item_type='restaurant', total=50.0,
                        status='voided')
        _add_folio_item(self.booking, item_type='restaurant', total=30.0,
                        status='open')
        rng = rep.resolve_range('today', today=_TODAY)
        self.assertEqual(rep.ancillary_revenue_breakdown(rng)['total'], 30.0)


# ─────────────────────────────────────────────────────────────────────
# 2) Payments are NOT revenue (Req 2 — anti-double-counting)
# ─────────────────────────────────────────────────────────────────────

class PaymentsNotRevenueTests(_BaseAppTest):

    def test_payments_appear_separately(self):
        room = _seed_room('1')
        booking = _seed_booking(room, _TODAY, _PLUS_2, total=1200.0)
        # Cashier posts a payment — also writes a negative folio_item
        _add_folio_item(booking, item_type='payment', total=-1200.0)
        _add_payment(booking, 1200.0)

        rng = rep.resolve_range('today', today=_TODAY)
        rev = rep.revenue_summary(rng)

        # Payments DO appear in payments_received…
        self.assertEqual(rev['payments_received'], 1200.0)
        # … but they DO NOT appear in total_charges (room only here)
        self.assertEqual(rev['total_charges'], 1200.0)
        # … and they DO NOT inflate ancillary
        self.assertEqual(rev['ancillary']['total'], 0.0)
        # Net cashflow is what's actually received minus refunds
        self.assertEqual(rev['net_cashflow'], 1200.0)

    def test_voided_cashier_excluded(self):
        room = _seed_room('1')
        booking = _seed_booking(room, _TODAY, _PLUS_2, total=1200.0)
        _add_payment(booking, 500.0)            # posted
        _add_payment(booking, 999.0, status='voided')
        rng = rep.resolve_range('today', today=_TODAY)
        self.assertEqual(rep.payments_total(rng), 500.0)

    def test_refunds_tracked_separately(self):
        room = _seed_room('1')
        booking = _seed_booking(room, _TODAY, _PLUS_2, total=1200.0)
        _add_payment(booking, 1200.0)
        _add_payment(booking, 100.0, transaction_type='refund')
        rng = rep.resolve_range('today', today=_TODAY)
        rev = rep.revenue_summary(rng)
        self.assertEqual(rev['payments_received'], 1200.0)
        self.assertEqual(rev['refunds_paid'],       100.0)
        self.assertEqual(rev['net_cashflow'],      1100.0)


# ─────────────────────────────────────────────────────────────────────
# 3) Discounts reduce totals (Req 3)
# ─────────────────────────────────────────────────────────────────────

class DiscountsTests(_BaseAppTest):

    def test_discount_reduces_total_charges(self):
        room = _seed_room('1')
        booking = _seed_booking(room, _TODAY, _PLUS_2, total=1200.0)
        # Discount stored negative per services/folio.py
        _add_folio_item(booking, item_type='discount', total=-200.0)
        rng = rep.resolve_range('today', today=_TODAY)
        rev = rep.revenue_summary(rng)
        self.assertEqual(rev['discounts_total'], 200.0)
        # 1200 room - 200 discount = 1000 total charges
        self.assertEqual(rev['total_charges'], 1000.0)


# ─────────────────────────────────────────────────────────────────────
# 4) Outstanding balance logic (Req 4)
# ─────────────────────────────────────────────────────────────────────

class OutstandingTests(_BaseAppTest):

    def test_outstanding_only_counts_live_bookings(self):
        r1 = _seed_room('1')
        r2 = _seed_room('2')
        # Live: confirmed, charge 800, no payment → balance 800
        b1 = _seed_booking(r1, _TODAY, _PLUS_2,
                           status='confirmed', total=0.0)
        _add_folio_item(b1, item_type='room_charge', total=800.0)

        # Cancelled: should NOT appear (status not in live list)
        g2 = Guest(first_name='Cx', last_name='Cancel',
                   phone='+9607000033', email='cx@x')
        db.session.add(g2); db.session.commit()
        b2 = _seed_booking(r2, _TODAY, _PLUS_2,
                           status='cancelled', total=0.0, guest=g2)
        _add_folio_item(b2, item_type='room_charge', total=600.0)

        pend = rep.pending_payment_summary()
        self.assertEqual(pend['outstanding_count'], 1)
        self.assertEqual(pend['outstanding_total'], 800.0)

        rows = rep.outstanding_balances()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]['booking'].id, b1.id)
        self.assertEqual(rows[0]['balance'], 800.0)

    def test_paid_in_full_not_outstanding(self):
        room = _seed_room('1')
        b = _seed_booking(room, _TODAY, _PLUS_2,
                          status='checked_in', total=0.0)
        _add_folio_item(b, item_type='room_charge', total=600.0)
        _add_folio_item(b, item_type='payment',     total=-600.0)
        pend = rep.pending_payment_summary()
        self.assertEqual(pend['outstanding_count'], 0)
        self.assertEqual(pend['outstanding_total'], 0.0)


# ─────────────────────────────────────────────────────────────────────
# 6) Occupancy (Req 6)
# ─────────────────────────────────────────────────────────────────────

class OccupancyTests(_BaseAppTest):

    def test_one_of_two_rooms_occupied(self):
        r1 = _seed_room('1')
        r2 = _seed_room('2')
        _seed_booking(r1, _YESTERDAY, _PLUS_2, status='checked_in')
        snap = rep.occupancy_for_day(_TODAY)
        self.assertEqual(snap['rooms_total'], 2)
        self.assertEqual(snap['rooms_occupied'], 1)
        self.assertEqual(snap['occupancy_pct'], 50.0)

    def test_confirmed_not_arrived_does_not_count(self):
        # A confirmed-but-not-checked-in booking on today's date
        # should NOT count as a room night sold.
        r = _seed_room('1')
        _seed_booking(r, _TODAY, _PLUS_2, status='confirmed')
        snap = rep.occupancy_for_day(_TODAY)
        self.assertEqual(snap['rooms_occupied'], 0)
        self.assertEqual(snap['occupancy_pct'], 0.0)

    def test_summary_aggregates(self):
        r1 = _seed_room('1')
        r2 = _seed_room('2')
        # Stay covering today + tomorrow + +2 (3 nights)
        _seed_booking(r1, _YESTERDAY, _PLUS_3, status='checked_in')
        rng = rep.resolve_range('custom',
                                  _TODAY.isoformat(), _PLUS_2.isoformat(),
                                  today=_TODAY)
        s = rep.occupancy_summary(rng)
        # 3 days × 2 rooms = 6 available; 3 occupied (one room each day)
        self.assertEqual(s['available_room_nights'], 6)
        self.assertEqual(s['room_nights_sold'],      3)
        self.assertEqual(s['occupancy_pct'],         50.0)


# ─────────────────────────────────────────────────────────────────────
# 8) Auth gate on report routes (Req 8)
# ─────────────────────────────────────────────────────────────────────

class ReportRouteAuthTests(_BaseAppTest):

    def _login(self, uid):
        with self.client.session_transaction() as sess:
            sess['_user_id'] = str(uid)
            sess['_fresh'] = True

    def test_anonymous_redirected(self):
        for path in ('/reports/', '/reports/revenue',
                      '/reports/occupancy', '/reports/outstanding'):
            r = self.client.get(path)
            self.assertIn(r.status_code, (301, 302, 401),
                          msg=f'{path} unexpectedly returned {r.status_code}')

    def test_staff_blocked(self):
        self._login(self.staff_id)
        for path in ('/reports/', '/reports/revenue'):
            r = self.client.get(path)
            self.assertIn(r.status_code, (302, 401, 403))

    def test_admin_allowed(self):
        self._login(self.admin_id)
        for path in ('/reports/', '/reports/revenue',
                      '/reports/occupancy', '/reports/outstanding'):
            r = self.client.get(path)
            self.assertEqual(r.status_code, 200,
                              msg=f'admin should reach {path}')

    def test_admin_overview_renders_kpis(self):
        self._login(self.admin_id)
        r = self.client.get('/reports/')
        self.assertEqual(r.status_code, 200)
        self.assertIn(b'Reports', r.data)
        self.assertIn(b'Operations', r.data)


# ─────────────────────────────────────────────────────────────────────
# 9 + 10) No external coupling (Reqs 9, 10)
# ─────────────────────────────────────────────────────────────────────

class NoExternalCouplingTests(_BaseAppTest):

    def _login(self, uid):
        with self.client.session_transaction() as sess:
            sess['_user_id'] = str(uid)
            sess['_fresh'] = True

    def test_full_tab_walk_no_external_calls(self):
        self._login(self.admin_id)
        for path in ('/reports/', '/reports/revenue',
                      '/reports/occupancy', '/reports/outstanding'):
            self.client.get(path)
        self.assertEqual(wa._send.call_count,           0)
        self.assertEqual(wa._send_template.call_count,  0)
        self.assertEqual(ai_drafts.generate_draft.call_count, 0)

    def test_no_activity_log_rows_for_page_views(self):
        # Per spec: V1 has no exports / management actions, so no
        # ActivityLog entries are written for report pages.
        self._login(self.admin_id)
        before = ActivityLog.query.count()
        self.client.get('/reports/')
        self.client.get('/reports/revenue')
        self.client.get('/reports/occupancy')
        self.client.get('/reports/outstanding')
        after = ActivityLog.query.count()
        self.assertEqual(after, before,
                          'reports must not write audit rows for views.')


if __name__ == '__main__':
    unittest.main()
