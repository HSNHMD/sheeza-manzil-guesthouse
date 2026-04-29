"""Tests for Online Menu / QR Ordering V1.

Covers the 12 requirements from the build spec, section I:

  1. public menu loads
  2. only active categories/items shown
  3. guest can create order
  4. invalid order rejected
  5. room validation works
  6. order status transitions work
  7. order can be confirmed/delivered/cancelled
  8. folio posting only happens when explicitly triggered
  9. ActivityLog created
 10. no WhatsApp / Gemini calls
 11. migration file exists
 12. migration only creates menu/order-related tables
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
    db, User, Room, Guest, Booking, FolioItem, ActivityLog,
    PosCategory, PosItem, GuestOrder, GuestOrderItem,
)
from app.services import menu_orders as svc                     # noqa: E402
from app.services import folio as folio_svc                     # noqa: E402
from app.services import whatsapp as wa                         # noqa: E402
from app.services import ai_drafts                              # noqa: E402


_REPO_ROOT = Path(__file__).resolve().parent.parent
_MIGRATION_PATH = (
    _REPO_ROOT / 'migrations' / 'versions'
    / 'e8b3c4d7f421_add_guest_order_tables.py'
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
    admin = User(username=f'om_admin_{n}', email=f'a{n}@x', role='admin')
    admin.set_password('aaaaaaaaaa1')
    staff = User(username=f'om_staff_{n}', email=f's{n}@x', role='staff')
    staff.set_password('aaaaaaaaaa1')
    db.session.add_all([admin, staff])
    db.session.commit()
    return admin.id, staff.id


def _seed_room(number='101'):
    r = Room(number=number, name='T', room_type='Test',
             floor=0, capacity=2, price_per_night=600.0,
             status='available', housekeeping_status='clean')
    db.session.add(r); db.session.commit()
    return r


def _seed_booking(room, last_name='Wilson', status='checked_in'):
    g = Guest(first_name='Anna', last_name=last_name,
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
    cat_f = PosCategory(name='Food', sort_order=2, is_active=True)
    cat_old = PosCategory(name='Hidden', sort_order=3, is_active=False)
    db.session.add_all([cat_d, cat_f, cat_old]); db.session.commit()
    espresso = PosItem(category_id=cat_d.id, name='Espresso',
                       price=4.50, default_item_type='restaurant',
                       is_active=True)
    juice = PosItem(category_id=cat_d.id, name='Mango juice',
                    price=3.00, default_item_type='restaurant',
                    is_active=True)
    burger = PosItem(category_id=cat_f.id, name='Tuna burger',
                     price=12.00, default_item_type='restaurant',
                     description='Fresh local tuna',
                     is_active=True)
    inactive = PosItem(category_id=cat_f.id, name='Old item',
                       price=10.0, default_item_type='restaurant',
                       is_active=False)
    db.session.add_all([espresso, juice, burger, inactive])
    db.session.commit()
    return {'cat_d': cat_d, 'cat_f': cat_f, 'cat_old': cat_old,
            'espresso': espresso, 'juice': juice,
            'burger': burger, 'inactive': inactive}


# ─────────────────────────────────────────────────────────────────────
# Common base — patches WhatsApp + AI providers (Req 10)
# ─────────────────────────────────────────────────────────────────────

class _BaseAppTest(unittest.TestCase):

    def setUp(self):
        self._patches = []
        self._wa_send = mock.patch.object(
            wa, '_send', side_effect=AssertionError(
                'WhatsApp must not be called by Online Menu V1'))
        self._patches.append(self._wa_send.start())
        self._wa_template = mock.patch.object(
            wa, '_send_template', side_effect=AssertionError(
                'WhatsApp templates must not be called by Online Menu V1'))
        self._patches.append(self._wa_template.start())
        self._ai_patch = mock.patch.object(
            ai_drafts, 'generate_draft',
            side_effect=AssertionError(
                'AI drafts must not be called by Online Menu V1'))
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
# 1 + 2) Public menu loads + only active items (Reqs 1, 2)
# ─────────────────────────────────────────────────────────────────────

class PublicMenuTests(_BaseAppTest):

    def setUp(self):
        super().setUp()
        self.cat = _seed_catalog()

    def test_menu_renders_for_anonymous(self):
        # Public — no login.
        r = self.client.get('/menu/')
        self.assertEqual(r.status_code, 200)
        self.assertIn(b'Menu', r.data)
        self.assertIn(b'Espresso', r.data)
        self.assertIn(b'Tuna burger', r.data)

    def test_only_active_items_shown(self):
        r = self.client.get('/menu/')
        body = r.get_data(as_text=True)
        self.assertIn('Espresso',     body)
        self.assertNotIn('Old item',  body)   # inactive item hidden
        # Inactive category is hidden — its name shouldn't surface
        # as a category tab. Use a stricter check tied to the
        # category-tab markup so other matches don't fool us.
        self.assertNotIn('data-cat-id="{0}"'
                         .format(self.cat['cat_old'].id), body)

    def test_room_prefill_route(self):
        r = self.client.get('/menu/room/101')
        self.assertEqual(r.status_code, 200)
        self.assertIn(b'value="101"', r.data)

    def test_qr_poster_renders(self):
        r = self.client.get('/menu/qr')
        self.assertEqual(r.status_code, 200)
        # The poster shows the menu URL in plaintext + QR image
        self.assertIn(b'/menu', r.data)
        self.assertIn(b'qrserver.com', r.data)


# ─────────────────────────────────────────────────────────────────────
# 3) Guest can create order (Req 3)
# ─────────────────────────────────────────────────────────────────────

class CreateOrderTests(_BaseAppTest):

    def setUp(self):
        super().setUp()
        self.cat = _seed_catalog()
        self.room = _seed_room('101')
        self.booking = _seed_booking(self.room, last_name='Wilson')

    def test_submit_creates_order_and_redirects(self):
        r = self.client.post('/menu/order', data={
            'cart_json': json.dumps([
                {'pos_item_id': self.cat['espresso'].id, 'qty': 2},
                {'pos_item_id': self.cat['juice'].id,    'qty': 1},
            ]),
            'room_number': '101',
            'guest_name':  'Wilson',
            'source':      'qr_menu',
        }, follow_redirects=False)
        self.assertIn(r.status_code, (301, 302))
        self.assertIn('/menu/order/', r.headers.get('Location', ''))
        self.assertEqual(GuestOrder.query.count(), 1)
        order = GuestOrder.query.first()
        # 2 × 4.50 + 1 × 3.00 = 12.00
        self.assertEqual(order.total_amount, 12.00)
        self.assertEqual(order.items.count(), 2)
        self.assertEqual(order.status, 'new')
        self.assertEqual(order.source, 'qr_menu')
        # Booking matched (room 101 + last name Wilson)
        self.assertEqual(order.booking_id, self.booking.id)

    def test_status_page_renders(self):
        result = svc.create_order(
            cleaned_cart=svc.validate_cart_input([
                {'pos_item_id': self.cat['espresso'].id, 'qty': 1},
            ])['cleaned'],
            room_number='101', guest_name='Wilson',
        )
        db.session.commit()
        r = self.client.get(f'/menu/order/{result["order"].public_token}')
        self.assertEqual(r.status_code, 200)
        self.assertIn(b'Espresso', r.data)
        self.assertIn(b'new', r.data)


# ─────────────────────────────────────────────────────────────────────
# 4) Invalid order rejected (Req 4)
# ─────────────────────────────────────────────────────────────────────

class InvalidOrderTests(_BaseAppTest):

    def setUp(self):
        super().setUp()
        self.cat = _seed_catalog()

    def test_empty_cart_rejected(self):
        r = self.client.post('/menu/order', data={'cart_json': '[]'})
        self.assertIn(r.status_code, (301, 302))
        self.assertEqual(GuestOrder.query.count(), 0)

    def test_inactive_item_rejected(self):
        r = self.client.post('/menu/order', data={
            'cart_json': json.dumps([
                {'pos_item_id': self.cat['inactive'].id, 'qty': 1}]),
        })
        self.assertIn(r.status_code, (301, 302))
        self.assertEqual(GuestOrder.query.count(), 0)

    def test_zero_qty_rejected(self):
        r = self.client.post('/menu/order', data={
            'cart_json': json.dumps([
                {'pos_item_id': self.cat['espresso'].id, 'qty': 0}]),
        })
        self.assertIn(r.status_code, (301, 302))
        self.assertEqual(GuestOrder.query.count(), 0)

    def test_huge_qty_rejected(self):
        r = self.client.post('/menu/order', data={
            'cart_json': json.dumps([
                {'pos_item_id': self.cat['espresso'].id, 'qty': 99}]),
        })
        self.assertIn(r.status_code, (301, 302))
        self.assertEqual(GuestOrder.query.count(), 0)

    def test_unknown_item_rejected(self):
        r = self.client.post('/menu/order', data={
            'cart_json': json.dumps([
                {'pos_item_id': 99999, 'qty': 1}]),
        })
        self.assertIn(r.status_code, (301, 302))
        self.assertEqual(GuestOrder.query.count(), 0)

    def test_malformed_json_rejected(self):
        r = self.client.post('/menu/order', data={
            'cart_json': 'not-json',
        })
        self.assertIn(r.status_code, (301, 302))
        self.assertEqual(GuestOrder.query.count(), 0)


# ─────────────────────────────────────────────────────────────────────
# 5) Room validation (Req 5) — match_booking() behavior
# ─────────────────────────────────────────────────────────────────────

class RoomMatchingTests(_BaseAppTest):

    def setUp(self):
        super().setUp()
        self.cat = _seed_catalog()
        self.room = _seed_room('101')

    def test_match_in_house_booking(self):
        b = _seed_booking(self.room, last_name='Wilson')
        self.assertEqual(svc.match_booking('101', 'Wilson').id, b.id)

    def test_match_case_insensitive(self):
        b = _seed_booking(self.room, last_name='Wilson')
        self.assertEqual(svc.match_booking('101', 'WILSON').id, b.id)
        self.assertEqual(svc.match_booking('101', 'wil').id, b.id)

    def test_match_wrong_name_returns_none(self):
        _seed_booking(self.room, last_name='Wilson')
        self.assertIsNone(svc.match_booking('101', 'Smith'))

    def test_match_wrong_room_returns_none(self):
        _seed_booking(self.room, last_name='Wilson')
        self.assertIsNone(svc.match_booking('999', 'Wilson'))

    def test_match_checked_out_booking_excluded(self):
        _seed_booking(self.room, last_name='Wilson', status='checked_out')
        self.assertIsNone(svc.match_booking('101', 'Wilson'))

    def test_no_input_returns_none(self):
        self.assertIsNone(svc.match_booking(None, 'Wilson'))
        self.assertIsNone(svc.match_booking('101', None))
        self.assertIsNone(svc.match_booking('', ''))

    def test_unmatched_order_still_recorded(self):
        # Order succeeds even when booking match fails — staff resolves later.
        r = self.client.post('/menu/order', data={
            'cart_json': json.dumps([
                {'pos_item_id': self.cat['espresso'].id, 'qty': 1}]),
            'room_number': '999',
            'guest_name':  'NoMatch',
        })
        self.assertIn(r.status_code, (301, 302))
        order = GuestOrder.query.first()
        self.assertIsNotNone(order)
        self.assertIsNone(order.booking_id)
        # But the typed strings are kept verbatim for staff
        self.assertEqual(order.room_number_input, '999')
        self.assertEqual(order.guest_name_input,  'NoMatch')


# ─────────────────────────────────────────────────────────────────────
# 6 + 7) Status transitions (Reqs 6, 7)
# ─────────────────────────────────────────────────────────────────────

class StatusTransitionTests(_BaseAppTest):

    def setUp(self):
        super().setUp()
        self.cat = _seed_catalog()
        self._login(self.admin_id)

    def _make_order(self):
        result = svc.create_order(
            cleaned_cart=svc.validate_cart_input([
                {'pos_item_id': self.cat['espresso'].id, 'qty': 1},
            ])['cleaned'],
            room_number='101', guest_name='Wilson',
        )
        db.session.commit()
        return result['order']

    def test_new_to_confirmed(self):
        o = self._make_order()
        self.client.post(f'/menu/admin/orders/{o.id}/confirm')
        o = db.session.get(GuestOrder, o.id)
        self.assertEqual(o.status, 'confirmed')
        self.assertIsNotNone(o.confirmed_at)
        self.assertEqual(o.confirmed_by_user_id, self.admin_id)

    def test_confirmed_to_delivered(self):
        o = self._make_order()
        self.client.post(f'/menu/admin/orders/{o.id}/confirm')
        self.client.post(f'/menu/admin/orders/{o.id}/deliver')
        o = db.session.get(GuestOrder, o.id)
        self.assertEqual(o.status, 'delivered')
        self.assertIsNotNone(o.delivered_at)

    def test_new_to_cancelled(self):
        o = self._make_order()
        self.client.post(f'/menu/admin/orders/{o.id}/cancel',
                         data={'cancel_reason': 'kitchen closed'})
        o = db.session.get(GuestOrder, o.id)
        self.assertEqual(o.status, 'cancelled')
        self.assertEqual(o.cancel_reason, 'kitchen closed')

    def test_invalid_transition_rejected(self):
        o = self._make_order()
        # new → delivered is not a valid skip
        self.client.post(f'/menu/admin/orders/{o.id}/deliver')
        o = db.session.get(GuestOrder, o.id)
        self.assertEqual(o.status, 'new')

    def test_cancelled_terminal(self):
        o = self._make_order()
        self.client.post(f'/menu/admin/orders/{o.id}/cancel')
        # Can't undo
        self.client.post(f'/menu/admin/orders/{o.id}/confirm')
        o = db.session.get(GuestOrder, o.id)
        self.assertEqual(o.status, 'cancelled')

    def test_admin_queue_renders(self):
        self._make_order()
        r = self.client.get('/menu/admin/')
        self.assertEqual(r.status_code, 200)
        self.assertIn(b'Order Queue', r.data)
        self.assertIn(b'Order #', r.data)

    def test_anonymous_cannot_access_queue(self):
        with self.client.session_transaction() as sess:
            sess.clear()
        r = self.client.get('/menu/admin/')
        self.assertIn(r.status_code, (301, 302, 401))

    def test_staff_cannot_confirm(self):
        o = self._make_order()
        with self.client.session_transaction() as sess:
            sess['_user_id'] = str(self.staff_id)
            sess['_fresh']   = True
        r = self.client.post(f'/menu/admin/orders/{o.id}/confirm')
        self.assertIn(r.status_code, (302, 401, 403))
        # State unchanged
        o = db.session.get(GuestOrder, o.id)
        self.assertEqual(o.status, 'new')


# ─────────────────────────────────────────────────────────────────────
# 8) Folio posting only happens explicitly (Req 8)
# ─────────────────────────────────────────────────────────────────────

class FolioPostingTests(_BaseAppTest):

    def setUp(self):
        super().setUp()
        self.cat = _seed_catalog()
        self.room = _seed_room('101')
        self.booking = _seed_booking(self.room, last_name='Wilson')
        self._login(self.admin_id)

    def _make_linked_order(self):
        result = svc.create_order(
            cleaned_cart=svc.validate_cart_input([
                {'pos_item_id': self.cat['burger'].id, 'qty': 1}])['cleaned'],
            room_number='101', guest_name='Wilson',
        )
        db.session.commit()
        return result['order']

    def test_create_does_not_post_to_folio(self):
        # Just creating an order MUST NOT post to folio.
        o = self._make_linked_order()
        self.assertEqual(FolioItem.query.count(), 0)
        self.assertFalse(o.is_posted_to_folio)

    def test_confirm_does_not_post_to_folio(self):
        o = self._make_linked_order()
        self.client.post(f'/menu/admin/orders/{o.id}/confirm')
        self.assertEqual(FolioItem.query.count(), 0)
        self.assertFalse(db.session.get(GuestOrder, o.id).is_posted_to_folio)

    def test_explicit_post_creates_folio_items(self):
        o = self._make_linked_order()
        self.client.post(f'/menu/admin/orders/{o.id}/post-to-folio')
        items = FolioItem.query.filter_by(booking_id=self.booking.id).all()
        self.assertEqual(len(items), 1)
        fi = items[0]
        self.assertEqual(fi.source_module, 'online_menu')
        self.assertEqual(fi.item_type, 'restaurant')
        self.assertEqual(fi.total_amount, 12.0)
        # Folio balance increased by exactly the order total
        self.assertEqual(folio_svc.folio_balance(self.booking), 12.0)
        # Order recorded the post
        o = db.session.get(GuestOrder, o.id)
        self.assertTrue(o.is_posted_to_folio)
        self.assertIsNotNone(o.posted_to_folio_at)

    def test_double_post_rejected(self):
        o = self._make_linked_order()
        self.client.post(f'/menu/admin/orders/{o.id}/post-to-folio')
        self.client.post(f'/menu/admin/orders/{o.id}/post-to-folio')
        self.assertEqual(FolioItem.query.count(), 1)

    def test_unlinked_order_cannot_post(self):
        # Order with no booking link
        result = svc.create_order(
            cleaned_cart=svc.validate_cart_input([
                {'pos_item_id': self.cat['burger'].id, 'qty': 1}])['cleaned'],
            room_number='999', guest_name='NoMatch',
        )
        db.session.commit()
        o = result['order']
        self.assertIsNone(o.booking_id)
        self.client.post(f'/menu/admin/orders/{o.id}/post-to-folio')
        self.assertEqual(FolioItem.query.count(), 0)
        self.assertFalse(db.session.get(GuestOrder, o.id).is_posted_to_folio)

    def test_attach_booking_then_post(self):
        # Unlinked order → staff attaches booking → post works
        result = svc.create_order(
            cleaned_cart=svc.validate_cart_input([
                {'pos_item_id': self.cat['burger'].id, 'qty': 1}])['cleaned'],
            room_number='999', guest_name='NoMatch',
        )
        db.session.commit()
        o = result['order']
        self.client.post(f'/menu/admin/orders/{o.id}/attach-booking',
                         data={'booking_id': str(self.booking.id)})
        self.client.post(f'/menu/admin/orders/{o.id}/post-to-folio')
        self.assertEqual(FolioItem.query.count(), 1)

    def test_cancelled_order_cannot_post(self):
        o = self._make_linked_order()
        self.client.post(f'/menu/admin/orders/{o.id}/cancel')
        self.client.post(f'/menu/admin/orders/{o.id}/post-to-folio')
        self.assertEqual(FolioItem.query.count(), 0)

    def test_post_to_checked_out_booking_rejected(self):
        # Manually flip the booking to checked_out
        self.booking.status = 'checked_out'
        db.session.commit()
        o = self._make_linked_order()
        self.client.post(f'/menu/admin/orders/{o.id}/post-to-folio')
        self.assertEqual(FolioItem.query.count(), 0)
        self.assertFalse(db.session.get(GuestOrder, o.id).is_posted_to_folio)


# ─────────────────────────────────────────────────────────────────────
# 9) ActivityLog (Req 9)
# ─────────────────────────────────────────────────────────────────────

class ActivityLogTests(_BaseAppTest):

    def setUp(self):
        super().setUp()
        self.cat = _seed_catalog()
        self.room = _seed_room('101')
        self.booking = _seed_booking(self.room, last_name='Wilson')

    def test_create_writes_audit_row(self):
        self.client.post('/menu/order', data={
            'cart_json': json.dumps([
                {'pos_item_id': self.cat['espresso'].id, 'qty': 2}]),
            'room_number': '101', 'guest_name': 'Wilson',
            'source': 'qr_menu',
        })
        rows = ActivityLog.query.filter_by(
            action='guest_order.created').all()
        self.assertEqual(len(rows), 1)
        meta = json.loads(rows[0].metadata_json or '{}')
        self.assertIn('order_id', meta)
        self.assertEqual(meta['source'], 'qr_menu')
        self.assertEqual(meta['booking_id'], self.booking.id)
        self.assertEqual(meta['item_count'], 1)

    def test_lifecycle_writes_audit_rows(self):
        self._login(self.admin_id)
        result = svc.create_order(
            cleaned_cart=svc.validate_cart_input([
                {'pos_item_id': self.cat['burger'].id, 'qty': 1}])['cleaned'],
            room_number='101', guest_name='Wilson',
        )
        db.session.commit()
        o = result['order']
        self.client.post(f'/menu/admin/orders/{o.id}/confirm')
        self.client.post(f'/menu/admin/orders/{o.id}/post-to-folio')
        self.client.post(f'/menu/admin/orders/{o.id}/deliver')
        for action, expected in [
            ('guest_order.confirmed',       1),
            ('guest_order.posted_to_folio', 1),
            ('guest_order.delivered',       1),
        ]:
            self.assertEqual(
                ActivityLog.query.filter_by(action=action).count(),
                expected,
                msg=f'{action} count mismatch',
            )


# ─────────────────────────────────────────────────────────────────────
# 10) No external coupling (Req 10)
# ─────────────────────────────────────────────────────────────────────

class NoExternalCouplingTests(_BaseAppTest):

    def test_full_flow_no_external_calls(self):
        cat = _seed_catalog()
        room = _seed_room('101')
        booking = _seed_booking(room, last_name='Wilson')

        # Public guest submission
        self.client.post('/menu/order', data={
            'cart_json': json.dumps([
                {'pos_item_id': cat['burger'].id, 'qty': 1}]),
            'room_number': '101', 'guest_name': 'Wilson',
        })
        order = GuestOrder.query.first()
        # Status page
        self.client.get(f'/menu/order/{order.public_token}')

        # Admin lifecycle
        self._login(self.admin_id)
        self.client.post(f'/menu/admin/orders/{order.id}/confirm')
        self.client.post(f'/menu/admin/orders/{order.id}/post-to-folio')
        self.client.post(f'/menu/admin/orders/{order.id}/deliver')

        self.assertEqual(wa._send.call_count,           0)
        self.assertEqual(wa._send_template.call_count,  0)
        self.assertEqual(ai_drafts.generate_draft.call_count, 0)


# ─────────────────────────────────────────────────────────────────────
# 11 + 12) Migration shape (Reqs 11, 12)
# ─────────────────────────────────────────────────────────────────────

class MigrationShapeTests(unittest.TestCase):

    def test_migration_file_exists(self):
        self.assertTrue(_MIGRATION_PATH.exists(),
                        f'expected migration at {_MIGRATION_PATH}')

    def test_migration_revision_metadata(self):
        text = _MIGRATION_PATH.read_text()
        self.assertIn("revision = 'e8b3c4d7f421'", text)
        self.assertIn("down_revision = 'd6a7b9c0e215'", text)

    def test_migration_creates_only_order_tables(self):
        text = _MIGRATION_PATH.read_text()
        creates = set(re.findall(r"op\.create_table\(\s*'([^']+)'", text))
        self.assertEqual(
            creates,
            {'guest_orders', 'guest_order_items'},
            f'unexpected tables: {creates}',
        )
        # No mutation of existing tables
        self.assertNotIn('op.add_column', text)
        self.assertNotIn('op.alter_column', text)
        self.assertIn("op.drop_table('guest_orders')", text)
        self.assertIn("op.drop_table('guest_order_items')", text)


if __name__ == '__main__':
    unittest.main()
