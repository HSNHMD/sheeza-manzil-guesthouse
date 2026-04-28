"""Tests for Guest Folio V1.

Hard rules covered:
  - FolioItem model: insertion, defaults, item_type whitelist, void state
  - Service helpers: validate_folio_item, signed_total, balance math
  - Routes: auth gating, validation, audit logging, no status mutation,
    no DELETE endpoint, void-already-voided rejection
  - Audit metadata is a strict whitelist (no body/passport/secret keys)
  - Booking detail page renders the Folio panel
  - No WhatsApp / Gemini side-effects from folio routes
  - Migration file exists, parents off c2b9f4d83a51, only creates folio_items
"""

from __future__ import annotations

import json
import os
import re
import unittest
from datetime import date, datetime, timedelta
from pathlib import Path
from unittest import mock

# Clean env BEFORE app import — same pattern as the other suites.
for _v in ('DATABASE_URL',
           'AI_DRAFT_PROVIDER', 'AI_DRAFT_MODEL',
           'GEMINI_API_KEY', 'ANTHROPIC_API_KEY', 'ANTHROPIC_MODEL',
           'WHATSAPP_ENABLED', 'WHATSAPP_TOKEN',
           'WHATSAPP_PHONE_NUMBER_ID', 'WHATSAPP_PHONE_ID',
           'WHATSAPP_WEBHOOK_VERIFY_TOKEN', 'WHATSAPP_APP_SECRET'):
    os.environ.pop(_v, None)
os.environ.setdefault('SECRET_KEY', 'test-secret-do-not-use-in-prod')

from config import Config                                      # noqa: E402
from app import create_app                                     # noqa: E402
from app.models import (                                       # noqa: E402
    db, User, Room, Guest, Booking, Invoice, ActivityLog,
    FolioItem,
)
from app.services import folio as folio_svc                    # noqa: E402
from app.services import ai_drafts                             # noqa: E402
from app.services import whatsapp as wa                        # noqa: E402


_REPO_ROOT = Path(__file__).resolve().parent.parent


class _TestConfig(Config):
    TESTING = True
    SQLALCHEMY_DATABASE_URI = 'sqlite:///:memory:'
    SQLALCHEMY_ENGINE_OPTIONS = {'pool_pre_ping': True, 'pool_recycle': 300}
    WTF_CSRF_ENABLED = False
    WHATSAPP_ENABLED = False


def _make_app():
    return create_app(_TestConfig)


def _seed_users():
    admin = User(username='admin1', email='a@x', role='admin')
    admin.set_password('a-very-strong-password-1!')
    staff = User(username='staff1', email='s@x', role='staff')
    staff.set_password('a-very-strong-password-1!')
    db.session.add_all([admin, staff])
    db.session.commit()
    return admin.id, staff.id


_seed_counter = {'i': 0}


def _seed_booking_with_invoice(payment_status='unpaid', booking_status='confirmed'):
    _seed_counter['i'] += 1
    suffix = f'{_seed_counter["i"]:03d}'
    g = Guest(first_name=f'Guest{suffix}', last_name='Test',
              phone=f'+96070099{suffix}', email=f'g{suffix}@x')
    db.session.add(g)
    room = Room(number=f'9{suffix}', name='Test', room_type='Test',
                floor=0, capacity=2, price_per_night=600.0)
    db.session.add(room)
    db.session.commit()
    b = Booking(
        booking_ref=f'BK{suffix}',
        room_id=room.id, guest_id=g.id,
        check_in_date=date.today() + timedelta(days=3),
        check_out_date=date.today() + timedelta(days=5),
        num_guests=1, total_amount=1200.0,
        status=booking_status,
    )
    db.session.add(b)
    db.session.commit()
    inv = Invoice(
        booking_id=b.id,
        invoice_number=f'INV-{suffix}',
        total_amount=1200.0,
        payment_status=payment_status,
        amount_paid=0.0,
    )
    db.session.add(inv)
    db.session.commit()
    return b


# ─────────────────────────────────────────────────────────────────────
# 1) Model + service-layer pure tests
# ─────────────────────────────────────────────────────────────────────

class FolioItemModelTests(unittest.TestCase):

    def setUp(self):
        self.app = _make_app()
        self.ctx = self.app.app_context()
        self.ctx.push()
        db.create_all()
        self.booking = _seed_booking_with_invoice()

    def tearDown(self):
        db.session.remove()
        db.drop_all()
        self.ctx.pop()

    def test_can_insert_folio_item(self):
        it = FolioItem(
            booking_id=self.booking.id,
            guest_id=self.booking.guest_id,
            item_type='laundry',
            description='3 shirts',
            quantity=1.0, unit_price=50.0,
            amount=50.0, total_amount=50.0,
            status='open', source_module='manual',
        )
        db.session.add(it)
        db.session.commit()
        self.assertIsNotNone(it.id)
        self.assertEqual(it.status, 'open')
        self.assertFalse(it.is_voided)
        self.assertTrue(it.is_open)

    def test_booking_backref_finds_folio_items(self):
        for i in range(3):
            db.session.add(FolioItem(
                booking_id=self.booking.id, guest_id=self.booking.guest_id,
                item_type='laundry', description=f'item {i}',
                quantity=1.0, unit_price=10.0, amount=10.0, total_amount=10.0,
                status='open', source_module='manual',
            ))
        db.session.commit()
        items = self.booking.folio_items.all()
        self.assertEqual(len(items), 3)


class ItemTypeAndStatusEnumTests(unittest.TestCase):

    def test_item_types_includes_all_required(self):
        required = {
            'room_charge', 'restaurant', 'laundry', 'transfer', 'excursion',
            'goods', 'service', 'fee', 'discount', 'payment', 'adjustment',
            'damage', 'other',
        }
        self.assertTrue(required.issubset(set(folio_svc.ITEM_TYPES)))

    def test_negative_types_subset(self):
        self.assertEqual(folio_svc.NEGATIVE_ITEM_TYPES,
                         frozenset(('discount', 'payment')))

    def test_statuses(self):
        self.assertEqual(set(folio_svc.STATUSES),
                         {'open', 'invoiced', 'paid', 'voided'})

    def test_source_modules(self):
        self.assertEqual(set(folio_svc.SOURCE_MODULES),
                         {'manual', 'booking', 'accounting', 'pos', 'system'})

    def test_normalize_item_type(self):
        self.assertEqual(folio_svc.normalize_folio_item_type('Laundry'), 'laundry')
        self.assertEqual(folio_svc.normalize_folio_item_type(' PAYMENT '), 'payment')
        self.assertIsNone(folio_svc.normalize_folio_item_type('drugs'))
        self.assertIsNone(folio_svc.normalize_folio_item_type(''))
        self.assertIsNone(folio_svc.normalize_folio_item_type(None))

    def test_display_label(self):
        self.assertEqual(folio_svc.display_folio_item_label('laundry'), 'Laundry')
        self.assertEqual(folio_svc.display_folio_item_label('LAUNDRY'), 'Laundry')
        self.assertEqual(folio_svc.display_folio_item_label('unknown_type'),
                         'unknown_type')


class ValidateFolioItemTests(unittest.TestCase):

    def test_valid_charge_passes(self):
        r = folio_svc.validate_folio_item(
            item_type='laundry', description='3 shirts',
            quantity='1', unit_price='50',
        )
        self.assertEqual(r['errors'], [])
        self.assertEqual(r['cleaned']['item_type'], 'laundry')
        self.assertEqual(r['cleaned']['quantity'], 1.0)

    def test_missing_description_rejected(self):
        r = folio_svc.validate_folio_item(
            item_type='laundry', description=' ',
            quantity='1', unit_price='50',
        )
        self.assertTrue(any('description' in e for e in r['errors']))

    def test_invalid_item_type_rejected(self):
        r = folio_svc.validate_folio_item(
            item_type='drugs', description='x',
            quantity='1', unit_price='10',
        )
        self.assertTrue(any('item_type' in e for e in r['errors']))

    def test_negative_unit_price_rejected_for_normal_type(self):
        r = folio_svc.validate_folio_item(
            item_type='laundry', description='x',
            quantity='1', unit_price='-50',
        )
        self.assertTrue(any('unit_price' in e for e in r['errors']))

    def test_negative_unit_price_allowed_for_adjustment(self):
        r = folio_svc.validate_folio_item(
            item_type='adjustment', description='credit',
            quantity='1', unit_price='-50',
        )
        self.assertEqual(r['errors'], [])

    def test_zero_quantity_rejected(self):
        r = folio_svc.validate_folio_item(
            item_type='laundry', description='x',
            quantity='0', unit_price='10',
        )
        self.assertTrue(any('quantity' in e for e in r['errors']))

    def test_garbage_unit_price_rejected(self):
        r = folio_svc.validate_folio_item(
            item_type='laundry', description='x',
            quantity='1', unit_price='abc',
        )
        self.assertTrue(any('unit_price' in e for e in r['errors']))

    def test_long_description_rejected(self):
        r = folio_svc.validate_folio_item(
            item_type='laundry', description='x' * 256,
            quantity='1', unit_price='10',
        )
        self.assertTrue(any('description' in e for e in r['errors']))


class SignedTotalTests(unittest.TestCase):

    def test_charge_is_positive(self):
        self.assertEqual(folio_svc.signed_total('laundry', 50, 0, 0), 50)
        self.assertEqual(folio_svc.signed_total('laundry', 50, 5, 0), 55)
        self.assertEqual(folio_svc.signed_total('restaurant', 100, 5, 10), 115)

    def test_discount_is_negative(self):
        self.assertEqual(folio_svc.signed_total('discount', 50, 0, 0), -50)

    def test_payment_is_negative(self):
        self.assertEqual(folio_svc.signed_total('payment', 1200, 0, 0), -1200)

    def test_adjustment_signed_passthrough(self):
        # Positive amount stays positive
        self.assertEqual(folio_svc.signed_total('adjustment', 25, 0, 0), 25)
        # Negative amount stays negative; tax+sc ride along negatively
        self.assertEqual(folio_svc.signed_total('adjustment', -100, 0, 0), -100)


class FolioBalanceTests(unittest.TestCase):

    def setUp(self):
        self.app = _make_app()
        self.ctx = self.app.app_context()
        self.ctx.push()
        db.create_all()
        self.booking = _seed_booking_with_invoice()

    def tearDown(self):
        db.session.remove()
        db.drop_all()
        self.ctx.pop()

    def _post(self, item_type, total_amount):
        db.session.add(FolioItem(
            booking_id=self.booking.id, guest_id=self.booking.guest_id,
            item_type=item_type, description=f'auto {item_type}',
            quantity=1.0, unit_price=abs(total_amount),
            amount=abs(total_amount), total_amount=total_amount,
            status='open', source_module='manual',
        ))
        db.session.commit()

    def test_charge_increases_balance(self):
        self._post('laundry', 50)
        self.assertEqual(folio_svc.folio_balance(self.booking), 50.0)

    def test_discount_reduces_balance(self):
        self._post('laundry', 100)
        self._post('discount', -25)
        self.assertEqual(folio_svc.folio_balance(self.booking), 75.0)

    def test_payment_reduces_balance(self):
        self._post('laundry', 200)
        self._post('payment', -200)
        self.assertEqual(folio_svc.folio_balance(self.booking), 0.0)

    def test_voided_item_excluded_from_balance(self):
        self._post('laundry', 100)
        self._post('laundry', 50)
        # Void the second
        items = list(self.booking.folio_items)
        items[1].status = 'voided'
        db.session.commit()
        self.assertEqual(folio_svc.folio_balance(self.booking), 100.0)

    def test_calculate_totals_buckets(self):
        self._post('laundry',    100)   # charge
        self._post('restaurant', 200)   # charge
        self._post('discount',   -30)   # credit
        self._post('payment',   -150)   # credit
        self._post('adjustment', -10)   # adjustment (negative)
        totals = folio_svc.calculate_folio_totals(self.booking)
        self.assertEqual(totals['total_charges'], 300.0)
        self.assertEqual(totals['total_credits'], 180.0)
        self.assertEqual(totals['total_adjustments'], -10.0)
        self.assertEqual(totals['balance'], 110.0)
        self.assertEqual(totals['item_count_open'], 5)
        self.assertEqual(totals['item_count_voided'], 0)


# ─────────────────────────────────────────────────────────────────────
# 2) Route tests (auth, validation, audit, no status mutation)
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


class AddItemRouteAuthTests(_RouteBase):

    def test_anonymous_blocked(self):
        r = self.client.post(
            f'/bookings/{self.booking.id}/folio/items',
            data={'item_type': 'laundry', 'description': 'x',
                  'quantity': '1', 'unit_price': '10'},
        )
        self.assertIn(r.status_code, (301, 302, 401))
        self.assertEqual(FolioItem.query.count(), 0)

    def test_staff_blocked(self):
        self._login(self.staff_id)
        r = self.client.post(
            f'/bookings/{self.booking.id}/folio/items',
            data={'item_type': 'laundry', 'description': 'x',
                  'quantity': '1', 'unit_price': '10'},
        )
        self.assertIn(r.status_code, (302, 401, 403))
        self.assertEqual(FolioItem.query.count(), 0)

    def test_get_method_not_allowed(self):
        self._login(self.admin_id)
        r = self.client.get(f'/bookings/{self.booking.id}/folio/items')
        self.assertEqual(r.status_code, 405)


class AddItemValidationTests(_RouteBase):

    def setUp(self):
        super().setUp()
        self._login(self.admin_id)

    def test_missing_description_rejected(self):
        r = self.client.post(
            f'/bookings/{self.booking.id}/folio/items',
            data={'item_type': 'laundry', 'description': '',
                  'quantity': '1', 'unit_price': '10'},
        )
        self.assertIn(r.status_code, (301, 302))
        self.assertEqual(FolioItem.query.count(), 0)

    def test_invalid_item_type_rejected(self):
        r = self.client.post(
            f'/bookings/{self.booking.id}/folio/items',
            data={'item_type': 'drugs', 'description': 'x',
                  'quantity': '1', 'unit_price': '10'},
        )
        self.assertIn(r.status_code, (301, 302))
        self.assertEqual(FolioItem.query.count(), 0)

    def test_unknown_booking_returns_404(self):
        r = self.client.post(
            '/bookings/999999/folio/items',
            data={'item_type': 'laundry', 'description': 'x',
                  'quantity': '1', 'unit_price': '10'},
        )
        self.assertEqual(r.status_code, 404)


class AddItemSuccessTests(_RouteBase):

    def setUp(self):
        super().setUp()
        self._login(self.admin_id)

    def test_charge_creates_item_with_positive_total(self):
        r = self.client.post(
            f'/bookings/{self.booking.id}/folio/items',
            data={'item_type': 'laundry', 'description': '3 shirts',
                  'quantity': '3', 'unit_price': '15'},
            follow_redirects=False,
        )
        self.assertIn(r.status_code, (301, 302))
        items = FolioItem.query.all()
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0].item_type, 'laundry')
        self.assertEqual(items[0].amount, 45.0)
        self.assertEqual(items[0].total_amount, 45.0)
        self.assertEqual(items[0].status, 'open')
        self.assertEqual(items[0].source_module, 'manual')
        self.assertEqual(items[0].posted_by_user_id, self.admin_id)

    def test_discount_stored_as_negative_total(self):
        self.client.post(
            f'/bookings/{self.booking.id}/folio/items',
            data={'item_type': 'discount', 'description': 'loyalty',
                  'quantity': '1', 'unit_price': '50'},
        )
        item = FolioItem.query.first()
        self.assertEqual(item.total_amount, -50.0)

    def test_payment_stored_as_negative_total(self):
        self.client.post(
            f'/bookings/{self.booking.id}/folio/items',
            data={'item_type': 'payment', 'description': 'cash on checkin',
                  'quantity': '1', 'unit_price': '500'},
        )
        item = FolioItem.query.first()
        self.assertEqual(item.total_amount, -500.0)

    def test_add_does_not_change_booking_status(self):
        before = self.booking.status
        self.client.post(
            f'/bookings/{self.booking.id}/folio/items',
            data={'item_type': 'laundry', 'description': 'x',
                  'quantity': '1', 'unit_price': '10'},
        )
        b = Booking.query.get(self.booking.id)
        self.assertEqual(b.status, before)

    def test_add_does_not_change_invoice_payment_status(self):
        before = self.booking.invoice.payment_status
        before_paid = self.booking.invoice.amount_paid
        self.client.post(
            f'/bookings/{self.booking.id}/folio/items',
            data={'item_type': 'payment', 'description': 'cash',
                  'quantity': '1', 'unit_price': '500'},
        )
        inv = Invoice.query.get(self.booking.invoice.id)
        self.assertEqual(inv.payment_status, before)
        self.assertEqual(inv.amount_paid, before_paid)

    def test_add_does_not_send_whatsapp_or_call_ai(self):
        with mock.patch.object(wa, 'send_text_message') as m_send, \
             mock.patch.object(ai_drafts, '_call_provider') as m_ai:
            self.client.post(
                f'/bookings/{self.booking.id}/folio/items',
                data={'item_type': 'laundry', 'description': 'x',
                      'quantity': '1', 'unit_price': '10'},
            )
        m_send.assert_not_called()
        m_ai.assert_not_called()


class AddItemAuditTests(_RouteBase):

    def setUp(self):
        super().setUp()
        self._login(self.admin_id)

    def test_add_writes_folio_item_created_audit(self):
        self.client.post(
            f'/bookings/{self.booking.id}/folio/items',
            data={'item_type': 'laundry', 'description': '3 shirts',
                  'quantity': '3', 'unit_price': '15'},
        )
        rows = (ActivityLog.query
                .filter(ActivityLog.action == 'folio.item.created').all())
        self.assertEqual(len(rows), 1)
        meta = json.loads(rows[0].metadata_json or '{}')
        # Allowed
        for key in ('booking_id', 'booking_ref', 'folio_item_id',
                    'item_type', 'source_module', 'amount', 'status',
                    'voided'):
            self.assertIn(key, meta, f'missing key {key}')
        self.assertEqual(meta['item_type'], 'laundry')
        self.assertEqual(meta['amount'], 45.0)
        self.assertFalse(meta['voided'])

    def test_audit_metadata_excludes_secrets(self):
        # Try to smuggle a forbidden key via the form — should not appear
        # in the audit metadata at all.
        self.client.post(
            f'/bookings/{self.booking.id}/folio/items',
            data={'item_type': 'goods', 'description': 'minibar Coke',
                  'quantity': '1', 'unit_price': '20',
                  'passport_number': 'ZZ12345',
                  'api_token':       'sk-test-xyz'},
        )
        row = (ActivityLog.query
               .filter(ActivityLog.action == 'folio.item.created').first())
        meta_blob = json.dumps(json.loads(row.metadata_json or '{}'))
        self.assertNotIn('passport', meta_blob.lower())
        self.assertNotIn('token', meta_blob.lower())
        self.assertNotIn('ZZ12345', meta_blob)
        self.assertNotIn('sk-test-xyz', meta_blob)


# ─────────────────────────────────────────────────────────────────────
# 3) Void route tests
# ─────────────────────────────────────────────────────────────────────

class VoidRouteAuthTests(_RouteBase):

    def setUp(self):
        super().setUp()
        # Pre-seed an open item directly via DB (no test-client login round-trip,
        # so subsequent anonymous/staff requests don't inherit any cookie state).
        item = FolioItem(
            booking_id=self.booking.id, guest_id=self.booking.guest_id,
            item_type='laundry', description='x',
            quantity=1.0, unit_price=10.0, amount=10.0, total_amount=10.0,
            status='open', source_module='manual',
        )
        db.session.add(item)
        db.session.commit()
        self.item_id = item.id

    def test_anonymous_blocked(self):
        r = self.client.post(
            f'/bookings/{self.booking.id}/folio/items/{self.item_id}/void')
        self.assertIn(r.status_code, (301, 302, 401))
        self.assertEqual(FolioItem.query.first().status, 'open')

    def test_staff_blocked(self):
        self._login(self.staff_id)
        r = self.client.post(
            f'/bookings/{self.booking.id}/folio/items/{self.item_id}/void')
        self.assertIn(r.status_code, (302, 401, 403))
        self.assertEqual(FolioItem.query.first().status, 'open')

    def test_get_method_not_allowed(self):
        self._login(self.admin_id)
        r = self.client.get(
            f'/bookings/{self.booking.id}/folio/items/{self.item_id}/void')
        self.assertEqual(r.status_code, 405)


class VoidRouteBehaviourTests(_RouteBase):

    def setUp(self):
        super().setUp()
        self._login(self.admin_id)
        self.client.post(
            f'/bookings/{self.booking.id}/folio/items',
            data={'item_type': 'laundry', 'description': 'x',
                  'quantity': '1', 'unit_price': '10'},
        )
        self.item_id = FolioItem.query.first().id

    def test_void_marks_status_voided(self):
        r = self.client.post(
            f'/bookings/{self.booking.id}/folio/items/{self.item_id}/void',
            data={'void_reason': 'duplicate'},
            follow_redirects=False,
        )
        self.assertIn(r.status_code, (301, 302))
        item = FolioItem.query.get(self.item_id)
        self.assertEqual(item.status, 'voided')
        self.assertEqual(item.void_reason, 'duplicate')
        self.assertEqual(item.voided_by_user_id, self.admin_id)
        self.assertIsNotNone(item.voided_at)

    def test_void_already_voided_blocked(self):
        # First void
        self.client.post(
            f'/bookings/{self.booking.id}/folio/items/{self.item_id}/void')
        # Second void attempt
        r = self.client.post(
            f'/bookings/{self.booking.id}/folio/items/{self.item_id}/void')
        self.assertIn(r.status_code, (301, 302))
        # Still only one voided audit row, not two
        rows = (ActivityLog.query
                .filter(ActivityLog.action == 'folio.item.voided').all())
        self.assertEqual(len(rows), 1)

    def test_void_writes_audit_row(self):
        self.client.post(
            f'/bookings/{self.booking.id}/folio/items/{self.item_id}/void',
            data={'void_reason': 'dup'},
        )
        row = (ActivityLog.query
               .filter(ActivityLog.action == 'folio.item.voided').first())
        self.assertIsNotNone(row)
        meta = json.loads(row.metadata_json or '{}')
        self.assertTrue(meta.get('voided'))
        self.assertEqual(meta.get('status'), 'voided')

    def test_wrong_booking_path_returns_404(self):
        # Create a second booking; try to void item via mismatched URL.
        # _seed_booking_with_invoice() auto-uniques room/guest/booking_ref.
        b2 = _seed_booking_with_invoice()
        r = self.client.post(
            f'/bookings/{b2.id}/folio/items/{self.item_id}/void')
        self.assertEqual(r.status_code, 404)
        item = FolioItem.query.get(self.item_id)
        self.assertEqual(item.status, 'open')


# ─────────────────────────────────────────────────────────────────────
# 4) Booking detail page renders Folio panel
# ─────────────────────────────────────────────────────────────────────

class BookingDetailFolioPanelTests(_RouteBase):

    def setUp(self):
        super().setUp()
        self._login(self.admin_id)

    def test_detail_page_includes_folio_section(self):
        r = self.client.get(f'/bookings/{self.booking.id}')
        self.assertEqual(r.status_code, 200)
        self.assertIn(b'Guest Folio', r.data)
        self.assertIn(b'Add folio item', r.data)
        # V1 disclaimer text
        self.assertIn(b'Guest Folio V1 is for manual charges', r.data)

    def test_detail_page_shows_folio_items_when_present(self):
        self.client.post(
            f'/bookings/{self.booking.id}/folio/items',
            data={'item_type': 'laundry', 'description': '3 shirts',
                  'quantity': '3', 'unit_price': '15'},
        )
        r = self.client.get(f'/bookings/{self.booking.id}')
        self.assertIn(b'3 shirts', r.data)
        self.assertIn(b'Laundry', r.data)


# ─────────────────────────────────────────────────────────────────────
# 5) Static / safety checks
# ─────────────────────────────────────────────────────────────────────

class NoDeleteEndpointTests(unittest.TestCase):
    """Confirm there is no DELETE-method route on folio items in the
    blueprint. Folio items are voided, never hard-deleted."""

    def test_no_delete_route_exists(self):
        app = _make_app()
        with app.app_context():
            for rule in app.url_map.iter_rules():
                if 'folio' in rule.rule:
                    self.assertNotIn(
                        'DELETE', rule.methods,
                        f'unexpected DELETE on folio route: {rule}',
                    )


class MigrationFileTests(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        cls.path = (_REPO_ROOT / 'migrations' / 'versions'
                    / 'd8a3e1f29c40_add_folio_items_table.py')

    def test_file_exists(self):
        self.assertTrue(self.path.exists(), self.path)

    def test_chain(self):
        text = self.path.read_text()
        self.assertIn("revision = 'd8a3e1f29c40'", text)
        self.assertIn("down_revision = 'c2b9f4d83a51'", text)

    def test_creates_only_folio_items(self):
        text = self.path.read_text()
        # Must create folio_items
        self.assertIn("create_table(\n        'folio_items'", text)
        # Must NOT create any other table
        other_tables = re.findall(
            r"create_table\(\s*'([^']+)'", text)
        self.assertEqual(other_tables, ['folio_items'])
        # Must NOT alter any existing table
        self.assertNotIn('alter_table', text)
        self.assertNotIn('add_column', text)
        self.assertNotIn('drop_column', text)

    def test_downgrade_drops_only_folio_items(self):
        text = self.path.read_text()
        dropped = re.findall(r"drop_table\('([^']+)'", text)
        self.assertEqual(dropped, ['folio_items'])


if __name__ == '__main__':
    unittest.main()
