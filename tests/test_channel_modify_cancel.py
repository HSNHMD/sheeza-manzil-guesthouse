"""Tests for OTA Modification + Cancellation Handling V1.

Covers the spec's 9 requirements:

  1. valid modification updates booking
  2. conflicting modification goes to exception queue
  3. valid cancellation updates booking state correctly
  4. unsafe cancellation goes to exception queue
  5. duplicate event is ignored/skipped safely
  6. external reservation ref lookup works
  7. ActivityLog/sync logs created
  8. no WhatsApp/Gemini coupling
  9. no production coupling

Plus the migration-shape guard.
"""

from __future__ import annotations

import os
import unittest
from datetime import date, timedelta
from pathlib import Path

for _v in ('DATABASE_URL', 'AI_DRAFT_PROVIDER', 'AI_DRAFT_MODEL',
           'GEMINI_API_KEY', 'ANTHROPIC_API_KEY',
           'WHATSAPP_ENABLED', 'WHATSAPP_TOKEN'):
    os.environ.pop(_v, None)
os.environ.setdefault('SECRET_KEY', 'test-secret-do-not-use-in-prod')

from config import Config                                       # noqa: E402
from app import create_app                                      # noqa: E402
from app.models import (                                        # noqa: E402
    db, User, Room, Guest, Booking, Invoice, ActivityLog,
    RoomType, RatePlan, ChannelConnection, ChannelRoomMap,
    ChannelRatePlanMap, ChannelImportException, ChannelInboundEvent,
)
from app.services import channel_import as ci_svc               # noqa: E402


_REPO_ROOT = Path(__file__).resolve().parent.parent
_MIGRATION_PATH = (
    _REPO_ROOT / 'migrations' / 'versions'
    / 'd6a2f59b8e34_add_channel_inbound_events.py'
)


class _TestConfig(Config):
    TESTING = True
    SQLALCHEMY_DATABASE_URI = 'sqlite:///:memory:'
    WTF_CSRF_ENABLED = False
    WHATSAPP_ENABLED = False


def _seed_with_booking():
    """Builds the channel/mappings fixture AND a baseline OTA-imported
    booking for ref BDC-MOD-001 — so tests can directly drive
    apply_modification / apply_cancellation without going through
    apply_import every time.
    """
    admin = User(username='admin', email='a@x', role='admin')
    admin.set_password('aaaaaaaaaa1')
    staff = User(username='staff', email='s@x', role='staff',
                 department='front_office')
    staff.set_password('aaaaaaaaaa1')
    db.session.add_all([admin, staff]); db.session.flush()

    rt = RoomType(code='DBL', name='Deluxe Double', max_occupancy=2,
                  base_capacity=2, is_active=True)
    db.session.add(rt); db.session.flush()

    rt_twin = RoomType(code='TWN', name='Twin', max_occupancy=2,
                       base_capacity=2, is_active=True)
    db.session.add(rt_twin); db.session.flush()

    rooms = []
    for i, (n, t) in enumerate([
            ('101', rt.id), ('102', rt.id),
            ('201', rt_twin.id),
            ]):
        r = Room(number=n, name='Test',
                 room_type='Deluxe Double' if t == rt.id else 'Twin',
                 room_type_id=t,
                 floor=1, capacity=2, price_per_night=600.0,
                 is_active=True,
                 status='available', housekeeping_status='clean')
        db.session.add(r); rooms.append(r)
    db.session.flush()

    rp = RatePlan(code='BAR', name='Best Available',
                  room_type_id=rt.id, base_rate=600.0,
                  currency='USD', is_refundable=True, is_active=True)
    db.session.add(rp); db.session.flush()

    conn = ChannelConnection(channel_name='booking_com',
                             status='sandbox', property_id=1)
    db.session.add(conn); db.session.flush()
    rm = ChannelRoomMap(channel_connection_id=conn.id,
                        room_type_id=rt.id,
                        external_room_id='BDC-DBL-01',
                        is_active=True)
    rm_twin = ChannelRoomMap(channel_connection_id=conn.id,
                              room_type_id=rt_twin.id,
                              external_room_id='BDC-TWN-01',
                              is_active=True)
    db.session.add_all([rm, rm_twin]); db.session.commit()

    today = date.today()
    base_payload = {
        'external_reservation_ref': 'BDC-MOD-001',
        'external_room_id':         'BDC-DBL-01',
        'check_in':                 (today + timedelta(days=14)).isoformat(),
        'check_out':                (today + timedelta(days=17)).isoformat(),
        'num_guests':               2,
        'guest_first_name':         'Original',
        'guest_last_name':          'Guest',
        'total_amount':             1800.0,
    }
    res = ci_svc.import_reservation(
        connection=conn, payload=base_payload,
        actor_user_id=admin.id,
    )
    assert res.action == 'imported', res.message

    return {
        'admin_id':       admin.id,
        'staff_id':       staff.id,
        'room_type_id':   rt.id,
        'room_type_twin': rt_twin.id,
        'room_ids':       [r.id for r in rooms[:2]],
        'twin_room_id':   rooms[2].id,
        'rate_plan_id':   rp.id,
        'conn_id':        conn.id,
        'conn':           conn,
        'booking_id':     res.booking.id,
        'booking_ref_local': res.booking.booking_ref,
    }


# ── Modification — happy paths ─────────────────────────────────────

class ModificationHappyPathTests(unittest.TestCase):

    def setUp(self):
        self.app = create_app(_TestConfig)
        self.ctx = self.app.app_context()
        self.ctx.push()
        db.create_all()
        self.ids = _seed_with_booking()

    def tearDown(self):
        db.session.remove()
        db.drop_all()
        self.ctx.pop()

    def test_extend_dates_applied(self):
        b_before = Booking.query.get(self.ids['booking_id'])
        new_co = (b_before.check_out_date + timedelta(days=2)).isoformat()
        r = ci_svc.apply_modification(
            connection=self.ids['conn'],
            payload={'external_reservation_ref': 'BDC-MOD-001',
                     'external_event_id': 'mod-1',
                     'check_out': new_co},
            actor_user_id=self.ids['admin_id'],
        )
        self.assertTrue(r.ok)
        self.assertEqual(r.action, 'imported')
        b_after = Booking.query.get(self.ids['booking_id'])
        self.assertEqual(b_after.check_out_date.isoformat(), new_co)
        # ActivityLog: channel.reservation_modified
        self.assertEqual(
            ActivityLog.query.filter_by(
                action='channel.reservation_modified').count(), 1)
        # ChannelInboundEvent recorded
        self.assertEqual(
            ChannelInboundEvent.query.filter_by(
                external_event_id='mod-1').count(), 1)

    def test_change_num_guests_applied(self):
        r = ci_svc.apply_modification(
            connection=self.ids['conn'],
            payload={'external_reservation_ref': 'BDC-MOD-001',
                     'external_event_id': 'mod-ng',
                     'num_guests': 1},
            actor_user_id=self.ids['admin_id'],
        )
        self.assertTrue(r.ok)
        b = Booking.query.get(self.ids['booking_id'])
        self.assertEqual(b.num_guests, 1)

    def test_guest_fields_applied(self):
        r = ci_svc.apply_modification(
            connection=self.ids['conn'],
            payload={'external_reservation_ref': 'BDC-MOD-001',
                     'external_event_id': 'mod-name',
                     'guest_first_name': 'Renamed',
                     'guest_email': 'renamed@example.com'},
            actor_user_id=self.ids['admin_id'],
        )
        self.assertTrue(r.ok)
        b = Booking.query.get(self.ids['booking_id'])
        self.assertEqual(b.guest.first_name, 'Renamed')
        self.assertEqual(b.guest.email, 'renamed@example.com')

    def test_no_op_when_payload_matches_state(self):
        b_before = Booking.query.get(self.ids['booking_id'])
        r = ci_svc.apply_modification(
            connection=self.ids['conn'],
            payload={'external_reservation_ref': 'BDC-MOD-001',
                     'external_event_id': 'mod-noop',
                     'num_guests': b_before.num_guests},
            actor_user_id=self.ids['admin_id'],
        )
        self.assertTrue(r.ok)
        # Still recorded as a successful event for replay protection
        self.assertEqual(
            ChannelInboundEvent.query.filter_by(
                external_event_id='mod-noop').count(), 1)


# ── Modification — failure branches ────────────────────────────────

class ModificationFailureTests(unittest.TestCase):

    def setUp(self):
        self.app = create_app(_TestConfig)
        self.ctx = self.app.app_context()
        self.ctx.push()
        db.create_all()
        self.ids = _seed_with_booking()

    def tearDown(self):
        db.session.remove()
        db.drop_all()
        self.ctx.pop()

    def test_booking_not_found_queued(self):
        r = ci_svc.apply_modification(
            connection=self.ids['conn'],
            payload={'external_reservation_ref': 'NEVER-IMPORTED',
                     'external_event_id': 'mod-nf',
                     'num_guests': 3},
            actor_user_id=self.ids['admin_id'],
        )
        self.assertEqual(r.action, 'queued')
        self.assertEqual(r.exception.issue_type, 'booking_not_found')

    def test_unsafe_state_queued(self):
        b = Booking.query.get(self.ids['booking_id'])
        b.status = 'checked_in'; db.session.commit()

        r = ci_svc.apply_modification(
            connection=self.ids['conn'],
            payload={'external_reservation_ref': 'BDC-MOD-001',
                     'external_event_id': 'mod-checked-in',
                     'num_guests': 1},
            actor_user_id=self.ids['admin_id'],
        )
        self.assertEqual(r.action, 'queued')
        self.assertEqual(r.exception.issue_type,
                         'modification_unsafe_state')
        self.assertEqual(r.exception.linked_booking_id, b.id)

    def test_conflict_when_extending_into_other_booking(self):
        # Block both Deluxe rooms for the day immediately after current
        # check_out. Then try to extend the OTA booking into that day.
        b = Booking.query.get(self.ids['booking_id'])
        next_day = b.check_out_date
        block_guest = Guest(first_name='Block', last_name='Guest')
        db.session.add(block_guest); db.session.flush()
        # Cover BOTH rooms of the type so re-allocation can't dodge.
        for idx, rid in enumerate(self.ids['room_ids']):
            db.session.add(Booking(
                booking_ref=f'BLK{idx}',
                room_id=rid, guest_id=block_guest.id,
                check_in_date=next_day,
                check_out_date=next_day + timedelta(days=2),
                num_guests=2, status='confirmed',
                total_amount=600.0,
                source='direct', billing_target='guest',
                created_by=self.ids['admin_id'],
            ))
        db.session.commit()

        r = ci_svc.apply_modification(
            connection=self.ids['conn'],
            payload={'external_reservation_ref': 'BDC-MOD-001',
                     'external_event_id': 'mod-extend',
                     'check_out': (next_day + timedelta(days=2)).isoformat()},
            actor_user_id=self.ids['admin_id'],
        )
        self.assertEqual(r.action, 'queued')
        self.assertEqual(r.exception.issue_type, 'conflict')

    def test_unknown_room_mapping_queued(self):
        r = ci_svc.apply_modification(
            connection=self.ids['conn'],
            payload={'external_reservation_ref': 'BDC-MOD-001',
                     'external_event_id': 'mod-mapless',
                     'external_room_id': 'NEVER-MAPPED'},
            actor_user_id=self.ids['admin_id'],
        )
        self.assertEqual(r.action, 'queued')
        self.assertEqual(r.exception.issue_type, 'mapping_missing')


# ── Cancellation — happy + idempotent ──────────────────────────────

class CancellationTests(unittest.TestCase):

    def setUp(self):
        self.app = create_app(_TestConfig)
        self.ctx = self.app.app_context()
        self.ctx.push()
        db.create_all()
        self.ids = _seed_with_booking()

    def tearDown(self):
        db.session.remove()
        db.drop_all()
        self.ctx.pop()

    def test_safe_cancel_applied(self):
        r = ci_svc.apply_cancellation(
            connection=self.ids['conn'],
            payload={'external_reservation_ref': 'BDC-MOD-001',
                     'external_event_id': 'cncl-1',
                     'reason': 'Guest requested'},
            actor_user_id=self.ids['admin_id'],
        )
        self.assertTrue(r.ok)
        self.assertEqual(r.action, 'imported')
        b = Booking.query.get(self.ids['booking_id'])
        self.assertEqual(b.status, 'cancelled')
        self.assertEqual(
            ActivityLog.query.filter_by(
                action='channel.reservation_cancelled').count(), 1)

    def test_already_cancelled_idempotent(self):
        # First cancel — happy path
        ci_svc.apply_cancellation(
            connection=self.ids['conn'],
            payload={'external_reservation_ref': 'BDC-MOD-001',
                     'external_event_id': 'cncl-first'},
            actor_user_id=self.ids['admin_id'],
        )
        # Second cancel with a NEW event_id — should hit the
        # already_cancelled branch (not duplicate dedup), still ok=True.
        r2 = ci_svc.apply_cancellation(
            connection=self.ids['conn'],
            payload={'external_reservation_ref': 'BDC-MOD-001',
                     'external_event_id': 'cncl-second'},
            actor_user_id=self.ids['admin_id'],
        )
        self.assertTrue(r2.ok)
        self.assertEqual(r2.action, 'duplicate_skipped')
        # event row records the special status
        ev = (ChannelInboundEvent.query
              .filter_by(external_event_id='cncl-second').first())
        self.assertIsNotNone(ev)
        self.assertEqual(ev.result_status, 'already_cancelled')

    def test_checked_in_cancel_queued(self):
        b = Booking.query.get(self.ids['booking_id'])
        b.status = 'checked_in'; db.session.commit()

        r = ci_svc.apply_cancellation(
            connection=self.ids['conn'],
            payload={'external_reservation_ref': 'BDC-MOD-001',
                     'external_event_id': 'cncl-ci'},
            actor_user_id=self.ids['admin_id'],
        )
        self.assertEqual(r.action, 'queued')
        self.assertEqual(r.exception.issue_type, 'cancel_unsafe_state')
        b2 = Booking.query.get(self.ids['booking_id'])
        self.assertEqual(b2.status, 'checked_in')  # unchanged

    def test_paid_invoice_cancel_queued(self):
        b = Booking.query.get(self.ids['booking_id'])
        inv = Invoice(invoice_number='INV-PAID', booking_id=b.id,
                      issue_date=date.today(),
                      subtotal=1800.0, total_amount=1800.0,
                      amount_paid=1800.0, payment_status='paid',
                      invoice_to='Original Guest')
        db.session.add(inv); db.session.commit()

        r = ci_svc.apply_cancellation(
            connection=self.ids['conn'],
            payload={'external_reservation_ref': 'BDC-MOD-001',
                     'external_event_id': 'cncl-paid'},
            actor_user_id=self.ids['admin_id'],
        )
        self.assertEqual(r.action, 'queued')
        self.assertEqual(r.exception.issue_type, 'cancel_unsafe_state')

    def test_not_found_cancel_queued(self):
        r = ci_svc.apply_cancellation(
            connection=self.ids['conn'],
            payload={'external_reservation_ref': 'NOT-A-THING',
                     'external_event_id': 'cncl-nf'},
            actor_user_id=self.ids['admin_id'],
        )
        self.assertEqual(r.action, 'queued')
        self.assertEqual(r.exception.issue_type, 'booking_not_found')


# ── Duplicate event safety ─────────────────────────────────────────

class DuplicateEventTests(unittest.TestCase):

    def setUp(self):
        self.app = create_app(_TestConfig)
        self.ctx = self.app.app_context()
        self.ctx.push()
        db.create_all()
        self.ids = _seed_with_booking()

    def tearDown(self):
        db.session.remove()
        db.drop_all()
        self.ctx.pop()

    def test_modify_with_same_event_id_is_dedup(self):
        first = ci_svc.apply_modification(
            connection=self.ids['conn'],
            payload={'external_reservation_ref': 'BDC-MOD-001',
                     'external_event_id': 'dup-event-1',
                     'num_guests': 1},
            actor_user_id=self.ids['admin_id'],
        )
        self.assertEqual(first.action, 'imported')

        second = ci_svc.apply_modification(
            connection=self.ids['conn'],
            payload={'external_reservation_ref': 'BDC-MOD-001',
                     'external_event_id': 'dup-event-1',
                     'num_guests': 4},  # different value would change state
            actor_user_id=self.ids['admin_id'],
        )
        self.assertEqual(second.action, 'duplicate_skipped')
        b = Booking.query.get(self.ids['booking_id'])
        self.assertEqual(b.num_guests, 1)  # second event NOT applied
        self.assertGreaterEqual(
            ActivityLog.query.filter_by(
                action='channel.event_duplicate_skipped').count(), 1)

    def test_cancel_with_same_event_id_is_dedup(self):
        first = ci_svc.apply_cancellation(
            connection=self.ids['conn'],
            payload={'external_reservation_ref': 'BDC-MOD-001',
                     'external_event_id': 'dup-cncl-1'},
            actor_user_id=self.ids['admin_id'],
        )
        self.assertEqual(first.action, 'imported')

        # Reset status, attempt the same event_id — should dedup
        b = Booking.query.get(self.ids['booking_id'])
        b.status = 'confirmed'; db.session.commit()

        second = ci_svc.apply_cancellation(
            connection=self.ids['conn'],
            payload={'external_reservation_ref': 'BDC-MOD-001',
                     'external_event_id': 'dup-cncl-1'},
            actor_user_id=self.ids['admin_id'],
        )
        self.assertEqual(second.action, 'duplicate_skipped')
        b2 = Booking.query.get(self.ids['booking_id'])
        self.assertEqual(b2.status, 'confirmed')  # not cancelled again

    def test_synthetic_event_id_dedup_when_payload_identical(self):
        """No `external_event_id` provided → service derives one
        deterministically from payload contents. Two replays of the
        identical payload should dedupe."""
        payload = {
            'external_reservation_ref': 'BDC-MOD-001',
            'num_guests': 1,
        }
        first = ci_svc.apply_modification(
            connection=self.ids['conn'], payload=dict(payload),
            actor_user_id=self.ids['admin_id'],
        )
        second = ci_svc.apply_modification(
            connection=self.ids['conn'], payload=dict(payload),
            actor_user_id=self.ids['admin_id'],
        )
        self.assertEqual(first.action, 'imported')
        self.assertEqual(second.action, 'duplicate_skipped')


# ── Routes / HTTP layer ────────────────────────────────────────────

class RouteTests(unittest.TestCase):

    def setUp(self):
        self.app = create_app(_TestConfig)
        self.ctx = self.app.app_context()
        self.ctx.push()
        db.create_all()
        self.ids = _seed_with_booking()
        self.client = self.app.test_client()

    def tearDown(self):
        db.session.remove()
        db.drop_all()
        self.ctx.pop()

    def _login(self, username):
        return self.client.post(
            '/appadmin',
            data={'username': username, 'password': 'aaaaaaaaaa1'},
            follow_redirects=False,
        )

    def test_sandbox_modify_route_applies(self):
        self._login('admin')
        r = self.client.post(
            f'/admin/channels/{self.ids["conn_id"]}/sandbox-modify',
            data={'external_reservation_ref': 'BDC-MOD-001',
                  'external_event_id': 'route-mod-1',
                  'num_guests': '1'},
            follow_redirects=False,
        )
        self.assertIn(r.status_code, (302, 303))
        b = Booking.query.get(self.ids['booking_id'])
        self.assertEqual(b.num_guests, 1)

    def test_sandbox_cancel_route_applies(self):
        self._login('admin')
        r = self.client.post(
            f'/admin/channels/{self.ids["conn_id"]}/sandbox-cancel',
            data={'external_reservation_ref': 'BDC-MOD-001',
                  'external_event_id': 'route-cncl-1'},
            follow_redirects=False,
        )
        self.assertIn(r.status_code, (302, 303))
        b = Booking.query.get(self.ids['booking_id'])
        self.assertEqual(b.status, 'cancelled')

    def test_sandbox_modify_anonymous_blocked(self):
        r = self.client.post(
            f'/admin/channels/{self.ids["conn_id"]}/sandbox-modify',
            data={'external_reservation_ref': 'BDC-MOD-001'},
            follow_redirects=False,
        )
        self.assertIn(r.status_code, (302, 303))
        self.assertNotIn('/bookings/', r.headers.get('Location', ''))

    def test_sandbox_cancel_staff_blocked(self):
        self._login('staff')
        r = self.client.post(
            f'/admin/channels/{self.ids["conn_id"]}/sandbox-cancel',
            data={'external_reservation_ref': 'BDC-MOD-001'},
            follow_redirects=False,
        )
        self.assertIn(r.status_code, (302, 303))


# ── Migration shape + isolation guard ──────────────────────────────

class MigrationTests(unittest.TestCase):

    def test_migration_file_exists(self):
        self.assertTrue(_MIGRATION_PATH.is_file(),
                        f'expected {_MIGRATION_PATH}')
        text = _MIGRATION_PATH.read_text()
        self.assertIn("revision      = 'd6a2f59b8e34'", text)
        self.assertIn("'channel_inbound_events'", text)
        self.assertIn('uq_chinev_connection_event', text)


class IsolationTests(unittest.TestCase):
    """Re-confirm the modify/cancel additions don't reach for HTTP /
    messaging libs."""

    _BANNED = ('whatsapp', 'gemini', 'requests', 'urllib', 'httpx',
               'anthropic')

    def _idents(self, source):
        import ast
        names = set()
        tree = ast.parse(source)
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    names.add(alias.name.split('.')[0].lower())
            elif isinstance(node, ast.ImportFrom):
                if node.module:
                    names.add(node.module.split('.')[0].lower())
            elif isinstance(node, ast.Attribute):
                names.add(node.attr.lower())
            elif isinstance(node, ast.Name):
                names.add(node.id.lower())
        return names

    def test_service_module_clean(self):
        idents = self._idents(
            (_REPO_ROOT / 'app' / 'services'
             / 'channel_import.py').read_text())
        for banned in self._BANNED:
            self.assertNotIn(banned, idents,
                             f'unexpected {banned!r} in channel_import.py')


if __name__ == '__main__':
    unittest.main()
