"""Tests for Maintenance / Work Orders V1.

Covers:
  - WorkOrder model creation defaults + label properties
  - services.maintenance enum validators reject invalid values
  - create_work_order writes ActivityLog 'maintenance.created'
  - assign / update_status / mark_room_out_of_order flows
  - Status transition guard: terminal states are sticky
  - Room OOO integration flips Room.housekeeping_status='out_of_order'
    AND Room.status='maintenance'
  - GET /maintenance returns 200 for admin, 302 for staff
  - POST /maintenance/ creates a work order end-to-end
  - Migration file exists and is wired up
  - No WhatsApp / Gemini calls anywhere in the maintenance code path
"""

from __future__ import annotations

import os
import re
import unittest
from datetime import date, timedelta

for _v in ('DATABASE_URL', 'AI_DRAFT_PROVIDER', 'AI_DRAFT_MODEL',
           'GEMINI_API_KEY', 'ANTHROPIC_API_KEY',
           'WHATSAPP_ENABLED', 'WHATSAPP_TOKEN'):
    os.environ.pop(_v, None)
os.environ.setdefault('SECRET_KEY', 'test-secret-do-not-use-in-prod')

from config import Config                                       # noqa: E402
from app import create_app                                      # noqa: E402
from app.models import (                                        # noqa: E402
    db, User, Room, Booking, Guest, WorkOrder, ActivityLog,
)
from app.services import maintenance as svc                     # noqa: E402


class _TestConfig(Config):
    TESTING = True
    SQLALCHEMY_DATABASE_URI = 'sqlite:///:memory:'
    WTF_CSRF_ENABLED = False
    WHATSAPP_ENABLED = False


def _seed_basic():
    """One admin, one staff, one room; returns dict of ids."""
    admin = User(username='admin', email='a@x', role='admin')
    admin.set_password('aaaaaaaaaa1')
    staff = User(username='staff', email='s@x', role='staff',
                 department='housekeeping')
    staff.set_password('aaaaaaaaaa1')
    db.session.add_all([admin, staff])
    room = Room(number='101', name='Standard', room_type='Standard',
                floor=1, capacity=2, price_per_night=800.0,
                is_active=True,
                status='available', housekeeping_status='clean')
    db.session.add(room)
    db.session.commit()
    return {'admin_id': admin.id, 'staff_id': staff.id, 'room_id': room.id}


# ── Model / vocabulary ────────────────────────────────────────────

class WorkOrderModelTests(unittest.TestCase):

    def setUp(self):
        self.app = create_app(_TestConfig)
        self.ctx = self.app.app_context()
        self.ctx.push()
        db.create_all()
        self.ids = _seed_basic()

    def tearDown(self):
        db.session.remove()
        db.drop_all()
        self.ctx.pop()

    def test_default_status_is_new(self):
        wo = WorkOrder(title='AC dripping')
        db.session.add(wo); db.session.commit()
        self.assertEqual(wo.status, 'new')
        self.assertEqual(wo.priority, 'medium')
        self.assertEqual(wo.category, 'general')
        self.assertTrue(wo.is_open)

    def test_label_properties_resolve_against_vocabulary(self):
        wo = WorkOrder(title='X', category='hvac', priority='urgent',
                       status='in_progress')
        self.assertEqual(wo.category_label, 'HVAC / climate')
        self.assertEqual(wo.priority_label, 'Urgent')
        self.assertEqual(wo.status_label, 'In progress')

    def test_vocabulary_tuples_match_spec(self):
        cats = {slug for slug, _ in WorkOrder.CATEGORIES}
        self.assertEqual(cats, {
            'plumbing', 'electrical', 'hvac', 'cleaning',
            'furniture', 'appliance', 'safety', 'general',
        })
        pris = {slug for slug, _ in WorkOrder.PRIORITIES}
        self.assertEqual(pris, {'low', 'medium', 'high', 'urgent'})
        sts = {slug for slug, _ in WorkOrder.STATUSES}
        self.assertEqual(sts, {
            'new', 'assigned', 'in_progress', 'waiting',
            'resolved', 'cancelled',
        })


# ── Service-layer validators + lifecycle ──────────────────────────

class MaintenanceServiceTests(unittest.TestCase):

    def setUp(self):
        self.app = create_app(_TestConfig)
        self.ctx = self.app.app_context()
        self.ctx.push()
        db.create_all()
        self.ids = _seed_basic()

    def tearDown(self):
        db.session.remove()
        db.drop_all()
        self.ctx.pop()

    # validators
    def test_rejects_invalid_category(self):
        r = svc.create_work_order(title='X', category='not-a-thing',
                                  actor_user_id=self.ids['admin_id'])
        self.assertFalse(r.ok)
        self.assertIn('category', r.message.lower())

    def test_rejects_invalid_priority(self):
        r = svc.create_work_order(title='X', priority='extreme',
                                  actor_user_id=self.ids['admin_id'])
        self.assertFalse(r.ok)
        self.assertIn('priority', r.message.lower())

    def test_rejects_invalid_status_in_update(self):
        wo_r = svc.create_work_order(title='X',
                                     actor_user_id=self.ids['admin_id'])
        self.assertTrue(wo_r.ok)
        r = svc.update_status(work_order_id=wo_r.work_order.id,
                              new_status='melted',
                              actor_user_id=self.ids['admin_id'])
        self.assertFalse(r.ok)
        self.assertIn('invalid status', r.message.lower())

    def test_blank_title_rejected(self):
        r = svc.create_work_order(title='   ',
                                  actor_user_id=self.ids['admin_id'])
        self.assertFalse(r.ok)

    # creation logs activity
    def test_create_emits_activity_log(self):
        before = ActivityLog.query.filter_by(action='maintenance.created').count()
        r = svc.create_work_order(
            title='Drip in 101',
            category='plumbing', priority='high',
            room_id=self.ids['room_id'],
            actor_user_id=self.ids['admin_id'],
            reported_by_user_id=self.ids['admin_id'],
        )
        self.assertTrue(r.ok)
        after = ActivityLog.query.filter_by(action='maintenance.created').count()
        self.assertEqual(after, before + 1)

    # assignment
    def test_assign_bumps_status_from_new_to_assigned(self):
        r = svc.create_work_order(title='X',
                                  actor_user_id=self.ids['admin_id'])
        self.assertEqual(r.work_order.status, 'new')
        a = svc.assign(work_order_id=r.work_order.id,
                       user_id=self.ids['staff_id'],
                       actor_user_id=self.ids['admin_id'])
        self.assertTrue(a.ok)
        self.assertEqual(a.work_order.status, 'assigned')
        self.assertEqual(a.work_order.assigned_to_user_id,
                         self.ids['staff_id'])
        # activity logged
        self.assertGreaterEqual(
            ActivityLog.query.filter_by(action='maintenance.assigned').count(),
            1,
        )

    def test_unassign_keeps_status(self):
        r = svc.create_work_order(title='X',
                                  assigned_to_user_id=self.ids['staff_id'],
                                  actor_user_id=self.ids['admin_id'])
        self.assertEqual(r.work_order.status, 'assigned')
        a = svc.assign(work_order_id=r.work_order.id, user_id=None,
                       actor_user_id=self.ids['admin_id'])
        self.assertTrue(a.ok)
        self.assertIsNone(a.work_order.assigned_to_user_id)

    def test_assign_rejected_for_terminal_orders(self):
        r = svc.create_work_order(title='X',
                                  actor_user_id=self.ids['admin_id'])
        svc.update_status(work_order_id=r.work_order.id,
                          new_status='resolved',
                          actor_user_id=self.ids['admin_id'])
        a = svc.assign(work_order_id=r.work_order.id,
                       user_id=self.ids['staff_id'],
                       actor_user_id=self.ids['admin_id'])
        self.assertFalse(a.ok)
        self.assertIn('cannot', a.message.lower())

    # status transitions
    def test_resolve_writes_resolution_notes_and_resolved_at(self):
        r = svc.create_work_order(title='X',
                                  actor_user_id=self.ids['admin_id'])
        u = svc.update_status(work_order_id=r.work_order.id,
                              new_status='resolved',
                              resolution_notes='Replaced gasket.',
                              actor_user_id=self.ids['admin_id'])
        self.assertTrue(u.ok)
        wo = WorkOrder.query.get(r.work_order.id)
        self.assertEqual(wo.status, 'resolved')
        self.assertIsNotNone(wo.resolved_at)
        self.assertEqual(wo.resolution_notes, 'Replaced gasket.')
        # 'maintenance.resolved' specifically logged
        self.assertGreaterEqual(
            ActivityLog.query.filter_by(action='maintenance.resolved').count(),
            1,
        )

    def test_cannot_move_from_resolved_back_to_in_progress(self):
        r = svc.create_work_order(title='X',
                                  actor_user_id=self.ids['admin_id'])
        svc.update_status(work_order_id=r.work_order.id,
                          new_status='resolved',
                          actor_user_id=self.ids['admin_id'])
        bad = svc.update_status(work_order_id=r.work_order.id,
                                new_status='in_progress',
                                actor_user_id=self.ids['admin_id'])
        self.assertFalse(bad.ok)

    def test_cannot_move_from_cancelled_to_anything(self):
        r = svc.create_work_order(title='X',
                                  actor_user_id=self.ids['admin_id'])
        svc.update_status(work_order_id=r.work_order.id,
                          new_status='cancelled',
                          actor_user_id=self.ids['admin_id'])
        for target in ('new', 'in_progress', 'resolved'):
            bad = svc.update_status(work_order_id=r.work_order.id,
                                    new_status=target,
                                    actor_user_id=self.ids['admin_id'])
            self.assertFalse(bad.ok, f'expected {target} to be blocked')

    def test_status_change_logs_status_changed_action(self):
        r = svc.create_work_order(title='X',
                                  actor_user_id=self.ids['admin_id'])
        before = ActivityLog.query.filter_by(
            action='maintenance.status_changed').count()
        svc.update_status(work_order_id=r.work_order.id,
                          new_status='in_progress',
                          actor_user_id=self.ids['admin_id'])
        after = ActivityLog.query.filter_by(
            action='maintenance.status_changed').count()
        self.assertEqual(after, before + 1)

    # room OOO integration
    def test_mark_room_ooo_flips_room_state(self):
        room = Room.query.get(self.ids['room_id'])
        self.assertEqual(room.housekeeping_status, 'clean')
        self.assertEqual(room.status, 'available')

        r = svc.create_work_order(
            title='Severe leak',
            category='plumbing', priority='urgent',
            room_id=self.ids['room_id'],
            actor_user_id=self.ids['admin_id'],
        )
        ooo = svc.mark_room_out_of_order(
            work_order_id=r.work_order.id,
            actor_user_id=self.ids['admin_id'],
        )
        self.assertTrue(ooo.ok)
        room = Room.query.get(self.ids['room_id'])
        self.assertEqual(room.housekeeping_status, 'out_of_order')
        self.assertEqual(room.status, 'maintenance')
        # action logged
        self.assertEqual(
            ActivityLog.query.filter_by(
                action='maintenance.room_out_of_order').count(),
            1,
        )

    def test_mark_room_ooo_fails_when_no_room(self):
        r = svc.create_work_order(title='Property-wide issue',
                                  actor_user_id=self.ids['admin_id'])
        ooo = svc.mark_room_out_of_order(
            work_order_id=r.work_order.id,
            actor_user_id=self.ids['admin_id'],
        )
        self.assertFalse(ooo.ok)
        self.assertIn('no linked room', ooo.message.lower())

    # read helpers
    def test_summary_counts_shape(self):
        # one open + one resolved + one urgent
        svc.create_work_order(title='A', priority='urgent',
                              actor_user_id=self.ids['admin_id'])
        b = svc.create_work_order(title='B',
                                  actor_user_id=self.ids['admin_id'])
        svc.update_status(work_order_id=b.work_order.id,
                          new_status='resolved',
                          actor_user_id=self.ids['admin_id'])
        s = svc.summary_counts()
        self.assertEqual(s['total'], 2)
        self.assertEqual(s['open'], 1)
        self.assertEqual(s['urgent'], 1)
        self.assertEqual(s['in_progress'], 0)
        self.assertEqual(s['waiting'], 0)

    def test_open_count_by_room(self):
        svc.create_work_order(title='A', room_id=self.ids['room_id'],
                              actor_user_id=self.ids['admin_id'])
        b = svc.create_work_order(title='B', room_id=self.ids['room_id'],
                                  actor_user_id=self.ids['admin_id'])
        svc.update_status(work_order_id=b.work_order.id,
                          new_status='resolved',
                          actor_user_id=self.ids['admin_id'])
        counts = svc.open_count_by_room([self.ids['room_id']])
        self.assertEqual(counts.get(self.ids['room_id']), 1)


# ── Routes / HTTP layer ───────────────────────────────────────────

class MaintenanceRoutesTests(unittest.TestCase):

    def setUp(self):
        self.app = create_app(_TestConfig)
        self.ctx = self.app.app_context()
        self.ctx.push()
        db.create_all()
        self.ids = _seed_basic()
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

    def test_admin_can_render_maintenance_index(self):
        self._login('admin')
        r = self.client.get('/maintenance/')
        self.assertEqual(r.status_code, 200)
        body = r.data.decode('utf-8', errors='ignore')
        self.assertIn('Maintenance', body)
        # KPI tiles present
        self.assertIn('Total', body)
        self.assertIn('Open', body)

    def test_staff_bounced_off_maintenance(self):
        # /maintenance is admin-only and not in the staff_guard whitelist
        self._login('staff')
        r = self.client.get('/maintenance/', follow_redirects=False)
        self.assertIn(r.status_code, (302, 303),
                      f'expected redirect, got {r.status_code}')

    def test_post_create_creates_work_order_and_redirects_to_detail(self):
        self._login('admin')
        r = self.client.post('/maintenance/', data={
            'title': 'Toilet running in 101',
            'category': 'plumbing',
            'priority': 'high',
            'room_id': str(self.ids['room_id']),
        }, follow_redirects=False)
        self.assertIn(r.status_code, (302, 303))
        self.assertEqual(WorkOrder.query.count(), 1)
        wo = WorkOrder.query.first()
        self.assertEqual(wo.title, 'Toilet running in 101')
        self.assertEqual(wo.priority, 'high')
        self.assertIn(f'/maintenance/{wo.id}', r.headers.get('Location', ''))

    def test_admin_can_render_detail_page(self):
        self._login('admin')
        r0 = svc.create_work_order(title='Render me', priority='medium',
                                   room_id=self.ids['room_id'],
                                   actor_user_id=self.ids['admin_id'])
        r = self.client.get(f'/maintenance/{r0.work_order.id}')
        self.assertEqual(r.status_code, 200)
        body = r.data.decode('utf-8', errors='ignore')
        self.assertIn('Render me', body)
        self.assertIn('Change status', body)
        self.assertIn('Assignment', body)

    def test_assign_route_persists(self):
        self._login('admin')
        r0 = svc.create_work_order(title='X',
                                   actor_user_id=self.ids['admin_id'])
        r = self.client.post(
            f'/maintenance/{r0.work_order.id}/assign',
            data={'assigned_to_user_id': str(self.ids['staff_id'])},
            follow_redirects=False,
        )
        self.assertIn(r.status_code, (302, 303))
        wo = WorkOrder.query.get(r0.work_order.id)
        self.assertEqual(wo.assigned_to_user_id, self.ids['staff_id'])
        self.assertEqual(wo.status, 'assigned')

    def test_status_route_resolves(self):
        self._login('admin')
        r0 = svc.create_work_order(title='X',
                                   actor_user_id=self.ids['admin_id'])
        r = self.client.post(
            f'/maintenance/{r0.work_order.id}/status',
            data={'status': 'resolved',
                  'resolution_notes': 'Tightened the valve.'},
            follow_redirects=False,
        )
        self.assertIn(r.status_code, (302, 303))
        wo = WorkOrder.query.get(r0.work_order.id)
        self.assertEqual(wo.status, 'resolved')
        self.assertEqual(wo.resolution_notes, 'Tightened the valve.')

    def test_mark_room_ooo_route_flips_room(self):
        self._login('admin')
        r0 = svc.create_work_order(
            title='X', room_id=self.ids['room_id'],
            actor_user_id=self.ids['admin_id'],
        )
        r = self.client.post(
            f'/maintenance/{r0.work_order.id}/mark-room-ooo',
            follow_redirects=False,
        )
        self.assertIn(r.status_code, (302, 303))
        room = Room.query.get(self.ids['room_id'])
        self.assertEqual(room.housekeeping_status, 'out_of_order')
        self.assertEqual(room.status, 'maintenance')


# ── Migration file exists & wired up ──────────────────────────────

class MaintenanceMigrationTests(unittest.TestCase):

    def test_work_orders_migration_file_exists(self):
        path = os.path.join(
            os.path.dirname(__file__), '..',
            'migrations', 'versions',
            'a8f3c91d5b27_add_work_orders_table.py',
        )
        self.assertTrue(os.path.isfile(path),
                        f'expected migration file at {path}')
        with open(path) as f:
            text = f.read()
        self.assertIn("revision      = 'a8f3c91d5b27'", text)
        self.assertIn("'work_orders'", text)
        # FKs present
        self.assertIn('rooms.id', text)
        self.assertIn('bookings.id', text)
        self.assertIn('users.id', text)


# ── No external calls in maintenance code path ────────────────────

class MaintenanceIsolationTests(unittest.TestCase):
    """Static guard: the maintenance module never imports/calls
    WhatsApp / Gemini / external network helpers. We parse the AST
    so a docstring sentence like "this module never sends WhatsApp"
    doesn't trip the guard — only real imports / Name / Attribute
    references do."""

    _BANNED = ('whatsapp', 'gemini', 'requests', 'urllib', 'httpx',
               'anthropic')

    def _read(self, rel_path):
        path = os.path.join(os.path.dirname(__file__), '..', rel_path)
        with open(path) as f:
            return f.read()

    def _collect_identifiers(self, source):
        """Return the lower-cased set of every imported module, called
        attribute name, and bare identifier in `source` — but skip
        string literals and comments. AST walk does this for free."""
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

    def _assert_no_banned_idents(self, rel_path):
        names = self._collect_identifiers(self._read(rel_path))
        for banned in self._BANNED:
            self.assertNotIn(
                banned, names,
                f'unexpected identifier {banned!r} in {rel_path}',
            )

    def test_service_has_no_external_calls(self):
        self._assert_no_banned_idents('app/services/maintenance.py')

    def test_routes_have_no_external_calls(self):
        self._assert_no_banned_idents('app/routes/maintenance.py')


if __name__ == '__main__':
    unittest.main()
