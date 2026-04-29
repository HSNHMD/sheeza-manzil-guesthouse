"""Tests for POS / F&B V1.

Covers the 11 requirements from the build spec, section I:

  1. category/item creation through admin UI
  2. POS routes require login + correct role (admin for catalog,
     login for terminal)
  3. cart total calculation
  4. post-to-room creates correct folio item(s)
  5. direct payment creates cashier transaction
  6. invalid room/booking selection rejected
  7. cannot post to checked-out booking
  8. ActivityLog created
  9. no WhatsApp / Gemini calls
 10. migration file exists
 11. migration only creates POS-related tables
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
    db, User, Room, Guest, Booking, FolioItem, CashierTransaction,
    ActivityLog, PosCategory, PosItem,
)
from app.services import pos as pos_svc                         # noqa: E402
from app.services import folio as folio_svc                     # noqa: E402
from app.services import whatsapp as wa                         # noqa: E402
from app.services import ai_drafts                              # noqa: E402


_REPO_ROOT = Path(__file__).resolve().parent.parent
_MIGRATION_PATH = (
    _REPO_ROOT / 'migrations' / 'versions'
    / 'd6a7b9c0e215_add_pos_tables.py'
)
_TODAY     = date.today()
_YESTERDAY = _TODAY - timedelta(days=1)
_PLUS_2    = _TODAY + timedelta(days=2)


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
    admin = User(username=f'pos_admin_{n}', email=f'a{n}@x', role='admin')
    admin.set_password('aaaaaaaaaa1')
    staff = User(username=f'pos_staff_{n}', email=f's{n}@x', role='staff')
    staff.set_password('aaaaaaaaaa1')
    db.session.add_all([admin, staff])
    db.session.commit()
    return admin.id, staff.id


def _seed_room(number='1'):
    r = Room(number=number, name='T', room_type='Test',
             floor=0, capacity=2, price_per_night=600.0,
             status='available', housekeeping_status='clean')
    db.session.add(r); db.session.commit()
    return r


def _seed_booking(room, status='checked_in'):
    g = Guest(first_name='G', last_name='X',
              phone='+9607000000', email='g@x')
    db.session.add(g); db.session.commit()
    b = Booking(
        booking_ref=f'BK-{room.id}-{status}',
        room_id=room.id, guest_id=g.id,
        check_in_date=_YESTERDAY, check_out_date=_PLUS_2,
        num_guests=1, total_amount=1200.0, status=status,
    )
    db.session.add(b); db.session.commit()
    return b


def _seed_catalog():
    cat_d = PosCategory(name='Drinks', sort_order=1, is_active=True)
    cat_f = PosCategory(name='Food',   sort_order=2, is_active=True)
    db.session.add_all([cat_d, cat_f]); db.session.commit()
    coffee = PosItem(category_id=cat_d.id, name='Espresso',
                     price=4.50, default_item_type='restaurant',
                     is_active=True)
    juice = PosItem(category_id=cat_d.id, name='Mango juice',
                    price=3.00, default_item_type='restaurant',
                    is_active=True)
    burger = PosItem(category_id=cat_f.id, name='Tuna burger',
                     price=12.00, default_item_type='restaurant',
                     is_active=True)
    inactive = PosItem(category_id=cat_f.id, name='Old item',
                       price=10.0, default_item_type='restaurant',
                       is_active=False)
    db.session.add_all([coffee, juice, burger, inactive])
    db.session.commit()
    return {'cat_d': cat_d, 'cat_f': cat_f,
            'coffee': coffee, 'juice': juice,
            'burger': burger, 'inactive': inactive}


# ─────────────────────────────────────────────────────────────────────
# Common base — patches WhatsApp + AI providers (Req 9)
# ─────────────────────────────────────────────────────────────────────

class _BaseAppTest(unittest.TestCase):

    def setUp(self):
        self._patches = []
        self._wa_send = mock.patch.object(
            wa, '_send', side_effect=AssertionError(
                'WhatsApp must not be called by POS V1'))
        self._patches.append(self._wa_send.start())
        self._wa_template = mock.patch.object(
            wa, '_send_template', side_effect=AssertionError(
                'WhatsApp templates must not be called by POS V1'))
        self._patches.append(self._wa_template.start())
        self._ai_patch = mock.patch.object(
            ai_drafts, 'generate_draft',
            side_effect=AssertionError(
                'AI drafts must not be called by POS V1'))
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

    def _login(self, uid):
        with self.client.session_transaction() as sess:
            sess['_user_id'] = str(uid)
            sess['_fresh'] = True


# ─────────────────────────────────────────────────────────────────────
# 3) Cart total (Req 3)
# ─────────────────────────────────────────────────────────────────────

class CartValidationTests(_BaseAppTest):

    def setUp(self):
        super().setUp()
        self.cat = _seed_catalog()

    def test_cart_total_simple(self):
        cart = [
            {'pos_item_id': self.cat['coffee'].id, 'qty': 2},   # 2 × 4.50
            {'pos_item_id': self.cat['juice'].id,  'qty': 1},   # 1 × 3.00
        ]
        v = pos_svc.validate_cart(cart)
        self.assertEqual(v['errors'], [])
        self.assertEqual(pos_svc.cart_total(v['cleaned']), 12.00)

    def test_empty_cart_rejected(self):
        v = pos_svc.validate_cart([])
        self.assertTrue(v['errors'])

    def test_inactive_item_rejected(self):
        v = pos_svc.validate_cart([{
            'pos_item_id': self.cat['inactive'].id, 'qty': 1}])
        self.assertTrue(any('inactive' in e for e in v['errors']))

    def test_zero_qty_rejected(self):
        v = pos_svc.validate_cart([{
            'pos_item_id': self.cat['coffee'].id, 'qty': 0}])
        self.assertTrue(any('qty' in e for e in v['errors']))

    def test_huge_qty_rejected(self):
        v = pos_svc.validate_cart([{
            'pos_item_id': self.cat['coffee'].id, 'qty': 100}])
        self.assertTrue(any('99' in e for e in v['errors']))

    def test_price_override_negative_rejected(self):
        v = pos_svc.validate_cart([{
            'pos_item_id': self.cat['coffee'].id, 'qty': 1,
            'price_override': -1}])
        self.assertTrue(any('negative' in e for e in v['errors']))


# ─────────────────────────────────────────────────────────────────────
# 1) Catalog admin CRUD (Req 1)
# ─────────────────────────────────────────────────────────────────────

class CatalogAdminTests(_BaseAppTest):

    def setUp(self):
        super().setUp()
        self._login(self.admin_id)

    def test_create_category(self):
        r = self.client.post('/pos/admin/categories/new', data={
            'name': 'Drinks', 'sort_order': '1',
        })
        self.assertIn(r.status_code, (301, 302))
        self.assertEqual(PosCategory.query.count(), 1)
        self.assertEqual(PosCategory.query.first().name, 'Drinks')

    def test_duplicate_category_rejected(self):
        db.session.add(PosCategory(name='Drinks', sort_order=1, is_active=True))
        db.session.commit()
        r = self.client.post('/pos/admin/categories/new', data={
            'name': 'Drinks',
        })
        self.assertEqual(r.status_code, 400)
        self.assertEqual(PosCategory.query.count(), 1)

    def test_create_item(self):
        cat = PosCategory(name='Drinks', sort_order=1, is_active=True)
        db.session.add(cat); db.session.commit()
        r = self.client.post('/pos/admin/items/new', data={
            'category_id': str(cat.id),
            'name': 'Espresso',
            'price': '4.50',
            'default_item_type': 'restaurant',
        })
        self.assertIn(r.status_code, (301, 302))
        self.assertEqual(PosItem.query.count(), 1)
        self.assertEqual(PosItem.query.first().price, 4.50)

    def test_invalid_item_type_rejected(self):
        cat = PosCategory(name='X', sort_order=1, is_active=True)
        db.session.add(cat); db.session.commit()
        r = self.client.post('/pos/admin/items/new', data={
            'category_id': str(cat.id),
            'name': 'X', 'price': '5',
            'default_item_type': 'discount',  # disallowed
        })
        self.assertEqual(r.status_code, 400)
        self.assertEqual(PosItem.query.count(), 0)

    def test_negative_price_rejected(self):
        cat = PosCategory(name='X', sort_order=1, is_active=True)
        db.session.add(cat); db.session.commit()
        r = self.client.post('/pos/admin/items/new', data={
            'category_id': str(cat.id),
            'name': 'X', 'price': '-1',
            'default_item_type': 'restaurant',
        })
        self.assertEqual(r.status_code, 400)


# ─────────────────────────────────────────────────────────────────────
# 2) Auth gates (Req 2)
# ─────────────────────────────────────────────────────────────────────

class AuthTests(_BaseAppTest):

    def test_terminal_requires_login(self):
        r = self.client.get('/pos/')
        self.assertIn(r.status_code, (301, 302, 401))

    def test_terminal_admin_allowed(self):
        self._login(self.admin_id)
        r = self.client.get('/pos/')
        self.assertEqual(r.status_code, 200)
        self.assertIn(b'POS Terminal', r.data)

    def test_terminal_staff_allowed(self):
        # POS terminal is whitelisted for staff (the staff_guard
        # bypass). Staff should reach the terminal cleanly.
        self._login(self.staff_id)
        r = self.client.get('/pos/')
        self.assertEqual(r.status_code, 200)

    def test_admin_catalog_blocks_staff(self):
        # Admin catalog routes still gated by admin_required even
        # though the path is whitelisted at /pos.
        self._login(self.staff_id)
        r = self.client.get('/pos/admin/categories', follow_redirects=False)
        self.assertIn(r.status_code, (302, 401, 403))

    def test_admin_catalog_admin_allowed(self):
        self._login(self.admin_id)
        r = self.client.get('/pos/admin/categories')
        self.assertEqual(r.status_code, 200)


# ─────────────────────────────────────────────────────────────────────
# 4 + 8) Post-to-room creates folio items + audit (Reqs 4, 8)
# ─────────────────────────────────────────────────────────────────────

class PostToRoomTests(_BaseAppTest):

    def setUp(self):
        super().setUp()
        self._login(self.staff_id)
        self.cat = _seed_catalog()
        self.room = _seed_room('1')
        self.booking = _seed_booking(self.room, status='checked_in')

    def _submit(self, cart, mode='room', **extra):
        data = {
            'booking_id': str(self.booking.id),
            'mode':       mode,
            'cart_json':  json.dumps(cart),
        }
        data.update(extra)
        return self.client.post('/pos/post', data=data,
                                 follow_redirects=False)

    def test_post_to_room_creates_folio_items(self):
        r = self._submit([
            {'pos_item_id': self.cat['coffee'].id, 'qty': 2},
            {'pos_item_id': self.cat['juice'].id,  'qty': 1},
        ])
        self.assertIn(r.status_code, (301, 302))

        items = (FolioItem.query
                 .filter_by(booking_id=self.booking.id)
                 .all())
        self.assertEqual(len(items), 2)
        for fi in items:
            self.assertEqual(fi.source_module, 'pos')
            self.assertEqual(fi.status, 'open')
            self.assertEqual(fi.item_type, 'restaurant')
        # 2×4.50 + 1×3.00 = 12.00
        self.assertEqual(folio_svc.folio_balance(self.booking), 12.00)
        # NO cashier transaction was created
        self.assertEqual(CashierTransaction.query.count(), 0)

    def test_audit_rows_written(self):
        self._submit([{'pos_item_id': self.cat['coffee'].id, 'qty': 1}])
        n_posted = ActivityLog.query.filter_by(
            action='pos.item_posted_to_folio').count()
        n_cart   = ActivityLog.query.filter_by(
            action='pos.cart_submitted').count()
        n_paid   = ActivityLog.query.filter_by(
            action='pos.sale_paid').count()
        self.assertEqual(n_posted, 1)
        self.assertEqual(n_cart,   1)
        self.assertEqual(n_paid,   0)


# ─────────────────────────────────────────────────────────────────────
# 5) Direct payment via pay_now (Req 5)
# ─────────────────────────────────────────────────────────────────────

class PayNowTests(_BaseAppTest):

    def setUp(self):
        super().setUp()
        self._login(self.staff_id)
        self.cat = _seed_catalog()
        self.room = _seed_room('1')
        self.booking = _seed_booking(self.room, status='checked_in')

    def test_pay_now_creates_cashier_transaction(self):
        r = self.client.post('/pos/post', data={
            'booking_id':     str(self.booking.id),
            'mode':           'pay_now',
            'payment_method': 'cash',
            'cart_json': json.dumps([
                {'pos_item_id': self.cat['burger'].id, 'qty': 1},
            ]),
        })
        self.assertIn(r.status_code, (301, 302))
        # 1 charge folio item + 1 payment folio item
        self.assertEqual(FolioItem.query.filter_by(
            booking_id=self.booking.id).count(), 2)
        # 1 cashier transaction posted
        txns = CashierTransaction.query.all()
        self.assertEqual(len(txns), 1)
        self.assertEqual(txns[0].status, 'posted')
        self.assertEqual(txns[0].transaction_type, 'payment')
        self.assertEqual(txns[0].amount, 12.0)
        self.assertEqual(txns[0].payment_method, 'cash')
        # Linked to a folio_item
        self.assertIsNotNone(txns[0].folio_item_id)
        # Folio balance is zero (charge + matching negative payment)
        self.assertEqual(folio_svc.folio_balance(self.booking), 0.0)
        # sale_paid audit row exists
        n_paid = ActivityLog.query.filter_by(
            action='pos.sale_paid').count()
        self.assertEqual(n_paid, 1)

    def test_pay_now_without_method_rejected(self):
        r = self.client.post('/pos/post', data={
            'booking_id': str(self.booking.id),
            'mode':       'pay_now',
            # no payment_method
            'cart_json': json.dumps([
                {'pos_item_id': self.cat['burger'].id, 'qty': 1},
            ]),
        })
        self.assertIn(r.status_code, (301, 302))
        self.assertEqual(FolioItem.query.count(), 0)
        self.assertEqual(CashierTransaction.query.count(), 0)
        # sale_failed audit row exists
        self.assertEqual(
            ActivityLog.query.filter_by(action='pos.sale_failed').count(),
            1,
        )


# ─────────────────────────────────────────────────────────────────────
# 6 + 7) Booking guards (Reqs 6, 7)
# ─────────────────────────────────────────────────────────────────────

class BookingGuardTests(_BaseAppTest):

    def setUp(self):
        super().setUp()
        self._login(self.staff_id)
        self.cat = _seed_catalog()
        self.room = _seed_room('1')

    def test_unknown_booking_rejected(self):
        r = self.client.post('/pos/post', data={
            'booking_id': '99999',
            'mode':       'room',
            'cart_json':  json.dumps([{'pos_item_id': self.cat['coffee'].id,
                                       'qty': 1}]),
        })
        self.assertIn(r.status_code, (301, 302))
        self.assertEqual(FolioItem.query.count(), 0)

    def test_no_booking_rejected(self):
        r = self.client.post('/pos/post', data={
            'mode':       'room',
            'cart_json':  json.dumps([{'pos_item_id': self.cat['coffee'].id,
                                       'qty': 1}]),
        })
        self.assertIn(r.status_code, (301, 302))
        self.assertEqual(FolioItem.query.count(), 0)

    def test_checked_out_booking_rejected(self):
        b = _seed_booking(self.room, status='checked_out')
        r = self.client.post('/pos/post', data={
            'booking_id': str(b.id),
            'mode':       'room',
            'cart_json':  json.dumps([{'pos_item_id': self.cat['coffee'].id,
                                       'qty': 1}]),
        })
        self.assertIn(r.status_code, (301, 302))
        self.assertEqual(FolioItem.query.count(), 0)
        # sale_failed row written
        rows = ActivityLog.query.filter_by(action='pos.sale_failed').all()
        self.assertEqual(len(rows), 1)
        meta = json.loads(rows[0].metadata_json or '{}')
        self.assertIn('reasons', meta)
        self.assertIn('checked out', meta['reasons'])

    def test_cancelled_booking_rejected(self):
        b = _seed_booking(self.room, status='cancelled')
        r = self.client.post('/pos/post', data={
            'booking_id': str(b.id),
            'mode':       'room',
            'cart_json':  json.dumps([{'pos_item_id': self.cat['coffee'].id,
                                       'qty': 1}]),
        })
        self.assertIn(r.status_code, (301, 302))
        self.assertEqual(FolioItem.query.count(), 0)


# ─────────────────────────────────────────────────────────────────────
# 9) No external coupling (Req 9)
# ─────────────────────────────────────────────────────────────────────

class NoExternalCouplingTests(_BaseAppTest):

    def test_full_flow_no_external_calls(self):
        self._login(self.staff_id)
        cat = _seed_catalog()
        room = _seed_room('1')
        booking = _seed_booking(room, status='checked_in')
        # Post to room
        self.client.post('/pos/post', data={
            'booking_id': str(booking.id), 'mode': 'room',
            'cart_json': json.dumps([
                {'pos_item_id': cat['coffee'].id, 'qty': 1},
            ]),
        })
        # Pay now
        self.client.post('/pos/post', data={
            'booking_id': str(booking.id), 'mode': 'pay_now',
            'payment_method': 'cash',
            'cart_json': json.dumps([
                {'pos_item_id': cat['juice'].id, 'qty': 2},
            ]),
        })
        self.assertEqual(wa._send.call_count,           0)
        self.assertEqual(wa._send_template.call_count,  0)
        self.assertEqual(ai_drafts.generate_draft.call_count, 0)


# ─────────────────────────────────────────────────────────────────────
# 10 + 11) Migration shape (Reqs 10, 11)
# ─────────────────────────────────────────────────────────────────────

class MigrationShapeTests(unittest.TestCase):

    def test_migration_file_exists(self):
        self.assertTrue(_MIGRATION_PATH.exists(),
                        f'expected migration at {_MIGRATION_PATH}')

    def test_migration_revision_metadata(self):
        text = _MIGRATION_PATH.read_text()
        self.assertIn("revision = 'd6a7b9c0e215'", text)
        self.assertIn("down_revision = 'c5d2a3f8e103'", text)

    def test_migration_creates_only_pos_tables(self):
        text = _MIGRATION_PATH.read_text()
        creates = set(re.findall(r"op\.create_table\(\s*'([^']+)'", text))
        self.assertEqual(
            creates,
            {'pos_categories', 'pos_items'},
            f'unexpected tables: {creates}',
        )
        # No mutation of existing tables
        self.assertNotIn('op.add_column', text)
        self.assertNotIn('op.alter_column', text)
        # Round-trip drops
        self.assertIn("op.drop_table('pos_items')", text)
        self.assertIn("op.drop_table('pos_categories')", text)


if __name__ == '__main__':
    unittest.main()
