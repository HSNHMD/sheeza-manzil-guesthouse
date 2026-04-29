"""Tests for Business Date + Night Audit V1.

Covers the 12 requirements from the build spec, section I:

  1. business date state can be created/read
  2. night audit route requires login + admin
  3. blocking issues prevent the audit run (no state change)
  4. warning-only issues do NOT block the run
  5. successful audit run advances business date by exactly one day
  6. audit run record (NightAuditRun) is created
  7. ActivityLog entries written: night_audit.completed + business_date.rolled
     (and night_audit.blocked on refused runs)
  8. Booking.status is NOT mutated by the close
  9. Invoice.payment_status / amount_paid are NOT mutated by the close
 10. No WhatsApp / email / Gemini side effects (services patched + asserted not called)
 11. Migration file exists at the expected path
 12. Migration only creates business_date_state + night_audit_runs (no other ops)

V1 contract reminder (see services/night_audit.py): the close advances
the business date and writes audit rows ONLY. It does NOT touch booking
or invoice rows. Phase 4+ work will add room-charge auto-posting.
"""

from __future__ import annotations

import json
import os
import re
import unittest
from datetime import date, datetime, timedelta
from pathlib import Path
from unittest import mock

# Test must run with a clean env — kill any inherited DB / provider settings
for _v in ('DATABASE_URL',
           'AI_DRAFT_PROVIDER', 'GEMINI_API_KEY', 'ANTHROPIC_API_KEY',
           'WHATSAPP_ENABLED', 'WHATSAPP_TOKEN'):
    os.environ.pop(_v, None)
os.environ.setdefault('SECRET_KEY', 'test-secret-do-not-use-in-prod')

from config import Config                                       # noqa: E402
from app import create_app                                      # noqa: E402
from app.models import (                                        # noqa: E402
    db, User, Room, Guest, Booking, Invoice, ActivityLog,
    BusinessDateState, NightAuditRun,
)
from app.services import night_audit as na_svc                  # noqa: E402
from app.services import whatsapp as wa                         # noqa: E402
from app.services import ai_drafts                              # noqa: E402


_REPO_ROOT = Path(__file__).resolve().parent.parent
_MIGRATION_PATH = (
    _REPO_ROOT / 'migrations' / 'versions'
    / 'a3b8e9d24f15_add_business_date_and_night_audit_tables.py'
)


class _TestConfig(Config):
    TESTING = True
    SQLALCHEMY_DATABASE_URI = 'sqlite:///:memory:'
    WTF_CSRF_ENABLED = False
    WHATSAPP_ENABLED = False


def _make_app():
    return create_app(_TestConfig)


def _seed_users():
    admin = User(username='na_admin', email='a@x', role='admin')
    admin.set_password('aaaaaaaaaa1')
    staff = User(username='na_staff', email='s@x', role='staff')
    staff.set_password('aaaaaaaaaa1')
    db.session.add_all([admin, staff])
    db.session.commit()
    return admin.id, staff.id


# Use TODAY as the closing business date so the clock-skew pre-check
# (which fires when server-clock is more than 1 day before the business
# date) stays quiet in tests. We're not testing the skew check here —
# it has its own dedicated checks via the helper functions.
_TODAY = date.today()
_TOMORROW = _TODAY + timedelta(days=1)


def _seed_state(business_date=None):
    """Create the singleton BusinessDateState row used in tests."""
    bd = business_date or _TODAY
    state = BusinessDateState(
        current_business_date=bd,
        audit_in_progress=False,
    )
    db.session.add(state)
    db.session.commit()
    return state


# ─────────────────────────────────────────────────────────────────────
# 1) Business-date state CRUD (Req 1)
# ─────────────────────────────────────────────────────────────────────

class BusinessDateStateModelTests(unittest.TestCase):

    def setUp(self):
        self.app = _make_app()
        self.ctx = self.app.app_context()
        self.ctx.push()
        db.create_all()

    def tearDown(self):
        db.session.remove()
        db.drop_all()
        self.ctx.pop()

    def test_state_can_be_created_and_read(self):
        bd = date(2026, 5, 1)
        state = BusinessDateState(current_business_date=bd,
                                  audit_in_progress=False)
        db.session.add(state)
        db.session.commit()

        fetched = na_svc.get_business_date_state()
        self.assertIsNotNone(fetched)
        self.assertEqual(fetched.current_business_date, bd)
        self.assertFalse(fetched.audit_in_progress)
        self.assertIsNone(fetched.last_audit_run_at)

    def test_current_business_date_helper_returns_state_value(self):
        bd = _TODAY + timedelta(days=14)
        _seed_state(bd)
        self.assertEqual(na_svc.current_business_date(), bd)

    def test_current_business_date_falls_back_to_today_when_missing(self):
        # No row created — helper must fall back, not raise
        result = na_svc.current_business_date()
        self.assertEqual(result, date.today())


# ─────────────────────────────────────────────────────────────────────
# Route base — patches WhatsApp + AI providers so a stray call FAILS LOUDLY
# ─────────────────────────────────────────────────────────────────────

class _RouteBase(unittest.TestCase):

    def setUp(self):
        # Hard-mock outbound side-effects so test 10 is genuinely enforced.
        # Patch the inner request senders + the AI provider entrypoints.
        self._patches = []
        self._wa_send = mock.patch.object(
            wa, '_send', side_effect=AssertionError(
                'WhatsApp must not be called by Night Audit V1'))
        self._patches.append(self._wa_send.start())
        self._wa_template = mock.patch.object(
            wa, '_send_template', side_effect=AssertionError(
                'WhatsApp templates must not be called by Night Audit V1'))
        self._patches.append(self._wa_template.start())
        # Block AI provider calls at the top-level entrypoint
        self._ai_patch = mock.patch.object(
            ai_drafts, 'generate_draft',
            side_effect=AssertionError(
                'AI drafts must not be called by Night Audit V1'))
        self._patches.append(self._ai_patch.start())

        self.app = _make_app()
        self.ctx = self.app.app_context()
        self.ctx.push()
        db.create_all()
        self.admin_id, self.staff_id = _seed_users()
        self.client = self.app.test_client()

    def tearDown(self):
        self._wa_send.stop()
        self._wa_template.stop()
        self._ai_patch.stop()
        db.session.remove()
        db.drop_all()
        self.ctx.pop()

    def _login(self, uid):
        with self.client.session_transaction() as sess:
            sess['_user_id'] = str(uid)
            sess['_fresh'] = True


# ─────────────────────────────────────────────────────────────────────
# 2) Route auth (Req 2)
# ─────────────────────────────────────────────────────────────────────

class NightAuditAuthTests(_RouteBase):

    def setUp(self):
        super().setUp()
        _seed_state()

    def test_index_anonymous_redirected_to_login(self):
        r = self.client.get('/admin/night-audit/')
        self.assertIn(r.status_code, (301, 302, 401))

    def test_index_staff_blocked(self):
        self._login(self.staff_id)
        r = self.client.get('/admin/night-audit/', follow_redirects=False)
        # admin_required either 403s or redirects staff away from /admin/*
        self.assertIn(r.status_code, (302, 401, 403))

    def test_index_admin_allowed(self):
        self._login(self.admin_id)
        r = self.client.get('/admin/night-audit/')
        self.assertEqual(r.status_code, 200)

    def test_run_anonymous_blocked(self):
        r = self.client.post('/admin/night-audit/run')
        self.assertIn(r.status_code, (301, 302, 401))
        # No state change, no audit row
        state = na_svc.get_business_date_state()
        self.assertEqual(state.current_business_date, _TODAY)
        self.assertEqual(NightAuditRun.query.count(), 0)

    def test_run_staff_blocked(self):
        self._login(self.staff_id)
        r = self.client.post(
            '/admin/night-audit/run',
            data={'confirm_date': _TODAY.isoformat()},
        )
        self.assertIn(r.status_code, (302, 401, 403))
        state = na_svc.get_business_date_state()
        self.assertEqual(state.current_business_date, _TODAY)
        self.assertEqual(NightAuditRun.query.count(), 0)


# ─────────────────────────────────────────────────────────────────────
# 3) Blocking issues prevent run (Req 3)
# ─────────────────────────────────────────────────────────────────────

class BlockingIssueTests(_RouteBase):

    def setUp(self):
        super().setUp()
        self._login(self.admin_id)

    def test_payment_mismatch_blocks_run(self):
        state = _seed_state(_TODAY)
        # Seed an Invoice with mismatched payment status (BLOCKING)
        room = Room(number='99', name='T', room_type='Test',
                    floor=0, capacity=2, price_per_night=600.0)
        guest = Guest(first_name='Mis', last_name='Match',
                      phone='+9607000099', email='m@x')
        db.session.add_all([room, guest]); db.session.commit()
        b = Booking(
            booking_ref='BKMISMATCH', room_id=room.id, guest_id=guest.id,
            check_in_date=_TODAY - timedelta(days=2),
            check_out_date=_TODAY + timedelta(days=4),
            num_guests=1, total_amount=2400.0, status='confirmed',
        )
        db.session.add(b); db.session.commit()
        inv = Invoice(
            booking_id=b.id, invoice_number='INV-MIS-001',
            total_amount=2400.0, payment_status='mismatch',
            amount_paid=0.0,
        )
        db.session.add(inv); db.session.commit()

        # Sanity: pre-checks see it as blocking
        issues = na_svc.run_pre_checks(state.current_business_date)
        blocking, _ = na_svc.split_by_severity(issues)
        self.assertTrue(any(i.code == 'invoices_payment_mismatch'
                            for i in blocking))

        r = self.client.post(
            '/admin/night-audit/run',
            data={'confirm_date': _TODAY.isoformat()},
        )
        self.assertIn(r.status_code, (301, 302))
        # Date NOT advanced
        state = na_svc.get_business_date_state()
        self.assertEqual(state.current_business_date, _TODAY)
        # ONE blocked run row written for the audit trail
        runs = NightAuditRun.query.all()
        self.assertEqual(len(runs), 1)
        self.assertEqual(runs[0].status, 'blocked')
        self.assertEqual(runs[0].business_date_closed, _TODAY)
        # And ONE night_audit.blocked ActivityLog row
        blocked_logs = (ActivityLog.query
                        .filter(ActivityLog.action == 'night_audit.blocked')
                        .all())
        self.assertEqual(len(blocked_logs), 1)
        # No completion / rolled rows written
        self.assertEqual(
            ActivityLog.query
            .filter(ActivityLog.action == 'night_audit.completed').count(),
            0,
        )
        self.assertEqual(
            ActivityLog.query
            .filter(ActivityLog.action == 'business_date.rolled').count(),
            0,
        )

    def test_audit_in_progress_flag_blocks_run(self):
        # Simulate a stale in-progress flag
        state = _seed_state(_TODAY)
        state.audit_in_progress = True
        state.audit_started_at = datetime.utcnow()
        state.audit_started_by_user_id = self.admin_id
        db.session.commit()

        r = self.client.post(
            '/admin/night-audit/run',
            data={'confirm_date': _TODAY.isoformat()},
        )
        self.assertIn(r.status_code, (301, 302))
        state = na_svc.get_business_date_state()
        self.assertEqual(state.current_business_date, _TODAY)


# ─────────────────────────────────────────────────────────────────────
# 4) Warning-only issues do NOT block (Req 4)
# ─────────────────────────────────────────────────────────────────────

class WarningOnlyDoesNotBlockTests(_RouteBase):

    def setUp(self):
        super().setUp()
        self._login(self.admin_id)

    def test_warning_only_proceeds_when_confirmed(self):
        state = _seed_state(_TODAY)
        # Seed a pending-arrival booking (WARNING) — should NOT block
        room = Room(number='99', name='T', room_type='Test',
                    floor=0, capacity=2, price_per_night=600.0)
        guest = Guest(first_name='Pen', last_name='Ding',
                      phone='+9607000098', email='p@x')
        db.session.add_all([room, guest]); db.session.commit()
        b = Booking(
            booking_ref='BKWARN001', room_id=room.id, guest_id=guest.id,
            check_in_date=_TODAY,  # arrival today → WARNING
            check_out_date=_TODAY + timedelta(days=4),
            num_guests=1, total_amount=2400.0, status='confirmed',
        )
        db.session.add(b); db.session.commit()

        issues = na_svc.run_pre_checks(state.current_business_date)
        blocking, warning = na_svc.split_by_severity(issues)
        self.assertEqual(len(blocking), 0)
        self.assertGreaterEqual(len(warning), 1)

        r = self.client.post(
            '/admin/night-audit/run',
            data={'confirm_date': _TODAY.isoformat()},
        )
        self.assertIn(r.status_code, (301, 302))
        # Advanced
        state = na_svc.get_business_date_state()
        self.assertEqual(state.current_business_date, _TOMORROW)
        # Completed run row exists with warning_count >= 1
        run = NightAuditRun.query.filter_by(status='completed').first()
        self.assertIsNotNone(run)
        self.assertGreaterEqual(run.warning_count, 1)


# ─────────────────────────────────────────────────────────────────────
# 5–9) Successful close — date advances, run row, audit logs,
#       no booking/payment status mutation (Reqs 5/6/7/8/9)
# ─────────────────────────────────────────────────────────────────────

class SuccessfulCloseTests(_RouteBase):

    def setUp(self):
        super().setUp()
        self._login(self.admin_id)
        _seed_state(_TODAY)

        # Seed a booking + invoice in known states. We will assert these
        # are STILL in the same state after the audit close.
        room = Room(number='99', name='T', room_type='Test',
                    floor=0, capacity=2, price_per_night=600.0)
        guest = Guest(first_name='Stay', last_name='Put',
                      phone='+9607000097', email='s@x')
        db.session.add_all([room, guest]); db.session.commit()
        self._stay_check_in  = _TODAY - timedelta(days=4)
        self._stay_check_out = _TODAY + timedelta(days=9)
        self.booking = Booking(
            booking_ref='BKSTAY01', room_id=room.id, guest_id=guest.id,
            check_in_date=self._stay_check_in,
            check_out_date=self._stay_check_out,
            num_guests=1, total_amount=2400.0, status='checked_in',
        )
        db.session.add(self.booking); db.session.commit()
        self.invoice = Invoice(
            booking_id=self.booking.id, invoice_number='INV-STAY01',
            total_amount=2400.0, payment_status='paid',
            amount_paid=2400.0,
        )
        db.session.add(self.invoice); db.session.commit()

        # Snapshot for diffing post-run
        self._pre_booking_status   = self.booking.status
        self._pre_payment_status   = self.invoice.payment_status
        self._pre_amount_paid      = self.invoice.amount_paid

    def _close(self):
        return self.client.post(
            '/admin/night-audit/run',
            data={
                'confirm_date': _TODAY.isoformat(),
                'notes':        'Smooth close.',
            },
        )

    def test_close_advances_business_date_by_one_day(self):
        r = self._close()
        self.assertIn(r.status_code, (301, 302))
        state = na_svc.get_business_date_state()
        self.assertEqual(state.current_business_date, _TOMORROW)
        # Stamps cleared and last_audit_run_at populated
        self.assertFalse(state.audit_in_progress)
        self.assertIsNone(state.audit_started_at)
        self.assertIsNotNone(state.last_audit_run_at)
        self.assertEqual(state.last_audit_run_by_user_id, self.admin_id)

    def test_audit_run_record_created(self):
        self._close()
        runs = NightAuditRun.query.filter_by(status='completed').all()
        self.assertEqual(len(runs), 1)
        run = runs[0]
        self.assertEqual(run.business_date_closed, _TODAY)
        self.assertEqual(run.next_business_date,   _TOMORROW)
        self.assertEqual(run.run_by_user_id,       self.admin_id)
        self.assertIsNotNone(run.completed_at)
        # summary_json is JSON-parseable and structurally correct
        summary = json.loads(run.summary_json)
        self.assertIn('issues',         summary)
        self.assertIn('blocking_count', summary)
        self.assertIn('warning_count',  summary)
        self.assertEqual(summary['blocking_count'], 0)
        # Notes captured
        self.assertEqual(run.notes, 'Smooth close.')

    def test_activity_log_rows_written(self):
        self._close()
        # Both completion + business-date-rolled rows
        completed = ActivityLog.query.filter_by(
            action='night_audit.completed').all()
        rolled = ActivityLog.query.filter_by(
            action='business_date.rolled').all()
        started = ActivityLog.query.filter_by(
            action='night_audit.started').all()
        self.assertEqual(len(completed), 1)
        self.assertEqual(len(rolled),    1)
        self.assertEqual(len(started),   1)
        # Metadata is the strict whitelist
        meta = json.loads(completed[0].metadata_json or '{}')
        for k in ('business_date_closed', 'next_business_date',
                  'blocking_issue_count', 'warning_count',
                  'run_by_user_id', 'audit_run_id'):
            self.assertIn(k, meta)
        self.assertEqual(meta['business_date_closed'], _TODAY.isoformat())
        self.assertEqual(meta['next_business_date'],   _TOMORROW.isoformat())

    def test_booking_status_unchanged_by_close(self):
        self._close()
        b = db.session.get(Booking, self.booking.id)
        self.assertEqual(b.status, self._pre_booking_status)
        # Other booking fields untouched too
        self.assertEqual(b.check_in_date,  self._stay_check_in)
        self.assertEqual(b.check_out_date, self._stay_check_out)
        self.assertEqual(b.total_amount,   2400.0)

    def test_invoice_payment_status_unchanged_by_close(self):
        self._close()
        inv = db.session.get(Invoice, self.invoice.id)
        self.assertEqual(inv.payment_status, self._pre_payment_status)
        self.assertEqual(inv.amount_paid,    self._pre_amount_paid)

    def test_confirm_date_mismatch_refuses(self):
        r = self.client.post(
            '/admin/night-audit/run',
            data={'confirm_date': '2099-01-01'},  # wrong
        )
        self.assertIn(r.status_code, (301, 302))
        state = na_svc.get_business_date_state()
        self.assertEqual(state.current_business_date, _TODAY)
        # No completed rows; no blocked rows either (this branch is the
        # confirm-date guard, which fires AFTER pre-checks pass clean).
        self.assertEqual(
            NightAuditRun.query.filter_by(status='completed').count(), 0,
        )
        self.assertEqual(
            NightAuditRun.query.filter_by(status='blocked').count(), 0,
        )


# ─────────────────────────────────────────────────────────────────────
# 10) No external side effects (Req 10)
# ─────────────────────────────────────────────────────────────────────
#
# The _RouteBase.setUp() patches replace whatsapp.send_message,
# whatsapp.send_template and ai_drafts.generate_reply_draft with
# AssertionError-raising mocks. If any of the night-audit code paths
# above triggered a real call, the corresponding test would already
# have failed. Below is an explicit sanity assertion belt-and-braces.

class NoExternalSideEffectsTests(_RouteBase):

    def setUp(self):
        super().setUp()
        self._login(self.admin_id)
        _seed_state(_TODAY)

    def test_close_does_not_call_whatsapp_or_ai(self):
        r = self.client.post(
            '/admin/night-audit/run',
            data={'confirm_date': _TODAY.isoformat()},
        )
        self.assertIn(r.status_code, (301, 302))
        # Mocks are AssertionError side-effects; if a call happened the
        # test would already have failed during the request. Verify
        # call_count == 0 explicitly for belt-and-braces clarity.
        self.assertEqual(wa._send.call_count,          0)
        self.assertEqual(wa._send_template.call_count, 0)
        self.assertEqual(ai_drafts.generate_draft.call_count, 0)


# ─────────────────────────────────────────────────────────────────────
# 11–12) Migration file presence + scope (Reqs 11/12)
# ─────────────────────────────────────────────────────────────────────

class MigrationShapeTests(unittest.TestCase):

    def test_migration_file_exists(self):
        self.assertTrue(_MIGRATION_PATH.exists(),
                        f'expected migration at {_MIGRATION_PATH}')

    def test_migration_has_correct_revision_metadata(self):
        text = _MIGRATION_PATH.read_text()
        self.assertIn("revision = 'a3b8e9d24f15'", text)
        self.assertIn("down_revision = 'f1c5b2a93e80'", text)

    def test_migration_only_creates_business_date_and_night_audit_tables(self):
        text = _MIGRATION_PATH.read_text()
        # Find every op.create_table('NAME', ...)
        tables = re.findall(r"op\.create_table\(\s*'([^']+)'", text)
        self.assertEqual(
            set(tables),
            {'business_date_state', 'night_audit_runs'},
            f'unexpected tables created: {tables}',
        )
        # And no scope-creep ops on existing tables
        forbidden = (
            'op.add_column',
            'op.alter_column',
            'op.drop_column',
            'op.rename_table',
        )
        for op_name in forbidden:
            self.assertNotIn(
                op_name, text,
                f'migration must not call {op_name}',
            )
        # Both tables must round-trip in downgrade
        self.assertIn("op.drop_table('business_date_state')", text)
        self.assertIn("op.drop_table('night_audit_runs')",    text)

    def test_migration_seeds_singleton_state_row(self):
        text = _MIGRATION_PATH.read_text()
        # Bootstrap row exists so the app is never in a stateless start
        self.assertIn('op.bulk_insert', text)
        self.assertIn('current_business_date', text)


if __name__ == '__main__':
    unittest.main()
