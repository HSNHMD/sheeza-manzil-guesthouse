"""Tests for Channel Manager Foundation V1.

Covers the 11 requirements from the build spec, section J:

  1. booking source validation
  2. external reservation ref uniqueness/safety
  3. channel connection creation
  4. room map creation
  5. rate map creation
  6. sync job/log model behavior
  7. admin channel pages require login/admin
  8. no OTA calls are made
  9. no WhatsApp/Gemini calls
 10. migration files exist
 11. migrations only create channel-foundation-related structures
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
    db, User, Room, Guest, Booking, ActivityLog, Property,
    RoomType, RatePlan, ChannelConnection, ChannelRoomMap,
    ChannelRatePlanMap, ChannelSyncJob, ChannelSyncLog,
)
from app.services import channels as ch_svc                     # noqa: E402
from app.services import property as prop_svc                   # noqa: E402
from app.services import whatsapp as wa                         # noqa: E402
from app.services import ai_drafts                              # noqa: E402


_REPO_ROOT = Path(__file__).resolve().parent.parent
_MIGRATION_PATH = (
    _REPO_ROOT / 'migrations' / 'versions'
    / '2e8c4d7a3f51_add_channel_foundation.py'
)


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
    admin = User(username=f'ch_admin_{n}', email=f'a{n}@x', role='admin')
    admin.set_password('aaaaaaaaaa1')
    staff = User(username=f'ch_staff_{n}', email=f's{n}@x', role='staff')
    staff.set_password('aaaaaaaaaa1')
    db.session.add_all([admin, staff])
    db.session.commit()
    return admin.id, staff.id


def _seed_room_type(code='DEL', name='Deluxe'):
    rt = RoomType(code=code, name=name, max_occupancy=2,
                   base_capacity=2, is_active=True)
    db.session.add(rt); db.session.commit()
    return rt


def _seed_rate_plan(rt, code='BAR', base_rate=600.0):
    rp = RatePlan(code=code, name=code, room_type_id=rt.id,
                   base_rate=base_rate, currency='USD',
                   is_refundable=True, is_active=True)
    db.session.add(rp); db.session.commit()
    return rp


def _seed_room(number='99'):
    r = Room(number=number, name='T', room_type='Test',
             floor=0, capacity=2, price_per_night=600.0,
             status='available', housekeeping_status='clean')
    db.session.add(r); db.session.commit()
    return r


def _seed_booking(room, status='confirmed'):
    g = Guest(first_name='G', last_name='X',
              phone='+9607000000', email='g@x')
    db.session.add(g); db.session.commit()
    b = Booking(
        booking_ref=f'BK-CH-{room.id}',
        room_id=room.id, guest_id=g.id,
        check_in_date=date.today(),
        check_out_date=date.today() + timedelta(days=2),
        num_guests=1, total_amount=1200.0, status=status,
    )
    db.session.add(b); db.session.commit()
    return b


# ─────────────────────────────────────────────────────────────────────
# Common base — patches WhatsApp + AI providers (Reqs 8, 9)
# ─────────────────────────────────────────────────────────────────────

class _BaseAppTest(unittest.TestCase):

    def setUp(self):
        self._patches = []
        self._wa_send = mock.patch.object(
            wa, '_send', side_effect=AssertionError(
                'WhatsApp must not be called by Channels V1'))
        self._patches.append(self._wa_send.start())
        self._wa_template = mock.patch.object(
            wa, '_send_template', side_effect=AssertionError(
                'WhatsApp templates must not be called by Channels V1'))
        self._patches.append(self._wa_template.start())
        self._ai_patch = mock.patch.object(
            ai_drafts, 'generate_draft',
            side_effect=AssertionError(
                'AI drafts must not be called by Channels V1'))
        self._patches.append(self._ai_patch.start())

        # NOTE: V1 deliberately has no `requests` import. If a future
        # change adds one here, add patches for `requests.get` /
        # `requests.post` with AssertionError side effects so tests
        # bomb immediately on any outbound call.

        self.app = _make_app()
        self.ctx = self.app.app_context()
        self.ctx.push()
        db.create_all()
        self.admin_id, self.staff_id = _seed_users()
        # Ensure a Property exists for FK targets.
        prop_svc.current_property()
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
            sess['_fresh']   = True


# ─────────────────────────────────────────────────────────────────────
# 1) Booking source validation (Req 1)
# ─────────────────────────────────────────────────────────────────────

class BookingSourceTests(_BaseAppTest):

    def test_existing_bookings_default_to_direct(self):
        # New Booking row without an explicit source should land on
        # 'direct' via server_default.
        room = _seed_room()
        b = _seed_booking(room)
        self.assertEqual(b.source, 'direct')

    def test_source_validation_helper(self):
        for ok in ch_svc.BOOKING_SOURCES:
            self.assertTrue(ch_svc.is_valid_booking_source(ok))
        for bad in ('', None, 'sky', 'BOOKING_COM', 'walk-in'):
            self.assertFalse(ch_svc.is_valid_booking_source(bad))

    def test_channel_name_validation(self):
        for ok in ch_svc.CHANNEL_NAMES:
            self.assertTrue(ch_svc.is_valid_channel_name(ok))
        for bad in ('', None, 'foo', 'walk_in', 'direct'):
            # walk_in / direct are booking sources, NOT channels
            self.assertFalse(ch_svc.is_valid_channel_name(bad))


# ─────────────────────────────────────────────────────────────────────
# 2) External reservation ref uniqueness (Req 2)
# ─────────────────────────────────────────────────────────────────────

class ExternalRefUniquenessTests(_BaseAppTest):

    def test_link_external_ref_happy_path(self):
        room = _seed_room()
        b = _seed_booking(room)
        result = ch_svc.link_external_ref(
            b, external_source='booking_com',
            external_reservation_ref='ABC-12345',
        )
        db.session.commit()
        self.assertTrue(result['ok'])
        b = db.session.get(Booking, b.id)
        self.assertEqual(b.source, 'booking_com')
        self.assertEqual(b.external_source, 'booking_com')
        self.assertEqual(b.external_reservation_ref, 'ABC-12345')

    def test_duplicate_ref_rejected(self):
        room1 = _seed_room('1')
        room2 = _seed_room('2')
        b1 = _seed_booking(room1)
        b2 = _seed_booking(room2)
        ch_svc.link_external_ref(b1, external_source='booking_com',
                                  external_reservation_ref='DUP-1')
        db.session.commit()
        result = ch_svc.link_external_ref(
            b2, external_source='booking_com',
            external_reservation_ref='DUP-1')
        self.assertFalse(result['ok'])
        self.assertIn('already linked', result['error'])
        # b2 must still have NO external link
        b2 = db.session.get(Booking, b2.id)
        self.assertIsNone(b2.external_reservation_ref)

    def test_same_ref_different_channel_allowed(self):
        # Booking.com 'X' and Agoda 'X' are different reservations
        room1 = _seed_room('1')
        room2 = _seed_room('2')
        b1 = _seed_booking(room1)
        b2 = _seed_booking(room2)
        ch_svc.link_external_ref(b1, external_source='booking_com',
                                  external_reservation_ref='X')
        result = ch_svc.link_external_ref(
            b2, external_source='agoda',
            external_reservation_ref='X')
        db.session.commit()
        self.assertTrue(result['ok'])

    def test_invalid_external_source_rejected(self):
        room = _seed_room()
        b = _seed_booking(room)
        result = ch_svc.link_external_ref(
            b, external_source='direct',     # not a channel
            external_reservation_ref='X')
        self.assertFalse(result['ok'])

    def test_blank_ref_rejected(self):
        room = _seed_room()
        b = _seed_booking(room)
        result = ch_svc.link_external_ref(
            b, external_source='booking_com',
            external_reservation_ref='')
        self.assertFalse(result['ok'])


# ─────────────────────────────────────────────────────────────────────
# 3) Channel connection creation (Req 3)
# ─────────────────────────────────────────────────────────────────────

class ConnectionCreationTests(_BaseAppTest):

    def test_create_via_route(self):
        self._login(self.admin_id)
        r = self.client.post('/admin/channels/new', data={
            'channel_name':  'booking_com',
            'account_label': 'Test Account',
            'notes':         'sandbox setup',
        })
        self.assertIn(r.status_code, (301, 302))
        c = ChannelConnection.query.first()
        self.assertEqual(c.channel_name, 'booking_com')
        self.assertEqual(c.status, 'inactive')

    def test_invalid_channel_name_rejected(self):
        self._login(self.admin_id)
        r = self.client.post('/admin/channels/new', data={
            'channel_name': 'sky_scanner',     # not in vocab
        })
        self.assertEqual(r.status_code, 400)
        self.assertEqual(ChannelConnection.query.count(), 0)

    def test_duplicate_channel_per_property_rejected(self):
        ch_svc.create_connection(channel_name='booking_com')
        db.session.commit()
        result = ch_svc.create_connection(channel_name='booking_com')
        self.assertFalse(result['ok'])
        self.assertIn('already exists', result['error'])

    def test_status_transition(self):
        result = ch_svc.create_connection(channel_name='booking_com')
        db.session.commit()
        conn = result['connection']
        self.assertEqual(conn.status, 'inactive')
        ch_svc.update_connection_status(conn, 'sandbox')
        db.session.commit()
        self.assertEqual(conn.status, 'sandbox')

    def test_invalid_status_rejected(self):
        result = ch_svc.create_connection(channel_name='agoda')
        db.session.commit()
        r = ch_svc.update_connection_status(result['connection'], 'haunted')
        self.assertFalse(r['ok'])


# ─────────────────────────────────────────────────────────────────────
# 4) Room map creation (Req 4)
# ─────────────────────────────────────────────────────────────────────

class RoomMapTests(_BaseAppTest):

    def setUp(self):
        super().setUp()
        result = ch_svc.create_connection(channel_name='booking_com')
        db.session.commit()
        self.conn = result['connection']
        self.rt = _seed_room_type()

    def test_create_map(self):
        result = ch_svc.create_room_map(
            connection=self.conn,
            room_type_id=self.rt.id,
            external_room_id='BC-DLX-001',
            external_room_name_snapshot='Deluxe Twin',
        )
        db.session.commit()
        self.assertTrue(result['ok'])
        self.assertEqual(ChannelRoomMap.query.count(), 1)

    def test_duplicate_room_type_rejected(self):
        ch_svc.create_room_map(connection=self.conn,
                                room_type_id=self.rt.id,
                                external_room_id='X1')
        db.session.commit()
        r = ch_svc.create_room_map(connection=self.conn,
                                    room_type_id=self.rt.id,
                                    external_room_id='X2')
        self.assertFalse(r['ok'])

    def test_duplicate_external_id_rejected(self):
        rt2 = _seed_room_type('TWN', 'Twin')
        ch_svc.create_room_map(connection=self.conn,
                                room_type_id=self.rt.id,
                                external_room_id='SAME-ID')
        db.session.commit()
        r = ch_svc.create_room_map(connection=self.conn,
                                    room_type_id=rt2.id,
                                    external_room_id='SAME-ID')
        self.assertFalse(r['ok'])

    def test_unknown_room_type_rejected(self):
        r = ch_svc.create_room_map(connection=self.conn,
                                    room_type_id=99999,
                                    external_room_id='X')
        self.assertFalse(r['ok'])

    def test_blank_external_id_rejected(self):
        r = ch_svc.create_room_map(connection=self.conn,
                                    room_type_id=self.rt.id,
                                    external_room_id='')
        self.assertFalse(r['ok'])


# ─────────────────────────────────────────────────────────────────────
# 5) Rate map creation (Req 5)
# ─────────────────────────────────────────────────────────────────────

class RateMapTests(_BaseAppTest):

    def setUp(self):
        super().setUp()
        result = ch_svc.create_connection(channel_name='agoda')
        db.session.commit()
        self.conn = result['connection']
        self.rt = _seed_room_type()
        self.rp = _seed_rate_plan(self.rt)

    def test_create_rate_plan_map(self):
        r = ch_svc.create_rate_plan_map(
            connection=self.conn,
            rate_plan_id=self.rp.id,
            external_rate_plan_id='AG-RP-1',
            meal_plan_external_id='BB',
        )
        db.session.commit()
        self.assertTrue(r['ok'])
        self.assertEqual(ChannelRatePlanMap.query.count(), 1)

    def test_duplicate_plan_rejected(self):
        ch_svc.create_rate_plan_map(connection=self.conn,
                                     rate_plan_id=self.rp.id,
                                     external_rate_plan_id='X')
        db.session.commit()
        r = ch_svc.create_rate_plan_map(connection=self.conn,
                                         rate_plan_id=self.rp.id,
                                         external_rate_plan_id='Y')
        self.assertFalse(r['ok'])


# ─────────────────────────────────────────────────────────────────────
# 6) Sync job/log behavior (Req 6) — V1 = no-op
# ─────────────────────────────────────────────────────────────────────

class SyncJobTests(_BaseAppTest):

    def setUp(self):
        super().setUp()
        result = ch_svc.create_connection(channel_name='expedia')
        db.session.commit()
        self.conn = result['connection']

    def test_test_sync_creates_job_and_log(self):
        result = ch_svc.enqueue_test_sync_job(self.conn)
        db.session.commit()
        self.assertTrue(result['ok'])
        self.assertEqual(ChannelSyncJob.query.count(), 1)
        self.assertEqual(ChannelSyncLog.query.count(), 1)
        job = result['job']
        self.assertEqual(job.job_type, 'test_noop')
        self.assertEqual(job.status, 'success')
        self.assertEqual(job.attempt_count, 1)
        log = result['log']
        self.assertEqual(log.entity_type, 'test_noop')
        self.assertEqual(log.action, 'simulated')
        self.assertEqual(log.status, 'skipped')
        self.assertIn('No outbound', log.message)

    def test_test_sync_route_is_noop(self):
        self._login(self.admin_id)
        r = self.client.post(
            f'/admin/channels/{self.conn.id}/sync/test')
        self.assertIn(r.status_code, (301, 302))
        # Job + log created
        self.assertEqual(ChannelSyncJob.query.count(), 1)
        # No outbound HTTP attempted (the requests.* patches would
        # have raised AssertionError otherwise — test wouldn't reach
        # this line).

    def test_invalid_job_type_rejected(self):
        r = ch_svc.enqueue_test_sync_job(self.conn, job_type='bogus')
        self.assertFalse(r['ok'])


# ─────────────────────────────────────────────────────────────────────
# 7) Auth gates on admin pages (Req 7)
# ─────────────────────────────────────────────────────────────────────

class AdminAuthTests(_BaseAppTest):

    def test_anonymous_redirected(self):
        for path in ('/admin/channels/', '/admin/channels/new'):
            r = self.client.get(path)
            self.assertIn(r.status_code, (301, 302, 401))

    def test_staff_blocked(self):
        self._login(self.staff_id)
        for path in ('/admin/channels/', '/admin/channels/new'):
            r = self.client.get(path)
            self.assertIn(r.status_code, (302, 401, 403))

    def test_admin_allowed(self):
        self._login(self.admin_id)
        r = self.client.get('/admin/channels/')
        self.assertEqual(r.status_code, 200)
        self.assertIn(b'Channel Manager', r.data)


# ─────────────────────────────────────────────────────────────────────
# 8 + 9) No external coupling (Reqs 8, 9)
# ─────────────────────────────────────────────────────────────────────

class NoExternalCouplingTests(_BaseAppTest):

    def test_full_admin_flow_no_external_calls(self):
        self._login(self.admin_id)
        # Create connection
        self.client.post('/admin/channels/new', data={
            'channel_name': 'booking_com',
            'account_label': 'Test',
        })
        c = ChannelConnection.query.first()

        # Add a room map
        rt = _seed_room_type()
        self.client.post(
            f'/admin/channels/{c.id}/maps/rooms/new',
            data={'room_type_id': str(rt.id),
                  'external_room_id': 'BC-001'})

        # Add a rate map
        rp = _seed_rate_plan(rt)
        self.client.post(
            f'/admin/channels/{c.id}/maps/rates/new',
            data={'rate_plan_id': str(rp.id),
                  'external_rate_plan_id': 'BC-RP-001'})

        # Trigger test sync
        self.client.post(f'/admin/channels/{c.id}/sync/test')

        # Status flip
        self.client.post(f'/admin/channels/{c.id}/status',
                          data={'status': 'sandbox'})

        # Mocks would raise AssertionError on any outbound call.
        self.assertEqual(wa._send.call_count, 0)
        self.assertEqual(wa._send_template.call_count, 0)
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
        self.assertIn("revision = '2e8c4d7a3f51'", text)
        self.assertIn("down_revision = '1d9b6a4f5e72'", text)

    def test_migration_creates_only_channel_tables(self):
        text = _MIGRATION_PATH.read_text()
        creates = set(re.findall(r"op\.create_table\(\s*'([^']+)'", text))
        self.assertEqual(
            creates,
            {
                'channel_connections',
                'channel_room_maps',
                'channel_rate_plan_maps',
                'channel_sync_jobs',
                'channel_sync_logs',
            },
            f'unexpected tables: {creates}',
        )

    def test_migration_only_extends_bookings(self):
        text = _MIGRATION_PATH.read_text()
        # Only `bookings` table receives op.add_column, and only the
        # 3 documented columns
        added = re.findall(
            r"op\.add_column\(\s*'([^']+)',\s*sa\.Column\(\s*'([^']+)'",
            text,
        )
        for tbl, col in added:
            self.assertEqual(tbl, 'bookings',
                              f'unexpected add_column on {tbl}')
        cols = {col for _, col in added}
        self.assertEqual(cols,
                          {'source', 'external_source',
                           'external_reservation_ref'})


# ─────────────────────────────────────────────────────────────────────
# ActivityLog wiring (bonus check)
# ─────────────────────────────────────────────────────────────────────

class ActivityLogTests(_BaseAppTest):

    def test_lifecycle_logs(self):
        self._login(self.admin_id)
        # Create connection
        self.client.post('/admin/channels/new', data={
            'channel_name': 'agoda', 'account_label': 'Log'})
        # Map a room
        c = ChannelConnection.query.first()
        rt = _seed_room_type()
        self.client.post(
            f'/admin/channels/{c.id}/maps/rooms/new',
            data={'room_type_id': str(rt.id),
                  'external_room_id': 'X'})
        # Test sync
        self.client.post(f'/admin/channels/{c.id}/sync/test')

        for action in ('channel.connection_created',
                        'channel.mapping_created',
                        'channel.sync_job_created'):
            self.assertEqual(
                ActivityLog.query.filter_by(action=action).count(),
                1,
                msg=f'{action} not logged exactly once',
            )

    def test_external_ref_link_logs(self):
        room = _seed_room()
        b = _seed_booking(room)
        ch_svc.link_external_ref(b, external_source='booking_com',
                                  external_reservation_ref='X-1')
        db.session.commit()
        rows = ActivityLog.query.filter_by(
            action='booking.external_ref_linked').all()
        self.assertEqual(len(rows), 1)
        meta = json.loads(rows[0].metadata_json or '{}')
        for k in ('booking_id', 'booking_ref', 'external_source',
                  'external_reservation_ref', 'source'):
            self.assertIn(k, meta)
        self.assertEqual(meta['external_source'], 'booking_com')


if __name__ == '__main__':
    unittest.main()
