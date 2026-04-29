"""Tests for the Reservation Board prototype."""

from __future__ import annotations

import os
import unittest
from datetime import date, datetime, timedelta

# Clean env BEFORE app import — same pattern as other suites.
for _v in ('DATABASE_URL',
           'AI_DRAFT_PROVIDER', 'AI_DRAFT_MODEL',
           'GEMINI_API_KEY', 'ANTHROPIC_API_KEY',
           'WHATSAPP_ENABLED', 'WHATSAPP_TOKEN',
           'BRAND_NAME', 'BRAND_SHORT_NAME'):
    os.environ.pop(_v, None)
os.environ.setdefault('SECRET_KEY', 'test-secret-do-not-use-in-prod')

from config import Config                                 # noqa: E402
from app import create_app                                # noqa: E402
from app.models import (                                  # noqa: E402
    db, User, Room, Guest, Booking, Invoice,
)
from app.services import board as board_svc               # noqa: E402


class _TestConfig(Config):
    TESTING = True
    SQLALCHEMY_DATABASE_URI = 'sqlite:///:memory:'
    WTF_CSRF_ENABLED = False
    WHATSAPP_ENABLED = False


def _make_app():
    return create_app(_TestConfig)


# ─────────────────────────────────────────────────────────────────────
# 1) Pure helpers
# ─────────────────────────────────────────────────────────────────────

class ViewConfigTests(unittest.TestCase):

    def test_view_spans(self):
        self.assertEqual(board_svc.view_span_days('day'), 1)
        self.assertEqual(board_svc.view_span_days('7d'), 7)
        self.assertEqual(board_svc.view_span_days('14d'), 14)
        self.assertEqual(board_svc.view_span_days('30d'), 30)

    def test_normalize_view_falls_back_to_default(self):
        self.assertEqual(board_svc.normalize_view(None), '14d')
        self.assertEqual(board_svc.normalize_view(''), '14d')
        self.assertEqual(board_svc.normalize_view('garbage'), '14d')
        self.assertEqual(board_svc.normalize_view('7d'), '7d')

    def test_view_day_widths_distinct(self):
        widths = {v: board_svc.view_day_width_px(v)
                  for v in ('day', '7d', '14d', '30d')}
        self.assertEqual(len(set(widths.values())), 4)
        # day view should be widest, 30d should be narrowest
        self.assertGreater(widths['day'], widths['30d'])


class DateRangeTests(unittest.TestCase):

    def test_parse_start_iso(self):
        self.assertEqual(board_svc.parse_start_date('2026-04-30'),
                         date(2026, 4, 30))

    def test_parse_start_garbage_falls_back(self):
        d = board_svc.parse_start_date('not-a-date',
                                       default=date(2026, 5, 1))
        self.assertEqual(d, date(2026, 5, 1))

    def test_date_range_yields_consecutive_days(self):
        days = board_svc.date_range(date(2026, 4, 30), 5)
        self.assertEqual(days[0], date(2026, 4, 30))
        self.assertEqual(days[-1], date(2026, 5, 4))
        self.assertEqual(len(days), 5)

    def test_shift_range_forward_back(self):
        start = date(2026, 4, 30)
        nxt   = board_svc.shift_range(start, 7,  1)
        prev  = board_svc.shift_range(start, 7, -1)
        self.assertEqual(nxt,  date(2026, 5, 7))
        self.assertEqual(prev, date(2026, 4, 23))


# ─────────────────────────────────────────────────────────────────────
# 2) Booking → BookingBar placement math
# ─────────────────────────────────────────────────────────────────────

class _MockBooking:
    """Tiny stand-in for Booking + Guest + Invoice without ORM."""

    def __init__(self, *, id, ref, ci, co, status, ps=None,
                 first='A', last='B', guests=1):
        self.id = id
        self.booking_ref = ref
        self.check_in_date  = ci
        self.check_out_date = co
        self.status = status
        self.num_guests = guests
        self.guest = type('G', (), {'first_name': first, 'last_name': last})()
        if ps is not None:
            inv = type('I', (), {'payment_status': ps})()
            self.invoice = inv
        else:
            self.invoice = None


class BookingBarPlacementTests(unittest.TestCase):

    def test_bar_inside_window(self):
        b = _MockBooking(id=1, ref='BK1',
                         ci=date(2026, 5, 2), co=date(2026, 5, 5),
                         status='confirmed')
        bar = board_svc.make_booking_bar(b, date(2026, 5, 1), date(2026, 5, 8))
        self.assertIsNotNone(bar)
        # window start = 2026-05-01, ci offset = 1 day, +2 (skip room col) = col 3
        self.assertEqual(bar.grid_col_start, 3)
        self.assertEqual(bar.grid_col_span, 3)  # 3 nights
        self.assertTrue(bar.starts_in_range)
        self.assertTrue(bar.ends_in_range)

    def test_bar_left_clipped(self):
        # Stay starts before window
        b = _MockBooking(id=2, ref='BK2',
                         ci=date(2026, 4, 28), co=date(2026, 5, 3),
                         status='confirmed')
        bar = board_svc.make_booking_bar(b, date(2026, 5, 1), date(2026, 5, 8))
        self.assertIsNotNone(bar)
        self.assertEqual(bar.grid_col_start, 2)  # window start
        self.assertEqual(bar.grid_col_span, 2)   # 5/1 + 5/2
        self.assertFalse(bar.starts_in_range)

    def test_bar_right_clipped(self):
        # Stay ends after window
        b = _MockBooking(id=3, ref='BK3',
                         ci=date(2026, 5, 6), co=date(2026, 5, 12),
                         status='confirmed')
        bar = board_svc.make_booking_bar(b, date(2026, 5, 1), date(2026, 5, 8))
        self.assertIsNotNone(bar)
        self.assertEqual(bar.grid_col_start, 7)
        self.assertEqual(bar.grid_col_span, 2)
        self.assertFalse(bar.ends_in_range)

    def test_bar_completely_outside_window(self):
        b = _MockBooking(id=4, ref='BK4',
                         ci=date(2026, 6, 1), co=date(2026, 6, 5),
                         status='confirmed')
        bar = board_svc.make_booking_bar(b, date(2026, 5, 1), date(2026, 5, 8))
        self.assertIsNone(bar)


class StatusColorTests(unittest.TestCase):

    def test_each_canonical_status_has_color(self):
        for s in ('new_request', 'pending_payment', 'payment_uploaded',
                  'payment_verified', 'confirmed', 'checked_in',
                  'checked_out', 'cancelled', 'rejected'):
            cls = board_svc.bar_color_class(s)
            self.assertTrue(cls)
            # Each has a Tailwind bg- class
            self.assertIn('bg-', cls)

    def test_unknown_status_falls_back_to_slate(self):
        cls = board_svc.bar_color_class('weird_status')
        self.assertIn('slate', cls)

    def test_confirmed_and_checked_in_are_distinct_colors(self):
        # Operational requirement: must be visually distinguishable.
        self.assertNotEqual(
            board_svc.bar_color_class('confirmed'),
            board_svc.bar_color_class('checked_in'),
        )


class PaymentAccentTests(unittest.TestCase):

    def test_mismatch_marked_red(self):
        self.assertIn('red', board_svc.payment_accent_class('mismatch'))

    def test_verified_marked_green(self):
        self.assertIn('emerald', board_svc.payment_accent_class('verified'))

    def test_unknown_payment_no_accent(self):
        self.assertEqual('', board_svc.payment_accent_class('not_received'))


# ─────────────────────────────────────────────────────────────────────
# 3) Route / template rendering
# ─────────────────────────────────────────────────────────────────────

def _seed_users():
    admin = User(username='admin1', email='a@x', role='admin')
    admin.set_password('a-very-strong-password-1!')
    staff = User(username='staff1', email='s@x', role='staff')
    staff.set_password('a-very-strong-password-1!')
    db.session.add_all([admin, staff])
    db.session.commit()
    return admin.id, staff.id


def _seed_room(num='99'):
    room = Room(number=num, name='Test', room_type='Test',
                floor=0, capacity=2, price_per_night=600.0)
    db.session.add(room)
    db.session.commit()
    return room


def _seed_booking(room, *, ci, co, status='confirmed', ref='BKBOARD'):
    g = Guest(first_name='Hassan', last_name='Demo',
              phone='+9607000001', email='hd@example.com')
    db.session.add(g)
    db.session.commit()
    b = Booking(
        booking_ref=ref,
        room_id=room.id, guest_id=g.id,
        check_in_date=ci, check_out_date=co,
        num_guests=1, total_amount=1200.0,
        status=status,
    )
    db.session.add(b)
    db.session.commit()
    return b


class _RouteBase(unittest.TestCase):

    def setUp(self):
        self.app = _make_app()
        self.ctx = self.app.app_context()
        self.ctx.push()
        db.create_all()
        self.admin_id, self.staff_id = _seed_users()
        self.client = self.app.test_client()

    def tearDown(self):
        db.session.remove()
        db.drop_all()
        self.ctx.pop()

    def _login(self, uid):
        with self.client.session_transaction() as sess:
            sess['_user_id'] = str(uid)
            sess['_fresh'] = True


class BoardRouteAuthTests(_RouteBase):

    def test_anonymous_redirected(self):
        r = self.client.get('/board')
        self.assertIn(r.status_code, (301, 302, 401))

    def test_staff_blocked(self):
        self._login(self.staff_id)
        r = self.client.get('/board')
        self.assertIn(r.status_code, (302, 401, 403))

    def test_admin_allowed(self):
        self._login(self.admin_id)
        r = self.client.get('/board')
        self.assertEqual(r.status_code, 200)


class BoardRouteRenderingTests(_RouteBase):

    def setUp(self):
        super().setUp()
        self._login(self.admin_id)

    def test_default_view_has_toolbar_and_grid(self):
        room = _seed_room('99')
        r = self.client.get('/board')
        self.assertEqual(r.status_code, 200)
        self.assertIn(b'Reservation Board', r.data)
        self.assertIn(b'14 days', r.data)        # default view label
        self.assertIn(b'#99', r.data)            # room number rendered
        self.assertIn(b'data-view="14d"', r.data)

    def test_view_toggle_changes_grid(self):
        _seed_room('99')
        r = self.client.get('/board?view=7d')
        self.assertEqual(r.status_code, 200)
        self.assertIn(b'data-view="7d"', r.data)

    def test_booking_appears_as_bar(self):
        room = _seed_room('99')
        today = date.today()
        # Make a booking that overlaps today
        _seed_booking(room,
                      ci=today, co=today + timedelta(days=2),
                      status='confirmed', ref='BKVIEW')
        r = self.client.get('/board?view=14d')
        self.assertEqual(r.status_code, 200)
        self.assertIn(b'BKVIEW', r.data)
        self.assertIn(b'data-booking-id', r.data)
        self.assertIn(b'Demo', r.data)  # guest last name in bar

    def test_today_cell_marked(self):
        _seed_room('99')
        r = self.client.get('/board')
        self.assertIn(b'is-today', r.data)

    def test_filter_by_floor_returns_only_matching_rooms(self):
        # Use room numbers that won't collide with CSS hex strings.
        room1 = Room(number='G7-room', name='F0', room_type='Deluxe',
                     floor=0, capacity=2, price_per_night=600.0)
        room2 = Room(number='F1-room', name='F1', room_type='Twin',
                     floor=1, capacity=2, price_per_night=600.0)
        db.session.add_all([room1, room2])
        db.session.commit()
        r = self.client.get('/board?floor=1')
        self.assertEqual(r.status_code, 200)
        # Look only at the board area BEFORE the modal markup. The
        # "Move room" / "Block room" modals list ALL rooms in their
        # <select> dropdowns regardless of the floor filter (correct
        # UX), so the unfiltered room legitimately appears in those
        # <option> tags later in the document.
        board_only = r.data.split(b'id="modal-move-room"')[0]
        self.assertIn(b'F1-room', board_only)
        self.assertNotIn(b'G7-room', board_only)

    def test_search_matches_booking_ref(self):
        room = _seed_room('99')
        today = date.today()
        _seed_booking(room,
                      ci=today, co=today + timedelta(days=2),
                      status='confirmed', ref='BKSEARCH123')
        r = self.client.get('/board?search=BKSEARCH')
        self.assertEqual(r.status_code, 200)
        self.assertIn(b'BKSEARCH123', r.data)

    def test_drawer_data_is_valid_json(self):
        import json
        import re
        room = _seed_room('99')
        today = date.today()
        _seed_booking(room,
                      ci=today, co=today + timedelta(days=2),
                      status='confirmed', ref='BKJSON')
        r = self.client.get('/board')
        match = re.search(
            rb'<script id="board-bookings-data"[^>]*>(.*?)</script>',
            r.data, re.DOTALL,
        )
        self.assertIsNotNone(match, 'bookings JSON script not found')
        raw = match.group(1).decode('utf-8')
        # Strip trailing commas before } / ] for jinja-loop safety
        raw = re.sub(r',\s*}', '}', raw)
        raw = re.sub(r',\s*\]', ']', raw)
        data = json.loads(raw)
        self.assertTrue(any(v.get('ref') == 'BKJSON' for v in data.values()))


class GroupingDensityTests(_RouteBase):

    def setUp(self):
        super().setUp()
        self._login(self.admin_id)

    def _seed_two_floor_rooms(self):
        rooms = [
            Room(number='101', name='F0a', room_type='Deluxe',
                 floor=0, capacity=2, price_per_night=600.0),
            Room(number='102', name='F0b', room_type='Twin',
                 floor=0, capacity=2, price_per_night=600.0),
            Room(number='201', name='F1a', room_type='Deluxe',
                 floor=1, capacity=2, price_per_night=600.0),
            Room(number='202', name='F1b', room_type='Suite',
                 floor=1, capacity=2, price_per_night=600.0),
        ]
        db.session.add_all(rooms)
        db.session.commit()
        return rooms

    def test_default_no_grouping(self):
        self._seed_two_floor_rooms()
        r = self.client.get('/board')
        self.assertEqual(r.status_code, 200)
        # No "Floor 0" header label rendered when grouping=none
        self.assertNotIn(b'class="group-header"', r.data)

    def test_group_by_floor_renders_headers(self):
        self._seed_two_floor_rooms()
        r = self.client.get('/board?group=floor')
        self.assertEqual(r.status_code, 200)
        self.assertIn(b'class="group-header"', r.data)
        self.assertIn(b'Floor 0', r.data)
        self.assertIn(b'Floor 1', r.data)

    def test_group_by_room_type_renders_type_headers(self):
        self._seed_two_floor_rooms()
        r = self.client.get('/board?group=room_type')
        self.assertEqual(r.status_code, 200)
        self.assertIn(b'class="group-header"', r.data)
        self.assertIn(b'Deluxe', r.data)
        self.assertIn(b'Suite', r.data)

    def test_invalid_group_falls_back_to_none(self):
        self._seed_two_floor_rooms()
        r = self.client.get('/board?group=garbage')
        self.assertEqual(r.status_code, 200)
        self.assertNotIn(b'class="group-header"', r.data)

    def test_density_compact_emits_attr(self):
        _seed_room('99')
        r = self.client.get('/board?density=compact')
        self.assertEqual(r.status_code, 200)
        self.assertIn(b'data-density="compact"', r.data)

    def test_density_default_standard(self):
        _seed_room('99')
        r = self.client.get('/board')
        self.assertIn(b'data-density="standard"', r.data)

    def test_grouping_preserved_in_view_toggle_links(self):
        self._seed_two_floor_rooms()
        r = self.client.get('/board?view=14d&group=floor&density=compact')
        # The 7d view link should preserve group=floor + density=compact
        # in its query string.
        self.assertIn(b'group=floor', r.data)
        self.assertIn(b'density=compact', r.data)


class DrawerActionTests(_RouteBase):

    def setUp(self):
        super().setUp()
        self._login(self.admin_id)

    def _drawer_data(self):
        import json
        import re
        r = self.client.get('/board')
        match = re.search(
            rb'<script id="board-bookings-data"[^>]*>(.*?)</script>',
            r.data, re.DOTALL,
        )
        self.assertIsNotNone(match)
        return json.loads(match.group(1).decode('utf-8'))

    def test_drawer_data_includes_guest_id_and_phone(self):
        room = _seed_room('77')
        today = date.today()
        _seed_booking(room,
                      ci=today, co=today + timedelta(days=2),
                      status='confirmed', ref='BKDRAWER')
        data = self._drawer_data()
        booking = next((v for v in data.values() if v.get('ref') == 'BKDRAWER'), None)
        self.assertIsNotNone(booking)
        self.assertIn('guestId', booking)
        self.assertIn('phoneDigits', booking)
        # Synthetic guest phone +9607000001 → digits-only "9607000001"
        self.assertEqual(booking['phoneDigits'], '9607000001')

    def test_drawer_data_includes_invoice_summary(self):
        room = _seed_room('77')
        today = date.today()
        b = _seed_booking(room,
                          ci=today, co=today + timedelta(days=2),
                          status='confirmed', ref='BKINV')
        # Attach an invoice with paid balance
        inv = Invoice(
            booking_id=b.id,
            invoice_number='INV-BKINV',
            total_amount=2400.0,
            payment_status='verified',
            amount_paid=1200.0,
        )
        db.session.add(inv)
        db.session.commit()

        data = self._drawer_data()
        booking = next((v for v in data.values() if v.get('ref') == 'BKINV'), None)
        self.assertIsNotNone(booking)
        self.assertIn('invoice', booking)
        self.assertIsNotNone(booking['invoice'])
        self.assertEqual(booking['invoice']['total'], 2400.0)
        self.assertEqual(booking['invoice']['paid'], 1200.0)
        self.assertEqual(booking['invoice']['balance'], 1200.0)
        self.assertEqual(booking['invoice']['number'], 'INV-BKINV')

    def test_drawer_data_includes_activity_array(self):
        from app.models import ActivityLog
        room = _seed_room('77')
        today = date.today()
        b = _seed_booking(room,
                          ci=today, co=today + timedelta(days=2),
                          status='confirmed', ref='BKACT')
        # Insert a few audit rows for this booking
        for action in ('booking.created', 'booking.confirmed'):
            db.session.add(ActivityLog(
                action=action, actor_type='admin',
                booking_id=b.id, description='test row',
            ))
        db.session.commit()

        data = self._drawer_data()
        booking = next((v for v in data.values() if v.get('ref') == 'BKACT'), None)
        self.assertIsNotNone(booking)
        self.assertIn('activity', booking)
        self.assertIsInstance(booking['activity'], list)
        # Should contain both audit rows we added (newest first)
        actions = [r['action'] for r in booking['activity']]
        self.assertIn('booking.created', actions)
        self.assertIn('booking.confirmed', actions)
        # Each row has the canonical fields
        for row in booking['activity']:
            self.assertIn('action', row)
            self.assertIn('description', row)
            self.assertIn('createdAt', row)
            self.assertIn('actor', row)

    def test_drawer_data_activity_capped_at_three(self):
        from app.models import ActivityLog
        room = _seed_room('77')
        today = date.today()
        b = _seed_booking(room,
                          ci=today, co=today + timedelta(days=2),
                          status='confirmed', ref='BKMANY')
        for i in range(7):
            db.session.add(ActivityLog(
                action='ai.draft.created', actor_type='admin',
                booking_id=b.id, description=f'row {i}',
            ))
        db.session.commit()
        data = self._drawer_data()
        booking = next((v for v in data.values() if v.get('ref') == 'BKMANY'), None)
        self.assertIsNotNone(booking)
        self.assertLessEqual(len(booking['activity']), 3)


class FilterStateTests(_RouteBase):

    def setUp(self):
        super().setUp()
        self._login(self.admin_id)

    def test_filter_summary_empty_when_no_filters(self):
        _seed_room('77')
        r = self.client.get('/board')
        # Reset chip should NOT appear when no filters active
        self.assertNotIn(b'Reset', r.data[:r.data.find(b'<form')])

    def test_filter_summary_visible_when_floor_active(self):
        _seed_room('77')
        r = self.client.get('/board?floor=0')
        self.assertIn(b'Reset', r.data)
        # Active count chip should show "1"
        self.assertIn(b'active', r.data)


class MobileFallbackTests(_RouteBase):

    def setUp(self):
        super().setUp()
        self._login(self.admin_id)

    def test_mobile_room_card_renders_for_every_room(self):
        # Mobile fallback now lists every room as a card so the front
        # desk can scan occupancy on a phone.
        for n in ('11', '12', '13'):
            room = Room(number=n, name='F0', room_type='Deluxe',
                        floor=0, capacity=2, price_per_night=600.0)
            db.session.add(room)
        db.session.commit()
        r = self.client.get('/board')
        self.assertEqual(r.status_code, 200)
        # New mobile-rooms structure
        self.assertIn(b'class="mobile-ops', r.data)
        self.assertIn(b'class="m-room-card"', r.data)
        # All three rooms render (the m-room-num cell carries the
        # room number — count those occurrences).
        self.assertGreaterEqual(
            r.data.count(b'class="m-room-num'),
            3,
            'expected one m-room-num per room',
        )

    def test_mobile_room_card_shows_booking_when_present(self):
        room = _seed_room('99')
        today = date.today()
        _seed_booking(room,
                      ci=today, co=today + timedelta(days=2),
                      status='confirmed', ref='BKMOB')
        r = self.client.get('/board')
        self.assertEqual(r.status_code, 200)
        # Booking row appears inside a room card with the m-booking-row class
        self.assertIn(b'class="m-booking-row"', r.data)
        self.assertIn(b'BKMOB', r.data)
        # The mobile booking row should call openDrawer so taps open the
        # side drawer (consistent with the desktop bar behaviour).
        self.assertIn(b'openDrawer(event,', r.data)

    def test_mobile_empty_room_shows_no_bookings_message(self):
        # Room with no bookings → "No bookings in this window"
        _seed_room('77')
        r = self.client.get('/board')
        self.assertIn(b'm-booking-empty', r.data)

    def test_mobile_show_full_board_toggle_present(self):
        _seed_room('77')
        r = self.client.get('/board')
        # The toggle button must exist so the user can override the
        # mobile view to see the full tape chart.
        self.assertIn(b'm-board-toggle', r.data)
        self.assertIn(b"force-board", r.data)

    def test_mobile_view_window_label_reflects_view(self):
        _seed_room('77')
        r = self.client.get('/board?view=7d')
        self.assertEqual(r.status_code, 200)
        self.assertIn(b'7-day window', r.data)
        r = self.client.get('/board?view=14d')
        self.assertIn(b'14-day window', r.data)


class BoardRouteSafetyTests(_RouteBase):

    def setUp(self):
        super().setUp()
        self._login(self.admin_id)

    def test_board_does_not_mutate_booking_status(self):
        room = _seed_room('99')
        today = date.today()
        b = _seed_booking(room, ci=today, co=today + timedelta(days=2),
                          status='confirmed', ref='BKKEEP')
        before = b.status
        self.client.get('/board')
        self.client.get('/board?view=7d')
        self.client.get('/board?view=30d&start=' + today.isoformat())
        b2 = Booking.query.get(b.id)
        self.assertEqual(b2.status, before)

    def test_board_renders_when_no_rooms_no_bookings(self):
        # Empty fresh DB — should not 500
        r = self.client.get('/board')
        self.assertEqual(r.status_code, 200)
        self.assertIn(b'No rooms match', r.data)


if __name__ == '__main__':
    unittest.main()
