"""Tests for Housekeeping V1.

Covers the 10 requirements from the build spec, section H:

  1. board route requires login (login required + staff-allowed)
  2. status update works
  3. invalid status rejected
  4. task assignment works
  5. ActivityLog created (status_changed / task_assigned / room_inspected)
  6. room rail / board reflects housekeeping state
  7. mobile-friendly route still works (route renders for phones)
  8. no WhatsApp / Gemini calls
  9. migration file exists
 10. migration only creates housekeeping-related table(s)/columns

Plus service-level unit tests + a confirm-vocabulary test.
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
    db, User, Room, Guest, Booking, ActivityLog,
    HousekeepingLog, BusinessDateState,
)
from app.services import housekeeping as hk_svc                 # noqa: E402
from app.services import whatsapp as wa                         # noqa: E402
from app.services import ai_drafts                              # noqa: E402


_REPO_ROOT = Path(__file__).resolve().parent.parent
_MIGRATION_PATH = (
    _REPO_ROOT / 'migrations' / 'versions'
    / 'b4c1f2d6e892_add_room_housekeeping_fields.py'
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
    admin = User(username=f'hk_admin_{n}', email=f'a{n}@x', role='admin')
    admin.set_password('aaaaaaaaaa1')
    staff = User(username=f'hk_staff_{n}', email=f's{n}@x', role='staff')
    staff.set_password('aaaaaaaaaa1')
    db.session.add_all([admin, staff])
    db.session.commit()
    return admin.id, staff.id


def _seed_room(number='99', floor=0, hk_status='clean', op_status='available'):
    room = Room(number=number, name='T', room_type='Test',
                floor=floor, capacity=2, price_per_night=600.0,
                status=op_status, housekeeping_status=hk_status)
    db.session.add(room); db.session.commit()
    return room


def _seed_business_date(d=None):
    state = BusinessDateState(
        current_business_date=d or date.today(),
        audit_in_progress=False,
    )
    db.session.add(state); db.session.commit()
    return state


# ─────────────────────────────────────────────────────────────────────
# 0) Vocabulary — keep the canonical set frozen
# ─────────────────────────────────────────────────────────────────────

class HKVocabularyTests(unittest.TestCase):

    def test_canonical_statuses(self):
        # If this list changes, so must the migration commentary,
        # the service, and templates. Force an explicit decision.
        self.assertEqual(
            hk_svc.HK_STATUSES,
            ('clean', 'dirty', 'in_progress', 'inspected', 'out_of_order'),
        )

    def test_is_valid_status_known_and_unknown(self):
        for ok in hk_svc.HK_STATUSES:
            self.assertTrue(hk_svc.is_valid_status(ok))
        for bad in ('CLEAN', 'broken', '', None, 'in progress', 'maintenance'):
            self.assertFalse(hk_svc.is_valid_status(bad))


# ─────────────────────────────────────────────────────────────────────
# Common base — patches WhatsApp + AI providers (Req 8)
# ─────────────────────────────────────────────────────────────────────

class _RouteBase(unittest.TestCase):

    def setUp(self):
        self._patches = []
        self._wa_send = mock.patch.object(
            wa, '_send', side_effect=AssertionError(
                'WhatsApp must not be called by Housekeeping V1'))
        self._patches.append(self._wa_send.start())
        self._wa_template = mock.patch.object(
            wa, '_send_template', side_effect=AssertionError(
                'WhatsApp templates must not be called by Housekeeping V1'))
        self._patches.append(self._wa_template.start())
        self._ai_patch = mock.patch.object(
            ai_drafts, 'generate_draft',
            side_effect=AssertionError(
                'AI drafts must not be called by Housekeeping V1'))
        self._patches.append(self._ai_patch.start())

        self.app = _make_app()
        self.ctx = self.app.app_context()
        self.ctx.push()
        db.create_all()
        self.admin_id, self.staff_id = _seed_users()
        _seed_business_date()
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
# 1) Auth — board route requires login (Req 1)
# ─────────────────────────────────────────────────────────────────────

class HousekeepingAuthTests(_RouteBase):

    def test_anonymous_redirected(self):
        r = self.client.get('/housekeeping/')
        self.assertIn(r.status_code, (301, 302, 401))

    def test_admin_allowed(self):
        self._login(self.admin_id)
        _seed_room()
        r = self.client.get('/housekeeping/')
        self.assertEqual(r.status_code, 200)

    def test_staff_allowed(self):
        # Housekeeping is a staff workflow → staff role IS permitted
        # via the staff_guard whitelist.
        self._login(self.staff_id)
        _seed_room()
        r = self.client.get('/housekeeping/')
        self.assertEqual(r.status_code, 200)


# ─────────────────────────────────────────────────────────────────────
# 2 + 3) Status update happy path + invalid (Reqs 2, 3)
# ─────────────────────────────────────────────────────────────────────

class StatusUpdateTests(_RouteBase):

    def setUp(self):
        super().setUp()
        self._login(self.staff_id)
        self.room = _seed_room(hk_status='dirty')

    def test_dirty_to_in_progress_updates(self):
        r = self.client.post(
            f'/housekeeping/update/{self.room.id}',
            data={'new_status': 'in_progress'},
        )
        self.assertIn(r.status_code, (301, 302))
        room = db.session.get(Room, self.room.id)
        self.assertEqual(room.housekeeping_status, 'in_progress')
        self.assertIsNotNone(room.housekeeping_updated_at)
        self.assertEqual(room.housekeeping_updated_by_user_id, self.staff_id)

    def test_in_progress_to_clean_updates(self):
        self.room.housekeeping_status = 'in_progress'
        db.session.commit()
        self.client.post(
            f'/housekeeping/update/{self.room.id}',
            data={'new_status': 'clean'},
        )
        self.assertEqual(
            db.session.get(Room, self.room.id).housekeeping_status,
            'clean',
        )

    def test_clean_to_inspected_updates(self):
        self.room.housekeeping_status = 'clean'
        db.session.commit()
        self.client.post(
            f'/housekeeping/update/{self.room.id}',
            data={'new_status': 'inspected'},
        )
        self.assertEqual(
            db.session.get(Room, self.room.id).housekeeping_status,
            'inspected',
        )

    def test_mark_out_of_order(self):
        self.client.post(
            f'/housekeeping/update/{self.room.id}',
            data={'new_status': 'out_of_order'},
        )
        self.assertEqual(
            db.session.get(Room, self.room.id).housekeeping_status,
            'out_of_order',
        )

    def test_invalid_status_rejected(self):
        r = self.client.post(
            f'/housekeeping/update/{self.room.id}',
            data={'new_status': 'sparkling'},
        )
        self.assertIn(r.status_code, (301, 302))
        # Status unchanged
        self.assertEqual(
            db.session.get(Room, self.room.id).housekeeping_status,
            'dirty',
        )
        # No status_changed audit row written
        rows = ActivityLog.query.filter_by(
            action='housekeeping.status_changed').all()
        self.assertEqual(len(rows), 0)

    def test_op_status_not_mutated_by_hk_update(self):
        # Front Office owns operational status. Housekeeping V1 must
        # NEVER touch it.
        self.room.status = 'occupied'
        db.session.commit()
        self.client.post(
            f'/housekeeping/update/{self.room.id}',
            data={'new_status': 'inspected'},
        )
        room = db.session.get(Room, self.room.id)
        self.assertEqual(room.status, 'occupied')   # unchanged
        self.assertEqual(room.housekeeping_status, 'inspected')


# ─────────────────────────────────────────────────────────────────────
# 4) Task assignment (Req 4)
# ─────────────────────────────────────────────────────────────────────

class AssignmentTests(_RouteBase):

    def setUp(self):
        super().setUp()
        self._login(self.admin_id)
        self.room = _seed_room()

    def test_assign_to_user(self):
        r = self.client.post(
            f'/housekeeping/assign/{self.room.id}',
            data={'assignee_user_id': str(self.staff_id)},
        )
        self.assertIn(r.status_code, (301, 302))
        room = db.session.get(Room, self.room.id)
        self.assertEqual(room.assigned_to_user_id, self.staff_id)
        self.assertIsNotNone(room.assigned_at)

    def test_clear_assignment(self):
        self.room.assigned_to_user_id = self.staff_id
        self.room.assigned_at = datetime.utcnow()
        db.session.commit()
        self.client.post(
            f'/housekeeping/assign/{self.room.id}',
            data={'assignee_user_id': '0'},
        )
        room = db.session.get(Room, self.room.id)
        self.assertIsNone(room.assigned_to_user_id)
        self.assertIsNone(room.assigned_at)


# ─────────────────────────────────────────────────────────────────────
# 5) ActivityLog (Req 5)
# ─────────────────────────────────────────────────────────────────────

class ActivityLogTests(_RouteBase):

    def setUp(self):
        super().setUp()
        self._login(self.admin_id)

    def test_status_changed_logged_with_safe_metadata(self):
        room = _seed_room(hk_status='dirty')
        self.client.post(
            f'/housekeeping/update/{room.id}',
            data={'new_status': 'clean'},
        )
        rows = ActivityLog.query.filter_by(
            action='housekeeping.status_changed').all()
        self.assertEqual(len(rows), 1)
        meta = json.loads(rows[0].metadata_json or '{}')
        for k in ('room_id', 'room_number', 'old_status', 'new_status'):
            self.assertIn(k, meta)
        self.assertEqual(meta['old_status'], 'dirty')
        self.assertEqual(meta['new_status'], 'clean')
        self.assertEqual(meta['room_number'], room.number)

    def test_task_assigned_logged(self):
        room = _seed_room()
        self.client.post(
            f'/housekeeping/assign/{room.id}',
            data={'assignee_user_id': str(self.staff_id)},
        )
        rows = ActivityLog.query.filter_by(
            action='housekeeping.task_assigned').all()
        self.assertEqual(len(rows), 1)
        meta = json.loads(rows[0].metadata_json or '{}')
        self.assertEqual(meta['assigned_user_id'], self.staff_id)
        self.assertEqual(meta['room_id'], room.id)

    def test_room_inspected_writes_two_rows(self):
        # marking a room as 'inspected' writes BOTH status_changed AND
        # the dedicated room_inspected audit row.
        room = _seed_room(hk_status='clean')
        self.client.post(
            f'/housekeeping/update/{room.id}',
            data={'new_status': 'inspected'},
        )
        changed = ActivityLog.query.filter_by(
            action='housekeeping.status_changed').count()
        inspected = ActivityLog.query.filter_by(
            action='housekeeping.room_inspected').count()
        self.assertEqual(changed, 1)
        self.assertEqual(inspected, 1)


# ─────────────────────────────────────────────────────────────────────
# 6) Room rail / board reflects housekeeping state (Req 6)
# ─────────────────────────────────────────────────────────────────────

class BoardIntegrationTests(_RouteBase):

    def setUp(self):
        super().setUp()
        self._login(self.admin_id)

    def test_housekeeping_state_visible_on_board_room_rail(self):
        room = _seed_room(hk_status='dirty', number='42')
        r = self.client.get('/board')
        self.assertEqual(r.status_code, 200)
        body = r.get_data(as_text=True)
        # The HK pill is rendered with "DIRTY" text in uppercase
        # plus an s-dirty class on the room rail.
        self.assertIn('s-dirty', body)
        self.assertIn('DIRTY', body)

    def test_housekeeping_board_renders_each_status_pill(self):
        for st in hk_svc.HK_STATUSES:
            _seed_room(hk_status=st, number=f'r-{st}')
        r = self.client.get('/housekeeping/')
        self.assertEqual(r.status_code, 200)
        body = r.get_data(as_text=True)
        # One status pill per room rendered
        for st in hk_svc.HK_STATUSES:
            self.assertIn(f's-{st}', body)


# ─────────────────────────────────────────────────────────────────────
# 7) Mobile-friendly rendering (Req 7)
# ─────────────────────────────────────────────────────────────────────

class MobileRenderingTests(_RouteBase):

    def setUp(self):
        super().setUp()
        self._login(self.staff_id)
        _seed_room()

    def test_phone_user_agent_renders(self):
        r = self.client.get(
            '/housekeeping/',
            headers={
                'User-Agent': (
                    'Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X) '
                    'AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.0 '
                    'Mobile/15E148 Safari/604.1'
                ),
            },
        )
        self.assertEqual(r.status_code, 200)
        body = r.get_data(as_text=True)
        # Check the mobile-first markup is present
        self.assertIn('hk-shell', body)
        self.assertIn('hk-tabs', body)
        # Status-change buttons exist with min-height styling
        self.assertIn('hk-btn', body)


# ─────────────────────────────────────────────────────────────────────
# 8) No external side effects (Req 8)
# ─────────────────────────────────────────────────────────────────────
#
# The _RouteBase patches force AssertionError on any call — every test
# above is implicitly testing this. Belt-and-braces explicit assertion:

class NoExternalSideEffectsTests(_RouteBase):

    def setUp(self):
        super().setUp()
        self._login(self.admin_id)

    def test_no_external_calls_on_status_change(self):
        room = _seed_room(hk_status='dirty')
        self.client.post(
            f'/housekeeping/update/{room.id}',
            data={'new_status': 'clean'},
        )
        self.assertEqual(wa._send.call_count, 0)
        self.assertEqual(wa._send_template.call_count, 0)
        self.assertEqual(ai_drafts.generate_draft.call_count, 0)

    def test_no_external_calls_on_assign(self):
        room = _seed_room()
        self.client.post(
            f'/housekeeping/assign/{room.id}',
            data={'assignee_user_id': str(self.staff_id)},
        )
        self.assertEqual(wa._send.call_count, 0)
        self.assertEqual(wa._send_template.call_count, 0)
        self.assertEqual(ai_drafts.generate_draft.call_count, 0)


# ─────────────────────────────────────────────────────────────────────
# 9 + 10) Migration shape (Reqs 9, 10)
# ─────────────────────────────────────────────────────────────────────

class MigrationShapeTests(unittest.TestCase):

    def test_migration_file_exists(self):
        self.assertTrue(_MIGRATION_PATH.exists(),
                        f'expected migration at {_MIGRATION_PATH}')

    def test_migration_has_correct_revision_metadata(self):
        text = _MIGRATION_PATH.read_text()
        self.assertIn("revision = 'b4c1f2d6e892'", text)
        self.assertIn("down_revision = 'a3b8e9d24f15'", text)

    def test_migration_only_touches_rooms_table(self):
        """Scope-tight: only the rooms table is altered. No new tables
        created, no other tables touched."""
        text = _MIGRATION_PATH.read_text()
        # Allowed ops touch only 'rooms'
        for m in re.finditer(r"op\.add_column\(\s*'([^']+)'", text):
            self.assertEqual(m.group(1), 'rooms')
        for m in re.finditer(r"op\.drop_column\(\s*'([^']+)'", text):
            self.assertEqual(m.group(1), 'rooms')
        # No new tables
        creates = re.findall(r"op\.create_table\(\s*'([^']+)'", text)
        self.assertEqual(creates, [],
                         f'migration must not create new tables: {creates}')
        # No drop_table
        self.assertNotIn('op.drop_table', text)

    def test_migration_adds_only_expected_columns(self):
        text = _MIGRATION_PATH.read_text()
        added = set(re.findall(
            r"op\.add_column\(\s*'rooms',\s*sa\.Column\(\s*'([^']+)'", text))
        self.assertEqual(
            added,
            {'assigned_to_user_id', 'assigned_at',
             'housekeeping_updated_at', 'housekeeping_updated_by_user_id'},
            f'unexpected columns added: {added}',
        )


# ─────────────────────────────────────────────────────────────────────
# Service-level units (used by route handlers)
# ─────────────────────────────────────────────────────────────────────

class ServiceUnitTests(_RouteBase):

    def setUp(self):
        super().setUp()
        self.room = _seed_room(hk_status='dirty')
        self.user = User.query.get(self.admin_id)

    def test_set_room_status_no_op_when_same(self):
        self.room.housekeeping_status = 'clean'
        db.session.commit()
        before_legacy = HousekeepingLog.query.count()
        result = hk_svc.set_room_status(self.room, 'clean', user=self.user)
        db.session.commit()
        # No legacy log written when state didn't actually change
        self.assertEqual(HousekeepingLog.query.count(), before_legacy)
        # But ActivityLog row IS written (operator may want a "re-confirm")
        self.assertGreaterEqual(
            ActivityLog.query.filter_by(
                action='housekeeping.status_changed').count(),
            1,
        )
        self.assertTrue(result['ok'])

    def test_set_room_status_invalid_returns_error(self):
        result = hk_svc.set_room_status(self.room, 'bogus', user=self.user)
        self.assertFalse(result['ok'])
        self.assertIn('invalid', (result['error'] or '').lower())
        # Unchanged
        self.assertEqual(self.room.housekeeping_status, 'dirty')


if __name__ == '__main__':
    unittest.main()
