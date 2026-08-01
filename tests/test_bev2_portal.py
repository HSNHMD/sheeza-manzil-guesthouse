"""Booking Engine V2 Phase 2 — portal integration tests (B6 blocking).

Full flow: search 2 types -> selection holds -> guest form/submit -> pending
group -> admin confirm -> group + master folio + assigned rooms. Plus the
expiry paths and the anti-abuse rule. SQLite; messaging off; tmp-safe.
The endpoint-level race is in test_bev2_race.py (Postgres).
"""

from __future__ import annotations

import io
import os
import tempfile
import unittest
from datetime import date, timedelta, datetime

for _v in ('DATABASE_URL', 'WHATSAPP_ENABLED', 'WHATSAPP_TOKEN',
           'AI_DRAFT_PROVIDER', 'GEMINI_API_KEY', 'ANTHROPIC_API_KEY'):
    os.environ.pop(_v, None)
os.environ.setdefault('SECRET_KEY', 'test-secret-do-not-use-in-prod')

from config import Config                                          # noqa: E402
from app import create_app                                        # noqa: E402
from app.models import (db, User, Room, Guest, Booking, RoomType,  # noqa: E402
                        Hold, BookingGroup, Property)
from app.services import portal as portal_svc, holds as holds_svc  # noqa: E402

_CI = date.today() + timedelta(days=14)
_CO = _CI + timedelta(days=2)


class _TestConfig(Config):
    TESTING = True
    SQLALCHEMY_DATABASE_URI = 'sqlite:///:memory:'
    WTF_CSRF_ENABLED = False
    WHATSAPP_ENABLED = False


class _Base(unittest.TestCase):
    def setUp(self):
        self.app = create_app(_TestConfig)
        self.ctx = self.app.app_context(); self.ctx.push()
        db.create_all()
        db.session.add(Property(code='default', name='Test Property'))
        db.session.commit()
        self.admin = User(username='pa', email='a@x', role='admin')
        self.admin.set_password('aaaaaaaaaa1')
        db.session.add(self.admin); db.session.commit()
        self.t1 = self._type('STD', 'Standard', 2, 10)
        self.t2 = self._type('DLX', 'Deluxe', 1, 20)
        self.client = self.app.test_client()

    def tearDown(self):
        db.session.remove(); db.drop_all(); self.ctx.pop()

    def _type(self, code, name, rooms, base):
        rt = RoomType(code=code, name=name, max_occupancy=2, base_capacity=2, is_active=True)
        db.session.add(rt); db.session.commit()
        for i in range(rooms):
            db.session.add(Room(number=f'{base+i}', name='T', room_type=name,
                                room_type_id=rt.id, floor=1, capacity=2,
                                price_per_night=500.0, status='available',
                                housekeeping_status='clean'))
        db.session.commit()
        return rt

    def _admin_login(self):
        # fresh client = the admin actor (not carrying the guest's portal cookie)
        self.admin_client = self.app.test_client()
        with self.admin_client.session_transaction() as s:
            s['_user_id'] = str(self.admin.id); s['_fresh'] = True


class PortalEndpointFlow(unittest.TestCase):
    """Full guest flow through the ACTUAL /book endpoints. Uses a tmp-FILE DB and
    fresh app-contexts (not a persistent one) so test_client request commits are
    visible — the working pattern; a persistent :memory: context interleaves
    request/test sessions on one connection."""

    def setUp(self):
        self._fd, self._path = tempfile.mkstemp(suffix='.db')

        class _Cfg(_TestConfig):
            SQLALCHEMY_DATABASE_URI = f'sqlite:///{self._path}'
        self.app = create_app(_Cfg)
        with self.app.app_context():
            db.create_all()
            db.session.add(Property(code='default', name='P'))
            db.session.commit()
            ad = User(username='pa', email='a@x', role='admin')
            ad.set_password('aaaaaaaaaa1')
            db.session.add(ad); db.session.commit()
            self.admin_id = ad.id
            self.t1 = self._seed_type('STD', 'Standard', 2, 10)
            self.t2 = self._seed_type('DLX', 'Deluxe', 1, 20)
        self.client = self.app.test_client()

    def tearDown(self):
        with self.app.app_context():
            db.drop_all()
        os.close(self._fd)
        try:
            os.remove(self._path)
        except OSError:
            pass

    def _seed_type(self, code, name, n, base):
        rt = RoomType(code=code, name=name, max_occupancy=2, base_capacity=2, is_active=True)
        db.session.add(rt); db.session.commit()
        for i in range(n):
            db.session.add(Room(number=str(base + i), name='T', room_type=name,
                                room_type_id=rt.id, floor=1, capacity=2,
                                price_per_night=500.0, status='available',
                                housekeeping_status='clean'))
        db.session.commit()
        return rt.id

    def test_reupload_slip_supersedes_and_clears_rejection(self):
        from app.models import PepperOutbox
        with self.client.session_transaction() as s:      # own-hold via session token
            s['portal_token'] = 'reupload-sess-token'
        with self.app.app_context():
            g = Guest(first_name='A', last_name='B', phone='9', nationality='MDV')
            db.session.add(g); db.session.commit()
            db.session.add(Hold(
                session_token='reupload-sess-token', hold_type='pending', state='active',
                room_type_id=self.t1, qty=1, check_in_date=_CI, check_out_date=_CO,
                expires_at=datetime.utcnow() + timedelta(hours=6),
                adults=1, children=0, lead_guest_id=g.id,
                payment_slip_filename='old_rejected.jpg',
                slip_rejected_at=datetime.utcnow(), slip_rejected_reason='blurry'))
            db.session.commit()
        r = self.client.post(
            '/book/slip', content_type='multipart/form-data',
            data={'payment_slip': (io.BytesIO(b'\xff\xd8\xffNEWSLIP'), 'new.jpg')})
        self.assertEqual(r.status_code, 302)
        with self.app.app_context():
            h = Hold.query.filter_by(session_token='reupload-sess-token').first()
            self.assertNotEqual(h.payment_slip_filename, 'old_rejected.jpg')   # superseded
            self.assertTrue(h.payment_slip_filename.startswith('holdslip_'))
            self.assertIsNone(h.slip_rejected_at)                             # cleared
            self.assertIsNone(h.slip_rejected_reason)
            self.assertEqual(h.state, 'active')                              # hold survives
            self.assertGreaterEqual(                                        # fresh alert queued
                PepperOutbox.query.filter_by(event_type='slip.uploaded').count(), 1)
        updir = os.path.join(self.app.root_path, 'uploads')                # clean up written file
        for fn in (os.listdir(updir) if os.path.isdir(updir) else []):
            if fn.startswith('holdslip_'):
                try:
                    os.remove(os.path.join(updir, fn))
                except OSError:
                    pass

    def test_full_flow_search_to_confirmed_group(self):
        self.assertEqual(self.client.get(
            f'/book/?check_in={_CI}&check_out={_CO}').status_code, 200)
        r = self.client.post('/book/hold', data={
            'check_in': _CI.isoformat(), 'check_out': _CO.isoformat(),
            'guests': '2', f'qty_{self.t1}': '1', f'qty_{self.t2}': '1'})
        self.assertEqual(r.status_code, 302)
        self.assertIn('/book/guest', r.headers.get('Location', ''))
        self.assertEqual(self.client.get('/book/guest').status_code, 200)
        r = self.client.post('/book/submit', data={
            'first_name': 'Test', 'last_name': 'Guest', 'phone': '+9600000000',
            'nationality': 'MDV'})
        self.assertEqual(r.status_code, 302)
        self.assertIn('/book/status', r.headers.get('Location', ''))
        self.assertEqual(self.client.get('/book/status').status_code, 200)

        with self.app.app_context():
            self.assertEqual(Hold.query.filter_by(hold_type='pending', state='active').count(), 2)
            pend = Hold.query.filter_by(hold_type='pending', state='active').first()
            conf = holds_svc.confirm_group(pend.session_token, user_id=self.admin_id)
            self.assertTrue(conf['ok'], conf.get('reasons'))
            self.assertEqual(BookingGroup.query.count(), 1)
            grp = BookingGroup.query.first()
            self.assertIsNotNone(grp.master_booking_id)
            bks = Booking.query.filter_by(booking_group_id=grp.id).all()
            self.assertEqual(len(bks), 2)
            self.assertTrue(all(b.room_id and b.status == 'confirmed' for b in bks))
            self.assertEqual(Hold.query.filter_by(state='converted').count(), 2)

    def test_guest_summary_shows_rooms_breakdown_and_guest_split(self):
        # Regression guard: the guest step must show BOTH the per-type rooms
        # breakdown (not just a count) AND the adults/children guest split, so
        # the guest-count fields coexist with the room selection detail.
        self.client.get(f'/book/?check_in={_CI}&check_out={_CO}')
        r = self.client.post('/book/hold', data={
            'check_in': _CI.isoformat(), 'check_out': _CO.isoformat(),
            'guests': '3', f'qty_{self.t1}': '2', f'qty_{self.t2}': '1'})
        self.assertEqual(r.status_code, 302)
        html = self.client.get('/book/guest').get_data(as_text=True)
        self.assertIn('2× Standard', html)     # rooms breakdown, per type
        self.assertIn('1× Deluxe', html)
        self.assertIn('3 room(s)', html)        # total room count kept
        self.assertIn('name="adults"', html)    # guest-count fields still there
        self.assertIn('name="children"', html)
        self.assertIn('id="gbreak"', html)      # live "N adults, M children" split

    def _occ_config(self):
        # Standard -> base 2 / max 3 / fee 100 (Sheeza-like) for the endpoint tests
        with self.app.app_context():
            rt = RoomType.query.get(self.t1)
            rt.max_occupancy = 3; rt.base_occupancy = 2; rt.extra_person_fee = 100.0
            db.session.commit()

    def test_selection_endpoint_blocks_over_capacity(self):
        # 5 guests + 1 room (max 3) -> server rejects at /book/hold, no hold made
        self._occ_config()
        r = self.client.post('/book/hold', data={
            'check_in': _CI.isoformat(), 'check_out': _CO.isoformat(),
            'guests': '5', f'qty_{self.t1}': '1'})
        self.assertEqual(r.status_code, 302)
        self.assertNotIn('/book/guest', r.headers.get('Location', ''))  # bounced
        with self.app.app_context():
            self.assertEqual(Hold.query.filter_by(
                hold_type='selection', state='active').count(), 0)

    def test_selection_endpoint_allows_carries_guests_and_fee(self):
        # 3 guests + 1 room (max 3): allowed, guests carried, guest step pre-filled
        self._occ_config()
        r = self.client.post('/book/hold', data={
            'check_in': _CI.isoformat(), 'check_out': _CO.isoformat(),
            'guests': '3', f'qty_{self.t1}': '1'})
        self.assertEqual(r.status_code, 302)
        self.assertIn('/book/guest', r.headers.get('Location', ''))
        with self.app.app_context():
            h = Hold.query.filter_by(hold_type='selection', state='active').first()
            self.assertEqual(h.adults, 3)          # search-bar count carried
        html = self.client.get('/book/guest').get_data(as_text=True)
        self.assertIn('value="3"', html)           # adults pre-filled, not asked from 1
        self.assertIn('id="extrafee">200', html)   # 1 extra × 100 × 2 nights, shown up front

    def test_selection_endpoint_requires_guests(self):
        # guests is required (no default): missing -> rejected, no hold, bounced
        self._occ_config()
        r = self.client.post('/book/hold', data={
            'check_in': _CI.isoformat(), 'check_out': _CO.isoformat(),
            f'qty_{self.t1}': '1'})            # no 'guests'
        self.assertEqual(r.status_code, 302)
        self.assertNotIn('/book/guest', r.headers.get('Location', ''))
        with self.app.app_context():
            self.assertEqual(Hold.query.filter_by(
                hold_type='selection', state='active').count(), 0)
        # the flashed message shows on the next page
        self.assertIn('Please enter number of guests',
                      self.client.get('/book/').get_data(as_text=True))


class AdminConfirmRoute(_Base):
    """The admin confirm ROUTE (fresh admin client, no guest cookies) confirms a
    pending group created directly — covers the route auth + wiring."""
    def test_confirm_route_confirms_group(self):
        from app.services import portal as ps
        tok = 'route-token'
        ps.create_holds([{'room_type_id': self.t1.id, 'qty': 1},
                         {'room_type_id': self.t2.id, 'qty': 1}], _CI, _CO, tok)
        ps.submit(tok, {'first_name': 'R', 'last_name': 'T', 'phone': '+960',
                        'nationality': 'MDV'})
        pend = Hold.query.filter_by(hold_type='pending', state='active').first()
        c = self.app.test_client()
        with c.session_transaction() as s:
            s['_user_id'] = str(self.admin.id); s['_fresh'] = True
        self.assertEqual(c.get('/admin/holds/').status_code, 200)
        r = c.post(f'/admin/holds/{pend.id}/confirm')
        self.assertIn(r.status_code, (302, 200))
        db.session.remove()
        self.assertEqual(Hold.query.filter_by(state='converted').count(), 2)

    def assertin_redirect(self, resp, path):
        self.assertEqual(resp.status_code, 302)
        self.assertIn(path, resp.headers.get('Location', ''))


class PortalServiceRules(_Base):
    def test_single_type_qty1_makes_plain_booking_no_group(self):
        tok = 'tok-single'
        res = portal_svc.create_holds([{'room_type_id': self.t2.id, 'qty': 1}],
                                      _CI, _CO, tok)
        self.assertTrue(res['ok'])
        portal_svc.submit(tok, {'first_name': 'A', 'last_name': 'B', 'phone': '+9600',
                                'nationality': 'MDV'})
        conf = holds_svc.confirm_group(tok, user_id=self.admin.id)
        self.assertTrue(conf['ok'], conf.get('reasons'))
        self.assertIsNone(conf['group_id'])                  # plain booking, no group
        self.assertEqual(len(conf['booking_ids']), 1)
        self.assertEqual(BookingGroup.query.count(), 0)

    def test_anti_abuse_one_active_selection_group_per_session(self):
        tok = 'tok-reselect'
        portal_svc.create_holds([{'room_type_id': self.t1.id, 'qty': 1}], _CI, _CO, tok)
        first = holds_svc.holds_for_session(tok, hold_type='selection', state='active')
        self.assertEqual(len(first), 1)
        # re-select -> the prior selection is released (audited), only new remains
        portal_svc.create_holds([{'room_type_id': self.t1.id, 'qty': 2}], _CI, _CO, tok)
        active = holds_svc.holds_for_session(tok, hold_type='selection', state='active')
        self.assertEqual(len(active), 1)
        self.assertEqual(active[0].qty, 2)
        self.assertEqual(Hold.query.filter_by(state='released').count(), 1)

    def test_selection_expiry_blocks_submit(self):
        tok = 'tok-exp'
        portal_svc.create_holds([{'room_type_id': self.t1.id, 'qty': 1}], _CI, _CO, tok)
        for h in holds_svc.holds_for_session(tok, hold_type='selection', state='active'):
            h.expires_at = datetime.utcnow() - timedelta(seconds=1)
        db.session.commit()
        res = portal_svc.submit(tok, {'first_name': 'A', 'last_name': 'B', 'phone': '+960',
                                      'nationality': 'MDV'})
        self.assertFalse(res['ok'])                          # dead hold -> no submit

    def test_pending_expiry_status_is_rebook(self):
        tok = 'tok-pexp'
        portal_svc.create_holds([{'room_type_id': self.t1.id, 'qty': 1}], _CI, _CO, tok)
        portal_svc.submit(tok, {'first_name': 'A', 'last_name': 'B', 'phone': '+960',
                                'nationality': 'MDV'})
        for h in holds_svc.holds_for_session(tok, hold_type='pending', state='active'):
            h.expires_at = datetime.utcnow() - timedelta(seconds=1)
        db.session.commit()
        self.assertEqual(portal_svc.status(tok)['state'], 'expired')

    def test_slip_transfers_to_booking_on_confirm(self):
        tok = 'tok-slip'
        portal_svc.create_holds([{'room_type_id': self.t2.id, 'qty': 1}], _CI, _CO, tok)
        portal_svc.submit(tok, {'first_name': 'S', 'last_name': 'L', 'phone': '+960',
                                'nationality': 'MDV'},
                          slip_filename='slip123.png',
                          slip_drive_id='payment-slips/slip123.png')
        h = holds_svc.holds_for_session(tok, hold_type='pending', state='active')[0]
        self.assertEqual(h.payment_slip_filename, 'slip123.png')       # on the hold
        conf = holds_svc.confirm_group(tok, user_id=self.admin.id)
        self.assertTrue(conf['ok'], conf.get('reasons'))
        b = Booking.query.get(conf['booking_ids'][0])
        # slip reference handed off to the booking -> findable via the normal
        # (admin-gated) booking UI after the hold archives
        self.assertEqual(b.payment_slip_filename, 'slip123.png')
        self.assertEqual(b.payment_slip_drive_id, 'payment-slips/slip123.png')

    def test_capacity_rejection_over(self):
        # STD max_occupancy=2, qty 1 -> capacity 2
        tok = 'tok-cap-over'
        portal_svc.create_holds([{'room_type_id': self.t1.id, 'qty': 1}], _CI, _CO, tok)
        res = portal_svc.submit(tok, {'first_name': 'A', 'last_name': 'B',
                                      'phone': '+960', 'adults': '3'})
        self.assertFalse(res['ok'])
        # new occupancy message: per-room cap + minimum-rooms suggestion
        self.assertIn('A maximum of 2 guests can stay in one room', res['reasons'][0])
        self.assertIn('at least 2 rooms', res['reasons'][0])

    def test_capacity_boundary_equal_passes(self):
        tok = 'tok-cap-eq'
        portal_svc.create_holds([{'room_type_id': self.t1.id, 'qty': 1}], _CI, _CO, tok)
        res = portal_svc.submit(tok, {'first_name': 'A', 'last_name': 'B',
                                      'phone': '+960', 'adults': '2', 'children': '0',
                                      'nationality': 'MDV'})
        self.assertTrue(res['ok'], res.get('reasons'))
        h = holds_svc.holds_for_session(tok, hold_type='pending', state='active')[0]
        self.assertEqual((h.adults, h.children), (2, 0))

    def test_children_default_zero(self):
        tok = 'tok-ch0'
        portal_svc.create_holds([{'room_type_id': self.t1.id, 'qty': 1}], _CI, _CO, tok)
        res = portal_svc.submit(tok, {'first_name': 'A', 'last_name': 'B',
                                      'phone': '+960', 'adults': '2',        # no children
                                      'nationality': 'MDV'})
        self.assertTrue(res['ok'], res.get('reasons'))
        h = holds_svc.holds_for_session(tok, hold_type='pending', state='active')[0]
        self.assertEqual(h.children, 0)

    def test_counts_hand_off_to_group_and_booking(self):
        tok = 'tok-ghand'
        portal_svc.create_holds([{'room_type_id': self.t1.id, 'qty': 1},
                                 {'room_type_id': self.t2.id, 'qty': 1}], _CI, _CO, tok)
        portal_svc.submit(tok, {'first_name': 'A', 'last_name': 'B',
                                'phone': '+960', 'adults': '2', 'children': '1',
                                'nationality': 'MDV'})
        conf = holds_svc.confirm_group(tok, user_id=self.admin.id)
        self.assertTrue(conf['ok'], conf.get('reasons'))
        grp = BookingGroup.query.get(conf['group_id'])
        self.assertEqual((grp.adults, grp.children), (2, 1))    # per-group totals
        head = Booking.query.get(conf['booking_ids'][0])
        self.assertEqual((head.adults, head.children, head.num_guests), (2, 1, 3))

    def test_search_never_exposes_room_numbers(self):
        cards = portal_svc.search(_CI, _CO)
        # cards carry type + counts only; no room object/number leaks
        for c in cards:
            self.assertNotIn('room', {k.lower() for k in c.keys()} - {'room_type'})
            self.assertIn('available_qty', c)


class OccupancyPricing(_Base):
    """Occupancy capacity validation + cheapest-distribution extra-person fee
    + itemized folio line. Sheeza config: base 2 / max 3 / fee 100."""

    def setUp(self):
        super().setUp()
        # Standard -> Sheeza-like: base 2, max 3, fee 100 (t1 already has 10 rooms)
        self.t1.base_occupancy = 2; self.t1.max_occupancy = 3
        self.t1.extra_person_fee = 100.0
        # Family: base 3, max 4, fee 100 (mixed distribution / free 3rd guest)
        self.fam = self._type('FAM', 'Family', 4, 300)
        self.fam.base_occupancy = 3; self.fam.max_occupancy = 4
        self.fam.extra_person_fee = 100.0
        # Cheap: base 2, max 3, fee 50 (cheapest-first distribution)
        self.cheap = self._type('CHP', 'Cheap', 4, 400)
        self.cheap.base_occupancy = 2; self.cheap.max_occupancy = 3
        self.cheap.extra_person_fee = 50.0
        db.session.commit()

    def _occ(self, items, g, nights=2):
        from app.services import occupancy
        return occupancy.compute(items, g, nights)

    def test_capacity_block_message(self):
        from app.services import occupancy
        occ = self._occ([{'room_type_id': self.t1.id, 'qty': 1}], 4)  # max 3
        self.assertTrue(occ['over_capacity'])
        self.assertEqual(occ['max_per_room'], 3)
        self.assertEqual(occ['min_rooms'], 2)                          # ceil(4/3)
        self.assertEqual(
            occupancy.block_message(4, occ['max_per_room'], occ['min_rooms']),
            'A maximum of 3 guests can stay in one room. '
            'For 4 guests, please select at least 2 rooms.')

    def test_fee_boundaries_uniform(self):
        one = [{'room_type_id': self.t1.id, 'qty': 1}]   # base 2 / max 3 / 100, 2n
        self.assertEqual(self._occ(one, 2)['fee_total'], 0)            # G=2R -> 0
        occ3 = self._occ(one, 3)
        self.assertEqual(occ3['extras'], 1)
        self.assertEqual(occ3['fee_total'], 200)                      # 1×100×2n (max)
        self.assertFalse(occ3['over_capacity'])
        two = [{'room_type_id': self.t1.id, 'qty': 2}]
        self.assertEqual(self._occ(two, 4)['fee_total'], 0)           # G=2R -> 0
        self.assertEqual(self._occ(two, 5)['fee_total'], 200)         # G=2R+1 -> 1 fee
        self.assertEqual(self._occ(two, 6)['fee_total'], 400)         # G=3R -> max
        self.assertEqual(self._occ(two, 6)['extras'], 2)

    def test_mixed_family_absorbs_third_guest_free(self):
        items = [{'room_type_id': self.fam.id, 'qty': 1},   # base 3
                 {'room_type_id': self.t1.id, 'qty': 1}]    # base 2  => base_total 5
        self.assertEqual(self._occ(items, 5)['fee_total'], 0)   # 3rd Family guest free
        self.assertEqual(self._occ(items, 6)['fee_total'], 200) # 1 extra ×100×2n

    def test_cheapest_seat_charged_first(self):
        items = [{'room_type_id': self.t1.id, 'qty': 1},     # fee 100
                 {'room_type_id': self.cheap.id, 'qty': 1}]  # fee 50, base_total 4
        self.assertEqual(self._occ(items, 5, nights=1)['fee_total'], 50)   # cheap first
        self.assertEqual(self._occ(items, 6, nights=1)['fee_total'], 150)  # 50 + 100

    def test_folio_itemization_and_room_rate_untouched(self):
        from app.services import group_booking
        from app.models import FolioItem
        g = Guest(first_name='F', last_name='L', phone='+960')
        db.session.add(g); db.session.commit()
        res = group_booking.create_group_booking(
            [{'room_type_id': self.t1.id, 'qty': 1}], _CI, _CO,
            lead_guest=g, adults=3, children=0, force_group=False)   # 1 extra
        self.assertTrue(res['ok'], res.get('reasons'))
        bid = res['booking_ids'][0]
        fees = FolioItem.query.filter_by(booking_id=bid, item_type='fee').all()
        self.assertEqual(len(fees), 1)
        self.assertIn('Extra person fee', fees[0].description)
        self.assertEqual(fees[0].total_amount, 200)                 # 1 × 2n × 100
        # fee is NOT folded into the room revenue
        self.assertEqual(Booking.query.get(bid).total_amount or 0, 0)

    def test_portal_submit_blocks_over_capacity(self):
        tok = 'occ-token'
        self.assertTrue(portal_svc.create_holds(
            [{'room_type_id': self.t1.id, 'qty': 1}], _CI, _CO, tok)['ok'])
        res = portal_svc.submit(tok, {'first_name': 'A', 'last_name': 'B',
                                      'phone': '+960', 'adults': '4', 'children': '0'})
        self.assertFalse(res['ok'])
        self.assertIn('A maximum of 3 guests can stay in one room', res['reasons'][0])
        self.assertIn('at least 2 rooms', res['reasons'][0])


if __name__ == '__main__':
    unittest.main()
