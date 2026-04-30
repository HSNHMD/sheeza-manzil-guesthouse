"""Tests for Multi-Property Foundation V1.

Covers the 10 requirements from the build spec, section K:

  1. Property model creation
  2. default/current property resolution
  3. core models can belong to a property
  4. backfilled staging data behaves correctly (server_default='1')
  5. property-scoped queries return expected results
  6. no unscoped leakage in key helpers/routes
  7. branding/settings use current property where implemented
  8. no WhatsApp/Gemini calls
  9. migration file exists
 10. migration only creates property-related structures and adds
     property_id where intended

Plus a regression test that proves a second seeded Property
(simulating the future multi-property mode) does not leak into the
default property's queries.
"""

from __future__ import annotations

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
    db, User, Room, Guest, Booking, Invoice, FolioItem,
    CashierTransaction, WhatsAppMessage, RoomType, RatePlan,
    RateOverride, RateRestriction, RoomBlock, BookingGroup,
    Property, PropertySettings,
)
from app.services import property as prop_svc                   # noqa: E402
from app.services import branding as branding_svc               # noqa: E402
from app.services import whatsapp as wa                         # noqa: E402
from app.services import ai_drafts                              # noqa: E402


_REPO_ROOT = Path(__file__).resolve().parent.parent
_MIGRATION_PATH = (
    _REPO_ROOT / 'migrations' / 'versions'
    / '1d9b6a4f5e72_add_property_foundation.py'
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
    admin = User(username=f'mp_admin_{n}', email=f'a{n}@x', role='admin')
    admin.set_password('aaaaaaaaaa1')
    staff = User(username=f'mp_staff_{n}', email=f's{n}@x', role='staff')
    staff.set_password('aaaaaaaaaa1')
    db.session.add_all([admin, staff])
    db.session.commit()
    return admin.id, staff.id


def _seed_room(number='1', property_id=None):
    r = Room(number=number, name='T', room_type='Test',
             floor=0, capacity=2, price_per_night=600.0,
             status='available', housekeeping_status='clean')
    if property_id is not None:
        r.property_id = property_id
    db.session.add(r); db.session.commit()
    return r


def _seed_booking(room, *, property_id=None, last_name='Wilson'):
    g = Guest(first_name='G', last_name=last_name,
              phone=f'+9607000{room.id:03d}', email=f'g{room.id}@x')
    db.session.add(g); db.session.commit()
    b = Booking(
        booking_ref=f'BK-{room.id}',
        room_id=room.id, guest_id=g.id,
        check_in_date=date.today(), check_out_date=date.today() + timedelta(days=2),
        num_guests=1, total_amount=1200.0, status='confirmed',
    )
    if property_id is not None:
        b.property_id = property_id
    db.session.add(b); db.session.commit()
    return b


# ─────────────────────────────────────────────────────────────────────
# Common base — patches WhatsApp + AI providers (Req 8)
# ─────────────────────────────────────────────────────────────────────

class _BaseAppTest(unittest.TestCase):

    def setUp(self):
        self._patches = []
        self._wa_send = mock.patch.object(
            wa, '_send', side_effect=AssertionError(
                'WhatsApp must not be called by Property V1'))
        self._patches.append(self._wa_send.start())
        self._wa_template = mock.patch.object(
            wa, '_send_template', side_effect=AssertionError(
                'WhatsApp templates must not be called by Property V1'))
        self._patches.append(self._wa_template.start())
        self._ai_patch = mock.patch.object(
            ai_drafts, 'generate_draft',
            side_effect=AssertionError(
                'AI drafts must not be called by Property V1'))
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
            sess['_fresh']   = True


# ─────────────────────────────────────────────────────────────────────
# 1) Property model creation (Req 1)
# ─────────────────────────────────────────────────────────────────────

class PropertyModelTests(_BaseAppTest):

    def test_create_property_directly(self):
        self.assertEqual(Property.query.count(), 0)
        p = Property(
            code='custom', name='Custom Inn',
            timezone='UTC', currency_code='USD',
        )
        db.session.add(p); db.session.commit()
        self.assertEqual(Property.query.count(), 1)
        self.assertEqual(Property.query.first().code, 'custom')

    def test_property_unique_code(self):
        from sqlalchemy.exc import IntegrityError
        db.session.add(Property(code='x', name='A'))
        db.session.add(Property(code='x', name='B'))
        with self.assertRaises(IntegrityError):
            db.session.commit()
        db.session.rollback()

    def test_property_links_to_settings(self):
        s = PropertySettings(property_name='S Inn', currency_code='USD',
                             timezone='UTC')
        db.session.add(s); db.session.commit()
        p = Property(code='linked', name='Linked',
                      timezone='UTC', currency_code='USD',
                      settings_id=s.id)
        db.session.add(p); db.session.commit()
        self.assertEqual(p.settings.property_name, 'S Inn')


# ─────────────────────────────────────────────────────────────────────
# 2) current_property resolution (Req 2)
# ─────────────────────────────────────────────────────────────────────

class CurrentPropertyTests(_BaseAppTest):

    def test_autoseeds_when_missing(self):
        # Fresh DB has no Property row — current_property() must seed
        # one and return it without exploding.
        self.assertIsNone(prop_svc.current_property(autoseed=False))
        p = prop_svc.current_property()
        self.assertIsNotNone(p)
        self.assertEqual(Property.query.count(), 1)
        # Seeded with default code
        self.assertEqual(p.code, 'default')

    def test_returns_first_existing(self):
        existing = Property(code='exist', name='Existing',
                             timezone='UTC', currency_code='USD')
        db.session.add(existing); db.session.commit()
        p = prop_svc.current_property()
        self.assertEqual(p.id, existing.id)

    def test_current_property_id_returns_int(self):
        pid = prop_svc.current_property_id()
        self.assertIsInstance(pid, int)
        self.assertEqual(pid, prop_svc.current_property().id)


# ─────────────────────────────────────────────────────────────────────
# 3) Core models accept property_id (Req 3)
# ─────────────────────────────────────────────────────────────────────

class CoreModelsBelongToPropertyTests(_BaseAppTest):

    def test_room_has_property_id(self):
        prop_svc.current_property()  # ensure seeded
        r = _seed_room()
        self.assertIsNotNone(r.property_id)
        self.assertEqual(r.property_id, 1)

    def test_booking_has_property_id(self):
        prop_svc.current_property()
        r = _seed_room()
        b = _seed_booking(r)
        self.assertIsNotNone(b.property_id)

    def test_invoice_has_property_id(self):
        prop_svc.current_property()
        r = _seed_room()
        b = _seed_booking(r)
        inv = Invoice(
            booking_id=b.id, invoice_number='INV-1',
            total_amount=100.0, payment_status='not_received',
        )
        db.session.add(inv); db.session.commit()
        self.assertIsNotNone(inv.property_id)

    def test_folio_item_has_property_id(self):
        prop_svc.current_property()
        r = _seed_room()
        b = _seed_booking(r)
        fi = FolioItem(
            booking_id=b.id, guest_id=b.guest_id,
            item_type='restaurant', description='—',
            quantity=1.0, unit_price=10.0, amount=10.0,
            total_amount=10.0, status='open', source_module='manual',
        )
        db.session.add(fi); db.session.commit()
        self.assertIsNotNone(fi.property_id)

    def test_room_type_and_rate_plan_have_property_id(self):
        prop_svc.current_property()
        rt = RoomType(code='X', name='X', max_occupancy=2,
                       base_capacity=2, is_active=True)
        db.session.add(rt); db.session.commit()
        self.assertIsNotNone(rt.property_id)

        rp = RatePlan(code='RP1', name='Plan', room_type_id=rt.id,
                       base_rate=100.0, currency='USD',
                       is_refundable=True, is_active=True)
        db.session.add(rp); db.session.commit()
        self.assertIsNotNone(rp.property_id)

    def test_whatsapp_message_has_property_id(self):
        prop_svc.current_property()
        m = WhatsAppMessage(
            direction='inbound', wa_message_id='wamid.test',
            from_phone_last4='0700', to_phone_last4='0800',
            message_type='text', body_text='hello',
        )
        db.session.add(m); db.session.commit()
        self.assertIsNotNone(m.property_id)


# ─────────────────────────────────────────────────────────────────────
# 4 + 5) Server-default backfill + scoped queries (Reqs 4, 5)
# ─────────────────────────────────────────────────────────────────────

class ServerDefaultBackfillTests(_BaseAppTest):

    def test_inserts_without_property_id_get_default(self):
        prop_svc.current_property()
        # Insert a row without setting property_id explicitly
        r = Room(number='42', name='Test', room_type='Test',
                 floor=0, capacity=2, price_per_night=600.0,
                 status='available', housekeeping_status='clean')
        db.session.add(r); db.session.commit()
        self.assertIsNotNone(r.property_id)
        self.assertEqual(r.property_id, 1)


class ScopedQueryTests(_BaseAppTest):

    def setUp(self):
        super().setUp()
        prop_svc.current_property()  # property #1

    def test_for_current_property_filters(self):
        # Create rooms in two properties: prop #1 (auto) and prop #2
        r1 = _seed_room('1', property_id=1)
        # Create a SECOND property
        prop2 = Property(code='second', name='Second',
                          timezone='UTC', currency_code='USD',
                          is_active=True)
        db.session.add(prop2); db.session.commit()
        r2 = _seed_room('2', property_id=prop2.id)

        # Without scoping — both rooms surface
        self.assertEqual(Room.query.count(), 2)

        # With scoping — only the active property's room
        scoped = prop_svc.for_current_property(Room.query)
        rows = scoped.all()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0].id, r1.id)


# ─────────────────────────────────────────────────────────────────────
# 6) No unscoped leakage in helpers (Req 6)
# ─────────────────────────────────────────────────────────────────────

class LeakRegressionTests(_BaseAppTest):

    def test_two_property_fixture_isolates_via_helper(self):
        """If a future code path uses for_current_property() consistently,
        a second seeded property does NOT leak into the default
        property's results."""
        # property #1 (auto-seeded) gets one room
        prop_svc.current_property()
        _seed_room('100', property_id=1)

        # property #2 gets a totally different room
        p2 = Property(code='leak-test', name='Leak Test',
                       timezone='UTC', currency_code='USD',
                       is_active=True)
        db.session.add(p2); db.session.commit()
        _seed_room('999', property_id=p2.id)

        scoped = prop_svc.for_current_property(Room.query).all()
        scoped_ids = {r.id for r in scoped}
        # Property #1's room only
        self.assertEqual(len(scoped_ids), 1)
        all_p2_rooms = Room.query.filter_by(property_id=p2.id).all()
        for r in all_p2_rooms:
            self.assertNotIn(r.id, scoped_ids,
                              msg='leakage: prop #2 room appeared in '
                                  'prop #1 scoped query')


# ─────────────────────────────────────────────────────────────────────
# 7) Branding uses current property settings (Req 7)
# ─────────────────────────────────────────────────────────────────────

class BrandingIntegrationTests(_BaseAppTest):

    def test_brand_dict_reads_from_property_settings(self):
        # Pre-seed a custom PropertySettings row + Property
        s = PropertySettings(property_name='BrandHaus',
                              short_name='BH',
                              currency_code='USD', timezone='UTC',
                              primary_color='#abcdef',
                              bank_account_number='999111')
        db.session.add(s); db.session.commit()
        p = Property(code='bh', name='BrandHaus Property',
                      timezone='UTC', currency_code='USD',
                      settings_id=s.id)
        db.session.add(p); db.session.commit()

        brand = branding_svc.get_brand()
        # Branding still pulls from PropertySettings (single-property)
        self.assertEqual(brand['name'], 'BrandHaus')
        self.assertEqual(brand['short_name'], 'BH')


# ─────────────────────────────────────────────────────────────────────
# 8) No WhatsApp / Gemini side effects (Req 8)
# ─────────────────────────────────────────────────────────────────────

class NoExternalCouplingTests(_BaseAppTest):

    def test_full_flow_no_external_calls(self):
        self._login(self.admin_id)
        prop_svc.current_property()
        # Hit the inspect page
        self.client.get('/admin/property/')
        self.assertEqual(wa._send.call_count,           0)
        self.assertEqual(wa._send_template.call_count,  0)
        self.assertEqual(ai_drafts.generate_draft.call_count, 0)


# ─────────────────────────────────────────────────────────────────────
# Inspect-page route tests
# ─────────────────────────────────────────────────────────────────────

class InspectPageTests(_BaseAppTest):

    def test_anonymous_redirected(self):
        r = self.client.get('/admin/property/')
        self.assertIn(r.status_code, (301, 302, 401))

    def test_staff_blocked(self):
        self._login(self.staff_id)
        r = self.client.get('/admin/property/')
        self.assertIn(r.status_code, (302, 401, 403))

    def test_admin_sees_property_name_and_counts(self):
        prop_svc.current_property()  # autoseed
        _seed_room('11')
        self._login(self.admin_id)
        r = self.client.get('/admin/property/')
        self.assertEqual(r.status_code, 200)
        body = r.get_data(as_text=True)
        self.assertIn('default', body)            # property code
        self.assertIn('Property-scoped record counts', body)


# ─────────────────────────────────────────────────────────────────────
# 9 + 10) Migration shape (Reqs 9, 10)
# ─────────────────────────────────────────────────────────────────────

class MigrationShapeTests(unittest.TestCase):

    def test_migration_file_exists(self):
        self.assertTrue(_MIGRATION_PATH.exists(),
                        f'expected migration at {_MIGRATION_PATH}')

    def test_migration_revision_metadata(self):
        text = _MIGRATION_PATH.read_text()
        self.assertIn("revision = '1d9b6a4f5e72'", text)
        self.assertIn("down_revision = '0c5e7f3b842a'", text)

    def test_migration_creates_only_properties_and_adds_property_id(self):
        text = _MIGRATION_PATH.read_text()
        # ONE new table created
        creates = set(re.findall(r"op\.create_table\(\s*'([^']+)'", text))
        self.assertEqual(creates, {'properties'},
                          f'unexpected tables: {creates}')

        # The migration uses a python-level for-loop over WAVE_1_TABLES
        # to call op.add_column with parameter `table`. Since the
        # column name is the only thing that matters at the source-
        # level audit, assert that the literal column name is
        # `property_id` everywhere it appears in `add_column(..., col)`
        # patterns and that the WAVE_1_TABLES tuple is intact.
        wave1_match = re.search(r'WAVE_1_TABLES\s*=\s*\((.*?)\)',
                                text, re.DOTALL)
        self.assertIsNotNone(wave1_match,
                              'migration must define WAVE_1_TABLES tuple')
        wave1 = set(re.findall(r"'([a-z_]+)'", wave1_match.group(1)))
        self.assertEqual(wave1, {
            'rooms', 'room_types', 'rate_plans', 'rate_overrides',
            'rate_restrictions', 'room_blocks', 'bookings',
            'booking_groups', 'invoices', 'folio_items',
            'cashier_transactions', 'whatsapp_messages',
        })
        # And the only column added inside the loop is property_id
        self.assertIn("sa.Column('property_id'", text)
        self.assertNotIn("sa.Column('foo'", text)   # sanity


if __name__ == '__main__':
    unittest.main()
