"""Tests for the audit/activity log V1 system.

Covers:
  1. ActivityLog model can be instantiated.
  2. log_activity() creates an ActivityLog row using the current db.session.
  3. log_activity() does not commit by itself.
  4. metadata sanitizer removes secret-like keys.
  5. description is truncated to 500 chars.
  6. invalid actor_type is normalized safely to 'system'.
  7. helper failure does NOT break the caller.
  8. Activity route is gated by login + admin.
  9. Migration file exists and only creates activity_logs.

These tests use an in-memory SQLite DB so they neither touch the dev
guesthouse.db file nor any production data. WhatsApp / R2 / network
calls are NOT exercised — every call site that triggers the helper is
unit-tested via the helper itself.
"""

from __future__ import annotations

import os
import re
import unittest
from pathlib import Path

# Make sure the production-style DATABASE_URL is NOT inherited from the
# shell — we want a clean SQLite in-memory engine for tests.
os.environ.pop('DATABASE_URL', None)
os.environ.setdefault('SECRET_KEY', 'test-secret-do-not-use-in-prod')

from config import Config                       # noqa: E402
from app import create_app                      # noqa: E402
from app.models import db, ActivityLog, User    # noqa: E402
from app.services.audit import (                # noqa: E402
    log_activity,
    sanitize_metadata,
    _BANNED_KEY_SUBSTRINGS,
)


class _TestConfig(Config):
    """SQLite in-memory config — avoids the Postgres pool kwargs in
    production Config that would otherwise crash a SQLite engine."""
    TESTING = True
    SQLALCHEMY_DATABASE_URI = 'sqlite:///:memory:'
    SQLALCHEMY_ENGINE_OPTIONS = {
        'pool_pre_ping': True,
        'pool_recycle': 300,
    }
    WTF_CSRF_ENABLED = False
    WHATSAPP_ENABLED = False


def _make_app():
    return create_app(_TestConfig)


class ActivityLogModelTests(unittest.TestCase):
    """Test 1 — model instantiation."""

    def test_model_can_be_instantiated(self):
        log = ActivityLog(
            actor_type='admin',
            action='booking.created',
            description='Test event',
        )
        self.assertEqual(log.actor_type, 'admin')
        self.assertEqual(log.action, 'booking.created')
        self.assertEqual(log.description, 'Test event')
        self.assertIsNone(log.booking_id)
        self.assertIsNone(log.invoice_id)


class LogActivityTests(unittest.TestCase):
    """Tests 2, 3 — db.session usage + no auto-commit."""

    def setUp(self):
        self.app = _make_app()
        self.ctx = self.app.app_context()
        self.ctx.push()
        db.create_all()

    def tearDown(self):
        db.session.remove()
        db.drop_all()
        self.ctx.pop()

    def test_log_activity_creates_row_in_session(self):
        with self.app.test_request_context('/'):
            row = log_activity(
                'booking.created',
                actor_type='guest',
                description='Guest submitted booking',
                metadata={'booking_ref': 'BKTEST01'},
            )
        self.assertIsNotNone(row)
        # The row is in session but NOT yet flushed/committed.
        self.assertIn(row, db.session.new)

    def test_log_activity_does_not_commit(self):
        # Sanity check: nothing in the table BEFORE we add anything.
        self.assertEqual(ActivityLog.query.count(), 0)
        with self.app.test_request_context('/'):
            log_activity('test.event', actor_type='system',
                         description='no-commit check')
        # If the helper committed, the row would be visible to a fresh query.
        # We rolled back instead — should be zero.
        db.session.rollback()
        self.assertEqual(ActivityLog.query.count(), 0)

    def test_log_activity_flushes_to_db_when_caller_commits(self):
        with self.app.test_request_context('/'):
            log_activity('test.event', actor_type='system',
                         description='caller commits')
        db.session.commit()
        self.assertEqual(ActivityLog.query.count(), 1)
        row = ActivityLog.query.first()
        self.assertEqual(row.action, 'test.event')
        self.assertEqual(row.actor_type, 'system')


class MetadataSanitizerTests(unittest.TestCase):
    """Test 4 — banned-key removal + value coercion."""

    def test_removes_banned_keys(self):
        meta = {
            'booking_ref': 'BKABC',
            'api_key': 'sk-leak',
            'PASSWORD': 'leak',
            'access_token': 'leak',
            'aws_secret_access_key': 'leak',
            'CREDENTIAL_FILE': 'leak',
            'private_key': 'leak',
        }
        cleaned = sanitize_metadata(meta)
        self.assertIsNotNone(cleaned)
        self.assertIn('booking_ref', cleaned)
        for k in ('api_key', 'PASSWORD', 'access_token',
                  'aws_secret_access_key', 'CREDENTIAL_FILE', 'private_key'):
            self.assertNotIn(k, cleaned, f'banned key {k!r} should be stripped')

    def test_keeps_only_scalars(self):
        cleaned = sanitize_metadata({
            'amount': 600,
            'rate': 1.5,
            'paid': True,
            'note': 'ok',
            'list_field': [1, 2, 3],
            'dict_field': {'nested': 'value'},
        })
        self.assertEqual(cleaned['amount'], 600)
        self.assertTrue(cleaned['paid'])
        self.assertEqual(cleaned['note'], 'ok')
        self.assertEqual(cleaned['list_field'], '<dropped>')
        self.assertEqual(cleaned['dict_field'], '<dropped>')

    def test_truncates_long_string_values(self):
        long_value = 'x' * 500
        cleaned = sanitize_metadata({'note': long_value})
        self.assertLessEqual(len(cleaned['note']), 201)  # 200 + '…'
        self.assertTrue(cleaned['note'].endswith('…'))

    def test_empty_input_returns_none(self):
        self.assertIsNone(sanitize_metadata(None))
        self.assertIsNone(sanitize_metadata({}))

    def test_banned_substring_list_is_complete(self):
        # Sanity: ensure all the substrings we promise in the docstring are
        # actually covered.
        for required in ('password', 'token', 'secret',
                         'api_key', 'key', 'credential'):
            self.assertIn(required, _BANNED_KEY_SUBSTRINGS,
                          f'{required!r} should be in banned list')


class DescriptionTruncationTests(unittest.TestCase):
    """Test 5 — description truncated to 500 chars."""

    def setUp(self):
        self.app = _make_app()
        self.ctx = self.app.app_context()
        self.ctx.push()
        db.create_all()

    def tearDown(self):
        db.session.remove()
        db.drop_all()
        self.ctx.pop()

    def test_description_truncated_to_500(self):
        big = 'A' * 1000
        with self.app.test_request_context('/'):
            row = log_activity('test.event', actor_type='system',
                               description=big)
        self.assertIsNotNone(row)
        self.assertEqual(len(row.description), 500)


class ActorNormalizationTests(unittest.TestCase):
    """Test 6 — invalid actor_type falls back to 'system'."""

    def setUp(self):
        self.app = _make_app()
        self.ctx = self.app.app_context()
        self.ctx.push()
        db.create_all()

    def tearDown(self):
        db.session.remove()
        db.drop_all()
        self.ctx.pop()

    def test_invalid_actor_type_normalized_to_system(self):
        with self.app.test_request_context('/'):
            row = log_activity('test.event', actor_type='hacker',
                               description='bogus actor')
        self.assertIsNotNone(row)
        self.assertEqual(row.actor_type, 'system')


class HelperFailureSafetyTests(unittest.TestCase):
    """Test 7 — helper failures must NOT propagate.

    We patch `db.session.add` to raise, simulating a catastrophic ORM
    failure mid-helper. The helper should catch, log a warning, and
    return None — the caller must not see an exception.
    """

    def setUp(self):
        self.app = _make_app()
        self.ctx = self.app.app_context()
        self.ctx.push()
        db.create_all()

    def tearDown(self):
        db.session.remove()
        db.drop_all()
        self.ctx.pop()

    def test_failure_inside_helper_returns_none(self):
        original_add = db.session.add

        def boom(_obj):
            raise RuntimeError('simulated session failure')

        db.session.add = boom
        try:
            with self.app.test_request_context('/'):
                # MUST NOT raise
                result = log_activity('test.event', actor_type='admin',
                                      description='will fail')
        finally:
            db.session.add = original_add

        self.assertIsNone(result)


class ActivityRouteTests(unittest.TestCase):
    """Test 8 — /admin/activity requires admin."""

    def setUp(self):
        self.app = _make_app()
        self.ctx = self.app.app_context()
        self.ctx.push()
        db.create_all()
        # Create one admin and one staff user
        admin = User(username='admin1', email='a@x', role='admin')
        admin.set_password('a-very-strong-password-1!')
        staff = User(username='staff1', email='s@x', role='staff')
        staff.set_password('a-very-strong-password-1!')
        db.session.add_all([admin, staff])
        db.session.commit()
        self.admin_id = admin.id
        self.staff_id = staff.id
        self.client = self.app.test_client()

    def tearDown(self):
        db.session.remove()
        db.drop_all()
        self.ctx.pop()

    def test_anonymous_redirected_to_login(self):
        resp = self.client.get('/admin/activity/', follow_redirects=False)
        self.assertIn(resp.status_code, (301, 302))
        # The app's `login_view` is auth.console_login, which mounts at
        # /console. Accept that, or any path containing 'login'.
        location = resp.headers.get('Location', '').lower()
        self.assertTrue(
            'login' in location or '/console' in location,
            f'unexpected redirect target: {location!r}',
        )

    def test_staff_gets_403(self):
        with self.client.session_transaction() as sess:
            sess['_user_id'] = str(self.staff_id)
            sess['_fresh'] = True
        resp = self.client.get('/admin/activity/', follow_redirects=False)
        # Staff guard might intercept before admin_required: either is fine
        # as long as the page is NOT 200.
        self.assertNotEqual(resp.status_code, 200)

    def test_admin_can_load(self):
        with self.client.session_transaction() as sess:
            sess['_user_id'] = str(self.admin_id)
            sess['_fresh'] = True
        resp = self.client.get('/admin/activity/')
        self.assertEqual(resp.status_code, 200)
        self.assertIn(b'Activity Log', resp.data)


class MigrationFileTests(unittest.TestCase):
    """Test 9 — migration file exists, is correctly chained, and only
    creates activity_logs (does not alter existing tables)."""

    @classmethod
    def setUpClass(cls):
        cls.repo = Path(__file__).resolve().parent.parent
        cls.path = (
            cls.repo / 'migrations' / 'versions'
            / 'f3a7c91b04e2_add_activity_log_table.py'
        )

    def test_file_exists(self):
        self.assertTrue(self.path.exists(),
                        f'migration file missing at {self.path}')

    def test_chained_to_correct_head(self):
        text = self.path.read_text()
        self.assertIn("revision = 'f3a7c91b04e2'", text)
        self.assertIn("down_revision = 'e4f7a2b1c8d3'", text)

    def test_only_creates_activity_logs(self):
        text = self.path.read_text()
        # Allowed DDL operations
        self.assertIn("op.create_table(", text)
        self.assertIn("'activity_logs'", text)
        # Forbidden operations: must not touch existing tables
        for forbidden in ('op.alter_column', 'op.add_column',
                          'op.drop_column', 'op.rename_table'):
            self.assertNotIn(forbidden, text,
                             f'{forbidden} should not appear in migration')
        # All op.create_table / op.create_index references must be on
        # 'activity_logs'
        for match in re.finditer(r"op\.create_(?:table|index)\([^)]*", text):
            snippet = match.group(0)
            self.assertIn("activity_logs", snippet,
                          f'unexpected target: {snippet}')

    def test_downgrade_drops_only_activity_logs(self):
        text = self.path.read_text()
        # Find the downgrade() body
        m = re.search(r'def downgrade\(\):(.*)', text, re.DOTALL)
        self.assertIsNotNone(m)
        body = m.group(1)
        self.assertIn("op.drop_table('activity_logs')", body)
        # Must not drop or alter anything else
        for forbidden in ('rooms', 'bookings', 'invoices', 'users',
                          'expenses', 'guests', 'housekeeping_logs'):
            # Allow the strings to appear in comments / FK names ONLY by
            # checking they are not the target of a drop_table call.
            self.assertNotIn(f"drop_table('{forbidden}')", body)


class WiringSmokeTests(unittest.TestCase):
    """Test 10 (bonus) — confirm log_activity is imported by the routes
    we promised to wire (sanity check, not a behavior test).
    """

    @classmethod
    def setUpClass(cls):
        cls.repo = Path(__file__).resolve().parent.parent

    def _read(self, relpath):
        return (self.repo / relpath).read_text()

    def test_bookings_route_imports_log_activity(self):
        self.assertIn('from ..services.audit import log_activity',
                      self._read('app/routes/bookings.py'))

    def test_public_route_imports_log_activity(self):
        self.assertIn('from ..services.audit import log_activity',
                      self._read('app/routes/public.py'))

    def test_invoices_route_imports_log_activity(self):
        self.assertIn('from ..services.audit import log_activity',
                      self._read('app/routes/invoices.py'))

    def test_staff_route_imports_log_activity(self):
        self.assertIn('from ..services.audit import log_activity',
                      self._read('app/routes/staff.py'))


if __name__ == '__main__':
    unittest.main()
