"""Tests for Group Bookings / Master Folios V1.

Covers the 11 requirements from the build spec, section I:

  1. group creation
  2. booking attachment to group
  3. booking removal from group
  4. booking detail shows group membership
  5. group summary page renders
  6. master folio logic
  7. no double-counting in simple totals
  8. ActivityLog created
  9. no WhatsApp / Gemini calls
 10. migration file exists
 11. migration only creates group-related structures
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
    BookingGroup,
)
from app.services import groups as svc                          # noqa: E402
from app.services import folio as folio_svc                     # noqa: E402
from app.services import whatsapp as wa                         # noqa: E402
from app.services import ai_drafts                              # noqa: E402


_REPO_ROOT = Path(__file__).resolve().parent.parent
_MIGRATION_PATH = (
    _REPO_ROOT / 'migrations' / 'versions'
    / 'f9a4b8d2c531_add_booking_groups.py'
)
_TODAY     = date.today()
_PLUS_2    = _TODAY + timedelta(days=2)
_PLUS_5    = _TODAY + timedelta(days=5)
_YESTERDAY = _TODAY - timedelta(days=1)


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
    admin = User(username=f'gp_admin_{n}', email=f'a{n}@x', role='admin')
    admin.set_password('aaaaaaaaaa1')
    staff = User(username=f'gp_staff_{n}', email=f's{n}@x', role='staff')
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


def _seed_booking(room, *, last_name='Wilson', first_name='Anna',
                  status='confirmed', total=1200.0,
                  ci=None, co=None):
    g = Guest(first_name=first_name, last_name=last_name,
              phone=f'+9607000{room.id:03d}', email=f'g{room.id}@x')
    db.session.add(g); db.session.commit()
    b = Booking(
        booking_ref=f'BK-{room.id}-{last_name}',
        room_id=room.id, guest_id=g.id,
        check_in_date=ci or _TODAY, check_out_date=co or _PLUS_2,
        num_guests=1, total_amount=total, status=status,
    )
    db.session.add(b); db.session.commit()
    return b


def _add_folio_charge(booking, *, item_type='restaurant', total=50.0):
    fi = FolioItem(
        booking_id=booking.id, guest_id=booking.guest_id,
        item_type=item_type, description='—',
        quantity=1.0, unit_price=total, amount=total,
        total_amount=total, status='open', source_module='manual',
    )
    db.session.add(fi); db.session.commit()
    return fi


# ─────────────────────────────────────────────────────────────────────
# Common base — patches WhatsApp + AI providers (Req 9)
# ─────────────────────────────────────────────────────────────────────

class _BaseAppTest(unittest.TestCase):

    def setUp(self):
        self._patches = []
        self._wa_send = mock.patch.object(
            wa, '_send', side_effect=AssertionError(
                'WhatsApp must not be called by Groups V1'))
        self._patches.append(self._wa_send.start())
        self._wa_template = mock.patch.object(
            wa, '_send_template', side_effect=AssertionError(
                'WhatsApp templates must not be called by Groups V1'))
        self._patches.append(self._wa_template.start())
        self._ai_patch = mock.patch.object(
            ai_drafts, 'generate_draft',
            side_effect=AssertionError(
                'AI drafts must not be called by Groups V1'))
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
# 1) Group creation (Req 1)
# ─────────────────────────────────────────────────────────────────────

class GroupCreationTests(_BaseAppTest):

    def setUp(self):
        super().setUp()
        self._login(self.admin_id)

    def test_create_via_route(self):
        r = self.client.post('/groups/new', data={
            'group_code': 'MARWED-2026',
            'group_name': 'Maria Wedding Party',
            'billing_mode': 'master',
        }, follow_redirects=False)
        self.assertIn(r.status_code, (301, 302))
        g = BookingGroup.query.first()
        self.assertIsNotNone(g)
        self.assertEqual(g.group_code, 'MARWED-2026')
        self.assertEqual(g.billing_mode, 'master')
        self.assertEqual(g.status, 'active')

    def test_invalid_code_rejected(self):
        # Empty, single char, special-only, contains disallowed punctuation.
        # NOTE: spaces are intentionally normalized to '-' by the service,
        # so 'AB CD' is valid by design (becomes 'AB-CD').
        for bad in ('', 'A', '!!!!!', 'AB!CD', 'A.B'):
            r = self.client.post('/groups/new', data={
                'group_code': bad, 'group_name': 'X',
            })
            self.assertEqual(r.status_code, 400, msg=f'code={bad!r}')
        self.assertEqual(BookingGroup.query.count(), 0)

    def test_duplicate_code_rejected(self):
        svc.create_group(group_code='G1', group_name='First')
        db.session.commit()
        r = self.client.post('/groups/new', data={
            'group_code': 'G1', 'group_name': 'Second',
        })
        self.assertEqual(r.status_code, 400)
        self.assertEqual(BookingGroup.query.count(), 1)

    def test_blank_name_rejected(self):
        r = self.client.post('/groups/new', data={
            'group_code': 'OK1', 'group_name': '   ',
        })
        self.assertEqual(r.status_code, 400)
        self.assertEqual(BookingGroup.query.count(), 0)


# ─────────────────────────────────────────────────────────────────────
# 2 + 3) Attachment / removal (Reqs 2, 3)
# ─────────────────────────────────────────────────────────────────────

class MembershipTests(_BaseAppTest):

    def setUp(self):
        super().setUp()
        self._login(self.admin_id)
        result = svc.create_group(group_code='WEDDING',
                                    group_name='W')
        db.session.commit()
        self.group = result['group']
        self.r1 = _seed_room('101')
        self.r2 = _seed_room('102')
        self.b1 = _seed_booking(self.r1, last_name='Aaron', total=1000)
        self.b2 = _seed_booking(self.r2, last_name='Brian', total=800)

    def test_attach_via_route(self):
        r = self.client.post(f'/groups/{self.group.id}/add-booking', data={
            'booking_id': str(self.b1.id),
            'billing_target': 'master',
        })
        self.assertIn(r.status_code, (301, 302))
        b = db.session.get(Booking, self.b1.id)
        self.assertEqual(b.booking_group_id, self.group.id)
        self.assertEqual(b.billing_target, 'master')

    def test_attach_default_target_individual(self):
        result = svc.attach_booking(self.group, self.b1)
        db.session.commit()
        self.assertTrue(result['ok'])
        self.assertEqual(self.b1.billing_target, 'individual')

    def test_cannot_attach_to_two_groups(self):
        svc.attach_booking(self.group, self.b1)
        db.session.commit()
        result2 = svc.create_group(group_code='OTHER', group_name='O')
        db.session.commit()
        result = svc.attach_booking(result2['group'], self.b1)
        self.assertFalse(result['ok'])
        self.assertIn('different group', result['error'])

    def test_detach(self):
        svc.attach_booking(self.group, self.b1)
        db.session.commit()
        r = self.client.post(f'/groups/{self.group.id}/remove-booking',
                              data={'booking_id': str(self.b1.id)})
        self.assertIn(r.status_code, (301, 302))
        b = db.session.get(Booking, self.b1.id)
        self.assertIsNone(b.booking_group_id)
        self.assertEqual(b.billing_target, 'individual')

    def test_cannot_detach_master(self):
        svc.attach_booking(self.group, self.b1)
        db.session.commit()
        svc.set_master_booking(self.group, self.b1)
        db.session.commit()
        result = svc.detach_booking(self.group, self.b1)
        self.assertFalse(result['ok'])
        self.assertIn('master', result['error'])

    def test_standalone_bookings_unaffected(self):
        # Pre-existing bookings keep working without being touched
        self.assertIsNone(self.b1.booking_group_id)
        self.assertEqual(self.b1.billing_target, 'individual')
        # Posting a folio item still works on a standalone booking
        _add_folio_charge(self.b1, total=70)
        self.assertEqual(folio_svc.folio_balance(self.b1), 1070.0
                          if False else 70.0)
        # Booking.total_amount is canonical room revenue and is
        # untouched by anything in this sprint
        self.assertEqual(self.b1.total_amount, 1000.0)


# ─────────────────────────────────────────────────────────────────────
# 4) Booking detail shows group badge (Req 4)
# ─────────────────────────────────────────────────────────────────────

class BookingDetailIntegrationTests(_BaseAppTest):

    def test_badge_visible_on_booking_detail(self):
        self._login(self.admin_id)
        result = svc.create_group(group_code='W1', group_name='Wedding')
        db.session.commit()
        room = _seed_room('101')
        booking = _seed_booking(room, last_name='X')
        svc.attach_booking(result['group'], booking,
                            billing_target='master')
        db.session.commit()
        r = self.client.get(f'/bookings/{booking.id}')
        self.assertEqual(r.status_code, 200)
        self.assertIn(b'W1', r.data)
        self.assertIn(b'master', r.data)


# ─────────────────────────────────────────────────────────────────────
# 5) Group summary page renders (Req 5)
# ─────────────────────────────────────────────────────────────────────

class GroupSummaryRouteTests(_BaseAppTest):

    def test_summary_renders_with_members(self):
        self._login(self.admin_id)
        result = svc.create_group(group_code='SUMTEST',
                                    group_name='Sum Test')
        db.session.commit()
        room = _seed_room('101')
        b = _seed_booking(room, last_name='Wilson', total=1500)
        svc.attach_booking(result['group'], b)
        db.session.commit()

        r = self.client.get(f'/groups/{result["group"].id}')
        self.assertEqual(r.status_code, 200)
        body = r.get_data(as_text=True)
        self.assertIn('Sum Test', body)
        self.assertIn('SUMTEST', body)
        self.assertIn(b.booking_ref, body)
        # Sum room revenue surfaces
        self.assertIn('1500.00', body)


# ─────────────────────────────────────────────────────────────────────
# 6) Master folio logic (Req 6)
# ─────────────────────────────────────────────────────────────────────

class MasterFolioTests(_BaseAppTest):

    def setUp(self):
        super().setUp()
        result = svc.create_group(group_code='M1', group_name='M')
        db.session.commit()
        self.group = result['group']
        self.r1 = _seed_room('201')
        self.r2 = _seed_room('202')
        self.master_bk = _seed_booking(self.r1, last_name='Org',
                                        total=2000)
        self.member_bk = _seed_booking(self.r2, last_name='Friend',
                                        total=1200)
        svc.attach_booking(self.group, self.master_bk,
                            billing_target='master')
        svc.attach_booking(self.group, self.member_bk,
                            billing_target='master')
        db.session.commit()

    def test_set_master_booking(self):
        result = svc.set_master_booking(self.group, self.master_bk)
        db.session.commit()
        self.assertTrue(result['ok'])
        g = db.session.get(BookingGroup, self.group.id)
        self.assertEqual(g.master_booking_id, self.master_bk.id)

    def test_master_must_be_member(self):
        # Create a non-member booking
        room = _seed_room('999')
        outsider = _seed_booking(room, last_name='Outsider')
        result = svc.set_master_booking(self.group, outsider)
        self.assertFalse(result['ok'])
        self.assertIn('member', result['error'])

    def test_clear_master(self):
        svc.set_master_booking(self.group, self.master_bk)
        db.session.commit()
        result = svc.set_master_booking(self.group, None)
        db.session.commit()
        self.assertTrue(result['ok'])
        g = db.session.get(BookingGroup, self.group.id)
        self.assertIsNone(g.master_booking_id)

    def test_summary_splits_balances_by_target(self):
        # Add folio charges to both bookings
        _add_folio_charge(self.master_bk, total=300)   # on master
        _add_folio_charge(self.member_bk, total=150)   # on member
        # master target on both → both contribute to sum_master_balance
        s = svc.group_summary(self.group)
        self.assertEqual(s['sum_master_balance'], 450.0)
        self.assertEqual(s['sum_individual_balance'], 0.0)

    def test_summary_with_mixed_targets(self):
        # Flip one booking back to individual target
        svc.set_billing_target(self.member_bk, 'individual')
        db.session.commit()
        _add_folio_charge(self.master_bk, total=300)
        _add_folio_charge(self.member_bk, total=150)
        s = svc.group_summary(self.group)
        self.assertEqual(s['sum_master_balance'], 300.0)
        self.assertEqual(s['sum_individual_balance'], 150.0)

    def test_set_billing_target_requires_group(self):
        # A booking that's not in any group cannot get target='master'
        room = _seed_room('555')
        standalone = _seed_booking(room, last_name='Solo')
        result = svc.set_billing_target(standalone, 'master')
        self.assertFalse(result['ok'])

    def test_set_billing_target_invalid_value(self):
        result = svc.set_billing_target(self.member_bk, 'company')
        self.assertFalse(result['ok'])


# ─────────────────────────────────────────────────────────────────────
# 7) No double-counting in totals (Req 7)
# ─────────────────────────────────────────────────────────────────────

class NoDoubleCountTests(_BaseAppTest):

    def test_charge_belongs_to_one_folio_only(self):
        result = svc.create_group(group_code='DC', group_name='DC')
        db.session.commit()
        r1 = _seed_room('301')
        r2 = _seed_room('302')
        b1 = _seed_booking(r1, last_name='A', total=500)
        b2 = _seed_booking(r2, last_name='B', total=500)
        svc.attach_booking(result['group'], b1, billing_target='master')
        svc.attach_booking(result['group'], b2, billing_target='master')
        svc.set_master_booking(result['group'], b1)
        db.session.commit()
        # Post a charge on b2's folio
        _add_folio_charge(b2, total=200)
        # The charge appears on b2 only. b1's folio is untouched.
        self.assertEqual(folio_svc.folio_balance(b2), 200.0)
        self.assertEqual(folio_svc.folio_balance(b1), 0.0)
        # Group summary: each balance counted exactly once
        s = svc.group_summary(result['group'])
        self.assertEqual(s['sum_outstanding'], 200.0)
        # Sum of individual balances is what we'd expect — no row
        # appears in two totals
        self.assertEqual(
            s['sum_individual_balance'] + s['sum_master_balance'],
            s['sum_outstanding'],
        )


# ─────────────────────────────────────────────────────────────────────
# 8) ActivityLog (Req 8)
# ─────────────────────────────────────────────────────────────────────

class ActivityLogTests(_BaseAppTest):

    def setUp(self):
        super().setUp()
        self._login(self.admin_id)

    def test_lifecycle_writes_audit_rows(self):
        # Create
        self.client.post('/groups/new', data={
            'group_code': 'LOG1', 'group_name': 'Log',
        })
        # Add a booking
        room = _seed_room('401')
        booking = _seed_booking(room, last_name='X')
        g = BookingGroup.query.first()
        self.client.post(f'/groups/{g.id}/add-booking', data={
            'booking_id': str(booking.id),
            'billing_target': 'master',
        })
        # Set master
        self.client.post(f'/groups/{g.id}/set-master', data={
            'booking_id': str(booking.id),
        })
        # Cancel (to test status transition)
        # Note: can't remove master, so test cancel of group instead
        self.client.post(f'/groups/{g.id}/cancel')

        for action, expected in [
            ('group.created',              1),
            ('group.booking_added',        1),
            ('group.master_folio_updated', 1),
            ('group.cancelled',            1),
        ]:
            self.assertEqual(
                ActivityLog.query.filter_by(action=action).count(),
                expected,
                msg=f'{action} count mismatch',
            )

    def test_metadata_keys_are_safe(self):
        result = svc.create_group(group_code='META', group_name='Meta')
        db.session.commit()
        rows = ActivityLog.query.filter_by(action='group.created').all()
        self.assertEqual(len(rows), 1)
        meta = json.loads(rows[0].metadata_json or '{}')
        self.assertEqual(meta['group_code'], 'META')
        self.assertEqual(meta['group_name'], 'Meta')
        # No PII / secrets in metadata — only the documented keys.
        for k in meta.keys():
            self.assertIn(k, {
                'group_id', 'group_code', 'group_name', 'billing_mode',
            })


# ─────────────────────────────────────────────────────────────────────
# 9) No external coupling (Req 9)
# ─────────────────────────────────────────────────────────────────────

class NoExternalCouplingTests(_BaseAppTest):

    def test_full_flow_no_external_calls(self):
        self._login(self.admin_id)
        # Full lifecycle — create + attach + set master + edit + cancel
        self.client.post('/groups/new', data={
            'group_code': 'NX', 'group_name': 'No External',
        })
        g = BookingGroup.query.first()
        room = _seed_room('501')
        booking = _seed_booking(room, last_name='X')
        self.client.post(f'/groups/{g.id}/add-booking', data={
            'booking_id': str(booking.id),
        })
        self.client.post(f'/groups/{g.id}/set-master', data={
            'booking_id': str(booking.id),
        })
        self.client.post(f'/groups/{g.id}/edit', data={
            'group_name': 'Renamed', 'billing_mode': 'master',
        })
        self.client.post(f'/groups/{g.id}/complete')
        self.client.post(f'/groups/{g.id}/reactivate')

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
        self.assertIn("revision = 'f9a4b8d2c531'", text)
        self.assertIn("down_revision = 'e8b3c4d7f421'", text)

    def test_migration_creates_only_group_table(self):
        text = _MIGRATION_PATH.read_text()
        creates = set(re.findall(r"op\.create_table\(\s*'([^']+)'", text))
        self.assertEqual(creates, {'booking_groups'},
                          f'unexpected tables: {creates}')

    def test_migration_only_extends_bookings(self):
        text = _MIGRATION_PATH.read_text()
        # Only `bookings` table receives op.add_column
        for m in re.finditer(r"op\.add_column\(\s*'([^']+)'", text):
            self.assertEqual(m.group(1), 'bookings')
        # Only the documented columns
        adds = set(re.findall(
            r"op\.add_column\(\s*'bookings',\s*sa\.Column\(\s*'([^']+)'", text))
        self.assertEqual(adds,
                          {'booking_group_id', 'billing_target'})


# ─────────────────────────────────────────────────────────────────────
# Auth gates
# ─────────────────────────────────────────────────────────────────────

class AuthTests(_BaseAppTest):

    def test_anonymous_redirected(self):
        for path in ('/groups/', '/groups/new', '/groups/1'):
            r = self.client.get(path)
            self.assertIn(r.status_code, (301, 302, 401, 404),
                          msg=f'unexpected for {path}: {r.status_code}')

    def test_staff_blocked(self):
        self._login(self.staff_id)
        r = self.client.get('/groups/')
        self.assertIn(r.status_code, (302, 401, 403))

    def test_admin_allowed(self):
        self._login(self.admin_id)
        r = self.client.get('/groups/')
        self.assertEqual(r.status_code, 200)


if __name__ == '__main__':
    unittest.main()
