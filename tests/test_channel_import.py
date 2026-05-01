"""Tests for OTA Reservation Import + Exception Queue V1.

Covers the 11 requirements from the build spec:

  1. valid import creates a Booking
  2. duplicate import is skipped (idempotent)
  3. conflicting import goes to the exception queue
  4. mapping_missing import goes to the exception queue
  5. invalid_payload import goes to the exception queue
  6. external ref linked correctly on the new Booking
  7. booking source set correctly to the channel name
  8. ActivityLog rows match the spec
  9. Exception queue lifecycle (status transitions)
 10. Admin pages require login/admin
 11. No OTA HTTP / WhatsApp / Gemini coupling

Plus migration-shape guards.
"""

from __future__ import annotations

import os
import unittest
from datetime import date, datetime, timedelta
from pathlib import Path

for _v in ('DATABASE_URL', 'AI_DRAFT_PROVIDER', 'AI_DRAFT_MODEL',
           'GEMINI_API_KEY', 'ANTHROPIC_API_KEY',
           'WHATSAPP_ENABLED', 'WHATSAPP_TOKEN'):
    os.environ.pop(_v, None)
os.environ.setdefault('SECRET_KEY', 'test-secret-do-not-use-in-prod')

from config import Config                                       # noqa: E402
from app import create_app                                      # noqa: E402
from app.models import (                                        # noqa: E402
    db, User, Room, Guest, Booking, ActivityLog,
    RoomType, RatePlan, ChannelConnection, ChannelRoomMap,
    ChannelRatePlanMap, ChannelImportException,
)
from app.services import channels as ch_svc                     # noqa: E402
from app.services import channel_import as ci_svc               # noqa: E402


_REPO_ROOT = Path(__file__).resolve().parent.parent
_MIGRATION_PATH = (
    _REPO_ROOT / 'migrations' / 'versions'
    / 'c4f7d2a86b15_add_channel_import_exceptions.py'
)


class _TestConfig(Config):
    TESTING = True
    SQLALCHEMY_DATABASE_URI = 'sqlite:///:memory:'
    WTF_CSRF_ENABLED = False
    WHATSAPP_ENABLED = False


def _seed():
    """Build a complete fixture: admin/staff, RoomType, two physical
    rooms of that type, RatePlan, ChannelConnection (booking_com),
    ChannelRoomMap, ChannelRatePlanMap.

    Returns a dict of ids + the connection instance for direct use.
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

    # Two physical rooms; both linked to the RoomType via FK so
    # _pick_room_id() finds them immediately.
    r1 = Room(number='101', name='Deluxe Double',
              room_type='Deluxe Double', room_type_id=rt.id,
              floor=1, capacity=2, price_per_night=600.0,
              is_active=True,
              status='available', housekeeping_status='clean')
    r2 = Room(number='102', name='Deluxe Double',
              room_type='Deluxe Double', room_type_id=rt.id,
              floor=1, capacity=2, price_per_night=600.0,
              is_active=True,
              status='available', housekeeping_status='clean')
    db.session.add_all([r1, r2])

    rp = RatePlan(code='BAR', name='Best Available',
                  room_type_id=rt.id, base_rate=600.0,
                  currency='USD', is_refundable=True, is_active=True)
    db.session.add(rp)
    db.session.commit()

    # Connection — pilot is booking_com. create_connection() is the
    # spec-blessed entry point but it goes through the property
    # context processor; we sidestep it by writing the row directly
    # so this fixture stays focused on the import pipeline.
    conn = ChannelConnection(channel_name='booking_com',
                             status='sandbox', property_id=1)
    db.session.add(conn); db.session.flush()

    rm = ChannelRoomMap(channel_connection_id=conn.id,
                        room_type_id=rt.id,
                        external_room_id='BDC-DBL-01',
                        external_room_name_snapshot='Deluxe Double',
                        is_active=True)
    rpm = ChannelRatePlanMap(channel_connection_id=conn.id,
                             rate_plan_id=rp.id,
                             external_rate_plan_id='BDC-RP-FLEX',
                             is_active=True)
    db.session.add_all([rm, rpm])
    db.session.commit()

    return {
        'admin_id': admin.id, 'staff_id': staff.id,
        'room_type_id': rt.id, 'rate_plan_id': rp.id,
        'room_ids': [r1.id, r2.id],
        'conn_id': conn.id, 'conn': conn,
    }


def _payload(**overrides):
    """Build a baseline-valid sandbox payload."""
    today = date.today()
    base = {
        'external_reservation_ref': 'BDC-RES-0001',
        'external_room_id':         'BDC-DBL-01',
        'external_rate_plan_id':    'BDC-RP-FLEX',
        'check_in':                 (today + timedelta(days=14)).isoformat(),
        'check_out':                (today + timedelta(days=17)).isoformat(),
        'num_guests':               2,
        'guest_first_name':         'Alex',
        'guest_last_name':          'Traveler',
        'guest_email':              'alex@example.com',
        'guest_phone':              '+1 555 1234',
        'total_amount':             1800.0,
    }
    base.update(overrides)
    return base


# ── Service: happy path ────────────────────────────────────────────

class HappyPathTests(unittest.TestCase):

    def setUp(self):
        self.app = create_app(_TestConfig)
        self.ctx = self.app.app_context()
        self.ctx.push()
        db.create_all()
        self.ids = _seed()

    def tearDown(self):
        db.session.remove()
        db.drop_all()
        self.ctx.pop()

    def test_valid_import_creates_booking(self):
        r = ci_svc.import_reservation(
            connection=self.ids['conn'], payload=_payload(),
            actor_user_id=self.ids['admin_id'],
        )
        self.assertTrue(r.ok)
        self.assertEqual(r.action, 'imported')
        self.assertIsNotNone(r.booking)
        b = Booking.query.get(r.booking.id)
        self.assertEqual(b.source, 'booking_com')
        self.assertEqual(b.external_source, 'booking_com')
        self.assertEqual(b.external_reservation_ref, 'BDC-RES-0001')
        self.assertEqual(b.status, 'confirmed')
        self.assertEqual(b.num_guests, 2)
        self.assertIn(b.room_id, self.ids['room_ids'])

    def test_external_ref_linked_correctly(self):
        ci_svc.import_reservation(
            connection=self.ids['conn'], payload=_payload(),
            actor_user_id=self.ids['admin_id'],
        )
        b = Booking.query.filter_by(external_source='booking_com').first()
        self.assertIsNotNone(b)
        self.assertEqual(b.external_reservation_ref, 'BDC-RES-0001')

    def test_booking_source_set_correctly(self):
        ci_svc.import_reservation(
            connection=self.ids['conn'], payload=_payload(),
            actor_user_id=self.ids['admin_id'],
        )
        b = Booking.query.filter_by(external_source='booking_com').first()
        self.assertEqual(b.source, 'booking_com')

    def test_imported_emits_activity_log(self):
        before = ActivityLog.query.filter_by(
            action='channel.reservation_imported').count()
        ci_svc.import_reservation(
            connection=self.ids['conn'], payload=_payload(),
            actor_user_id=self.ids['admin_id'],
        )
        after = ActivityLog.query.filter_by(
            action='channel.reservation_imported').count()
        self.assertEqual(after, before + 1)


# ── Service: duplicate / idempotency ───────────────────────────────

class DuplicateTests(unittest.TestCase):

    def setUp(self):
        self.app = create_app(_TestConfig)
        self.ctx = self.app.app_context()
        self.ctx.push()
        db.create_all()
        self.ids = _seed()

    def tearDown(self):
        db.session.remove()
        db.drop_all()
        self.ctx.pop()

    def test_duplicate_import_skipped(self):
        first = ci_svc.import_reservation(
            connection=self.ids['conn'], payload=_payload(),
            actor_user_id=self.ids['admin_id'],
        )
        self.assertEqual(first.action, 'imported')

        second = ci_svc.import_reservation(
            connection=self.ids['conn'], payload=_payload(),
            actor_user_id=self.ids['admin_id'],
        )
        self.assertEqual(second.action, 'duplicate_skipped')
        self.assertTrue(second.ok)  # ok=True so retries are safe
        # only one Booking + one Guest from this OTA ref
        self.assertEqual(
            Booking.query.filter_by(external_source='booking_com').count(),
            1)
        # activity row written
        self.assertGreaterEqual(
            ActivityLog.query.filter_by(
                action='channel.reservation_duplicate_skipped').count(),
            1)


# ── Service: conflict → exception ──────────────────────────────────

class ConflictTests(unittest.TestCase):

    def setUp(self):
        self.app = create_app(_TestConfig)
        self.ctx = self.app.app_context()
        self.ctx.push()
        db.create_all()
        self.ids = _seed()

    def tearDown(self):
        db.session.remove()
        db.drop_all()
        self.ctx.pop()

    def _block_both_rooms(self, check_in, check_out):
        """Stamp existing bookings on both rooms to force overbooking."""
        guest = Guest(first_name='Existing', last_name='Guest')
        db.session.add(guest); db.session.flush()
        for idx, rid in enumerate(self.ids['room_ids']):
            db.session.add(Booking(
                booking_ref=f'EX{idx:03d}',
                room_id=rid, guest_id=guest.id,
                check_in_date=check_in, check_out_date=check_out,
                num_guests=2, status='confirmed',
                total_amount=600.0,
                source='direct', billing_target='guest',
                created_by=self.ids['admin_id'],
            ))
        db.session.commit()

    def test_conflicting_import_goes_to_exception_queue(self):
        today = date.today()
        ci, co = today + timedelta(days=14), today + timedelta(days=17)
        self._block_both_rooms(ci, co)

        r = ci_svc.import_reservation(
            connection=self.ids['conn'],
            payload=_payload(check_in=ci.isoformat(),
                             check_out=co.isoformat(),
                             external_reservation_ref='BDC-RES-CONFLICT'),
            actor_user_id=self.ids['admin_id'],
        )
        self.assertEqual(r.action, 'queued')
        self.assertFalse(r.ok)
        self.assertIsNotNone(r.exception)
        self.assertEqual(r.exception.issue_type, 'conflict')
        self.assertEqual(r.exception.status, 'new')
        # NO Booking created with the OTA ref
        self.assertEqual(
            Booking.query.filter_by(
                external_reservation_ref='BDC-RES-CONFLICT').count(),
            0)
        # activity row
        self.assertGreaterEqual(
            ActivityLog.query.filter_by(
                action='channel.reservation_conflict_queued').count(),
            1)


# ── Service: mapping missing → exception ───────────────────────────

class MappingMissingTests(unittest.TestCase):

    def setUp(self):
        self.app = create_app(_TestConfig)
        self.ctx = self.app.app_context()
        self.ctx.push()
        db.create_all()
        self.ids = _seed()

    def tearDown(self):
        db.session.remove()
        db.drop_all()
        self.ctx.pop()

    def test_unknown_external_room_id_queued(self):
        r = ci_svc.import_reservation(
            connection=self.ids['conn'],
            payload=_payload(external_room_id='UNMAPPED-XYZ'),
            actor_user_id=self.ids['admin_id'],
        )
        self.assertEqual(r.action, 'queued')
        self.assertEqual(r.exception.issue_type, 'mapping_missing')
        # no booking created
        self.assertEqual(
            Booking.query.filter_by(external_source='booking_com').count(),
            0)
        self.assertGreaterEqual(
            ActivityLog.query.filter_by(
                action='channel.reservation_import_failed').count(),
            1)

    def test_unknown_external_rate_plan_id_queued(self):
        r = ci_svc.import_reservation(
            connection=self.ids['conn'],
            payload=_payload(external_rate_plan_id='UNMAPPED-RP'),
            actor_user_id=self.ids['admin_id'],
        )
        self.assertEqual(r.action, 'queued')
        self.assertEqual(r.exception.issue_type, 'mapping_missing')


# ── Service: invalid_payload → exception ───────────────────────────

class InvalidPayloadTests(unittest.TestCase):

    def setUp(self):
        self.app = create_app(_TestConfig)
        self.ctx = self.app.app_context()
        self.ctx.push()
        db.create_all()
        self.ids = _seed()

    def tearDown(self):
        db.session.remove()
        db.drop_all()
        self.ctx.pop()

    def test_missing_external_ref_queued(self):
        r = ci_svc.import_reservation(
            connection=self.ids['conn'],
            payload=_payload(external_reservation_ref=''),
            actor_user_id=self.ids['admin_id'],
        )
        self.assertEqual(r.action, 'queued')
        self.assertEqual(r.exception.issue_type, 'invalid_payload')

    def test_bad_dates_queued(self):
        today = date.today()
        r = ci_svc.import_reservation(
            connection=self.ids['conn'],
            payload=_payload(
                check_in=(today + timedelta(days=10)).isoformat(),
                check_out=(today + timedelta(days=10)).isoformat()),
            actor_user_id=self.ids['admin_id'],
        )
        self.assertEqual(r.action, 'queued')
        self.assertEqual(r.exception.issue_type, 'invalid_payload')

    def test_missing_guest_name_queued(self):
        r = ci_svc.import_reservation(
            connection=self.ids['conn'],
            payload=_payload(guest_first_name='', guest_last_name=''),
            actor_user_id=self.ids['admin_id'],
        )
        self.assertEqual(r.action, 'queued')
        self.assertEqual(r.exception.issue_type, 'invalid_payload')


# ── Exception lifecycle ────────────────────────────────────────────

class ExceptionLifecycleTests(unittest.TestCase):

    def setUp(self):
        self.app = create_app(_TestConfig)
        self.ctx = self.app.app_context()
        self.ctx.push()
        db.create_all()
        self.ids = _seed()
        # Force a queued exception
        self.exc = ci_svc.import_reservation(
            connection=self.ids['conn'],
            payload=_payload(external_room_id='UNMAPPED-XYZ',
                             external_reservation_ref='BDC-LIFE-1'),
            actor_user_id=self.ids['admin_id'],
        ).exception

    def tearDown(self):
        db.session.remove()
        db.drop_all()
        self.ctx.pop()

    def test_new_to_reviewed(self):
        r = ci_svc.update_exception_status(
            exception=self.exc, new_status='reviewed',
            actor_user_id=self.ids['admin_id'],
        )
        self.assertTrue(r['ok'])
        self.assertEqual(self.exc.status, 'reviewed')

    def test_resolve_links_booking(self):
        # Make a manual booking we can link
        guest = Guest(first_name='Manual', last_name='Linker')
        db.session.add(guest); db.session.flush()
        b = Booking(booking_ref='MAN001',
                    room_id=self.ids['room_ids'][0], guest_id=guest.id,
                    check_in_date=date.today() + timedelta(days=14),
                    check_out_date=date.today() + timedelta(days=17),
                    num_guests=2, status='confirmed',
                    total_amount=1800.0,
                    source='direct', billing_target='guest',
                    created_by=self.ids['admin_id'])
        db.session.add(b); db.session.commit()

        r = ci_svc.update_exception_status(
            exception=self.exc, new_status='resolved',
            actor_user_id=self.ids['admin_id'],
            linked_booking_id=b.id,
            notes='Manually rebooked',
        )
        self.assertTrue(r['ok'])
        self.assertEqual(self.exc.status, 'resolved')
        self.assertEqual(self.exc.linked_booking_id, b.id)
        self.assertIsNotNone(self.exc.reviewed_at)

    def test_terminal_states_are_sticky(self):
        ci_svc.update_exception_status(
            exception=self.exc, new_status='ignored',
            actor_user_id=self.ids['admin_id'])
        bad = ci_svc.update_exception_status(
            exception=self.exc, new_status='reviewed',
            actor_user_id=self.ids['admin_id'])
        self.assertFalse(bad['ok'])

    def test_invalid_transition_rejected(self):
        # Reviewed cannot go back to 'new'
        ci_svc.update_exception_status(
            exception=self.exc, new_status='reviewed',
            actor_user_id=self.ids['admin_id'])
        bad = ci_svc.update_exception_status(
            exception=self.exc, new_status='new',
            actor_user_id=self.ids['admin_id'])
        self.assertFalse(bad['ok'])

    def test_summary_counts_shape(self):
        s = ci_svc.summary_counts()
        for k in ('total', 'open', 'new', 'reviewed', 'conflict'):
            self.assertIn(k, s)
        self.assertGreaterEqual(s['total'], 1)


# ── Routes / HTTP layer ────────────────────────────────────────────

class RouteTests(unittest.TestCase):

    def setUp(self):
        self.app = create_app(_TestConfig)
        self.ctx = self.app.app_context()
        self.ctx.push()
        db.create_all()
        self.ids = _seed()
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

    def test_admin_can_render_exception_queue(self):
        self._login('admin')
        r = self.client.get('/admin/channel-exceptions/')
        self.assertEqual(r.status_code, 200)
        self.assertIn(b'Channel import exceptions', r.data)

    def test_anonymous_redirected_off_queue(self):
        r = self.client.get('/admin/channel-exceptions/',
                            follow_redirects=False)
        self.assertIn(r.status_code, (302, 303))

    def test_staff_blocked_from_queue(self):
        self._login('staff')
        r = self.client.get('/admin/channel-exceptions/',
                            follow_redirects=False)
        self.assertIn(r.status_code, (302, 303))

    def test_sandbox_import_route_creates_booking(self):
        self._login('admin')
        today = date.today()
        r = self.client.post(
            f'/admin/channels/{self.ids["conn_id"]}/sandbox-import',
            data={
                'external_reservation_ref': 'BDC-ROUTE-OK',
                'external_room_id':         'BDC-DBL-01',
                'external_rate_plan_id':    'BDC-RP-FLEX',
                'check_in':                 (today + timedelta(days=14)).isoformat(),
                'check_out':                (today + timedelta(days=17)).isoformat(),
                'num_guests':               '2',
                'guest_first_name':         'Route',
                'guest_last_name':          'Tester',
                'total_amount':             '1800',
            },
            follow_redirects=False,
        )
        self.assertIn(r.status_code, (302, 303))
        b = Booking.query.filter_by(
            external_reservation_ref='BDC-ROUTE-OK').first()
        self.assertIsNotNone(b)
        # Redirect goes to bookings.detail
        self.assertIn('/bookings/', r.headers.get('Location', ''))

    def test_sandbox_import_route_redirects_queued_to_exception(self):
        self._login('admin')
        today = date.today()
        r = self.client.post(
            f'/admin/channels/{self.ids["conn_id"]}/sandbox-import',
            data={
                'external_reservation_ref': 'BDC-ROUTE-QUEUED',
                'external_room_id':         'UNMAPPED-XYZ',
                'check_in':                 (today + timedelta(days=14)).isoformat(),
                'check_out':                (today + timedelta(days=17)).isoformat(),
                'num_guests':               '2',
                'guest_first_name':         'Route',
                'guest_last_name':          'Queued',
            },
            follow_redirects=False,
        )
        self.assertIn(r.status_code, (302, 303))
        self.assertIn('/admin/channel-exceptions/',
                      r.headers.get('Location', ''))

    def test_admin_can_render_exception_detail(self):
        self._login('admin')
        # create one
        r0 = ci_svc.import_reservation(
            connection=self.ids['conn'],
            payload=_payload(external_room_id='UNMAPPED-XYZ',
                             external_reservation_ref='BDC-DETAIL'),
            actor_user_id=self.ids['admin_id'],
        )
        r = self.client.get(
            f'/admin/channel-exceptions/{r0.exception.id}')
        self.assertEqual(r.status_code, 200)
        self.assertIn(b'Suggested action', r.data)

    def test_status_route_resolves(self):
        self._login('admin')
        r0 = ci_svc.import_reservation(
            connection=self.ids['conn'],
            payload=_payload(external_room_id='UNMAPPED-XYZ',
                             external_reservation_ref='BDC-RESOLVED'),
            actor_user_id=self.ids['admin_id'],
        )
        r = self.client.post(
            f'/admin/channel-exceptions/{r0.exception.id}/status',
            data={'status': 'ignored', 'notes': 'spam'},
            follow_redirects=False,
        )
        self.assertIn(r.status_code, (302, 303))
        exc = ChannelImportException.query.get(r0.exception.id)
        self.assertEqual(exc.status, 'ignored')
        self.assertEqual(exc.notes, 'spam')


# ── Migration shape + isolation guard ──────────────────────────────

class MigrationTests(unittest.TestCase):

    def test_migration_file_exists(self):
        self.assertTrue(_MIGRATION_PATH.is_file(),
                        f'expected {_MIGRATION_PATH}')
        text = _MIGRATION_PATH.read_text()
        self.assertIn("revision      = 'c4f7d2a86b15'", text)
        self.assertIn("'channel_import_exceptions'", text)
        self.assertIn('channel_connections.id', text)
        self.assertIn('bookings.id', text)
        self.assertIn('users.id', text)


class IsolationTests(unittest.TestCase):
    """AST-based static guard: the import code path never reaches for
    requests/httpx/whatsapp/gemini. Docstrings can mention the words."""

    _BANNED = ('whatsapp', 'gemini', 'requests', 'urllib', 'httpx',
               'anthropic')

    def _read(self, rel_path):
        return (_REPO_ROOT / rel_path).read_text()

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

    def _assert_clean(self, rel_path):
        idents = self._idents(self._read(rel_path))
        for banned in self._BANNED:
            self.assertNotIn(
                banned, idents,
                f'unexpected identifier {banned!r} in {rel_path}',
            )

    def test_service_module_clean(self):
        self._assert_clean('app/services/channel_import.py')

    def test_routes_module_clean(self):
        self._assert_clean('app/routes/channel_exceptions.py')


if __name__ == '__main__':
    unittest.main()
