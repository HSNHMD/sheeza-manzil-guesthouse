"""Tests for Reservation Board operational interactions:
move booking, extend stay, room blocks.

Hard rules covered:
  - Routes are admin-gated; anonymous + staff users blocked.
  - Validation: target room must exist; no overlap with other bookings;
    no overlap with active blocks. Dates must parse cleanly.
  - Booking status / payment status / room.status are NEVER mutated by
    these endpoints.
  - Every change writes a strict-whitelist audit row.
  - No WhatsApp / email / Gemini / R2 calls.
  - Removed blocks are excluded from conflict checks but kept for audit.
"""

from __future__ import annotations

import json
import os
import unittest
from datetime import date, datetime, timedelta
from unittest import mock

# Clean env BEFORE app import.
for _v in ('DATABASE_URL',
           'AI_DRAFT_PROVIDER', 'AI_DRAFT_MODEL',
           'GEMINI_API_KEY', 'ANTHROPIC_API_KEY',
           'WHATSAPP_ENABLED', 'WHATSAPP_TOKEN'):
    os.environ.pop(_v, None)
os.environ.setdefault('SECRET_KEY', 'test-secret-do-not-use-in-prod')

from config import Config                                       # noqa: E402
from app import create_app                                      # noqa: E402
from app.models import (                                        # noqa: E402
    db, User, Room, Guest, Booking, Invoice, ActivityLog,
    RoomBlock,
)
from app.services import board_actions                          # noqa: E402
from app.services import whatsapp as wa                         # noqa: E402
from app.services import ai_drafts                              # noqa: E402


class _TestConfig(Config):
    TESTING = True
    SQLALCHEMY_DATABASE_URI = 'sqlite:///:memory:'
    WTF_CSRF_ENABLED = False
    WHATSAPP_ENABLED = False


def _make_app():
    return create_app(_TestConfig)


def _seed_users():
    admin = User(username='admin1', email='a@x', role='admin')
    admin.set_password('a-very-strong-password-1!')
    staff = User(username='staff1', email='s@x', role='staff')
    staff.set_password('a-very-strong-password-1!')
    db.session.add_all([admin, staff])
    db.session.commit()
    return admin.id, staff.id


def _seed_room(num):
    r = Room(number=num, name=f'R{num}', room_type='Deluxe',
             floor=0, capacity=2, price_per_night=600.0)
    db.session.add(r)
    db.session.commit()
    return r


def _seed_guest():
    g = Guest(first_name='Hassan', last_name='Demo',
              phone='+9607000001', email='h@x')
    db.session.add(g)
    db.session.commit()
    return g


def _seed_booking(room, guest, *, ci, co, status='confirmed', ref='BK0001'):
    b = Booking(
        booking_ref=ref,
        room_id=room.id, guest_id=guest.id,
        check_in_date=ci, check_out_date=co,
        num_guests=1, total_amount=600.0 * (co - ci).days,
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
        self.guest = _seed_guest()
        self.room_a = _seed_room('101')
        self.room_b = _seed_room('102')
        self.room_c = _seed_room('103')
        self.client = self.app.test_client()

    def tearDown(self):
        db.session.remove()
        db.drop_all()
        self.ctx.pop()

    def _login(self, uid):
        with self.client.session_transaction() as sess:
            sess['_user_id'] = str(uid)
            sess['_fresh'] = True


# ─────────────────────────────────────────────────────────────────────
# 1) Pure helpers
# ─────────────────────────────────────────────────────────────────────

class OverlapHelperTests(unittest.TestCase):

    def test_overlap_half_open(self):
        d1, d2 = date(2026, 5, 1), date(2026, 5, 5)
        # Adjacent (5/5 ↔ 5/5) does NOT overlap (half-open)
        self.assertFalse(board_actions.overlaps(d1, d2,
                                                date(2026, 5, 5),
                                                date(2026, 5, 8)))
        # Overlapping by 1 day
        self.assertTrue(board_actions.overlaps(d1, d2,
                                               date(2026, 5, 4),
                                               date(2026, 5, 8)))
        # Fully inside
        self.assertTrue(board_actions.overlaps(d1, d2,
                                               date(2026, 5, 2),
                                               date(2026, 5, 4)))

    def test_parse_iso_date(self):
        self.assertEqual(board_actions.parse_iso_date('2026-05-01'),
                         date(2026, 5, 1))
        self.assertIsNone(board_actions.parse_iso_date('not a date'))
        self.assertIsNone(board_actions.parse_iso_date(''))


# ─────────────────────────────────────────────────────────────────────
# 2) Move booking — Phase A
# ─────────────────────────────────────────────────────────────────────

class MoveRoomAuthTests(_RouteBase):

    def test_anonymous_blocked(self):
        b = _seed_booking(self.room_a, self.guest,
                          ci=date(2026, 5, 1), co=date(2026, 5, 3))
        r = self.client.post(f'/bookings/{b.id}/move-room',
                             data={'new_room_id': self.room_b.id})
        self.assertIn(r.status_code, (301, 302, 401))
        b2 = Booking.query.get(b.id)
        self.assertEqual(b2.room_id, self.room_a.id)

    def test_staff_blocked(self):
        b = _seed_booking(self.room_a, self.guest,
                          ci=date(2026, 5, 1), co=date(2026, 5, 3))
        self._login(self.staff_id)
        r = self.client.post(f'/bookings/{b.id}/move-room',
                             data={'new_room_id': self.room_b.id})
        self.assertIn(r.status_code, (302, 401, 403))
        b2 = Booking.query.get(b.id)
        self.assertEqual(b2.room_id, self.room_a.id)


class MoveRoomValidationTests(_RouteBase):

    def setUp(self):
        super().setUp()
        self._login(self.admin_id)

    def test_invalid_target_room_rejected(self):
        b = _seed_booking(self.room_a, self.guest,
                          ci=date(2026, 5, 1), co=date(2026, 5, 3))
        r = self.client.post(f'/bookings/{b.id}/move-room',
                             data={'new_room_id': 'abc'})
        self.assertIn(r.status_code, (301, 302))
        b2 = Booking.query.get(b.id)
        self.assertEqual(b2.room_id, self.room_a.id)

    def test_unknown_target_room_rejected(self):
        b = _seed_booking(self.room_a, self.guest,
                          ci=date(2026, 5, 1), co=date(2026, 5, 3))
        r = self.client.post(f'/bookings/{b.id}/move-room',
                             data={'new_room_id': '99999'})
        self.assertIn(r.status_code, (301, 302))
        b2 = Booking.query.get(b.id)
        self.assertEqual(b2.room_id, self.room_a.id)

    def test_overlap_with_existing_booking_rejected(self):
        b1 = _seed_booking(self.room_a, self.guest,
                           ci=date(2026, 5, 1), co=date(2026, 5, 3),
                           ref='BKMOVE1')
        # Another booking on room B during the same dates
        g2 = Guest(first_name='Other', last_name='Guest',
                   phone='+9607000002', email='o@x')
        db.session.add(g2)
        db.session.commit()
        b2 = _seed_booking(self.room_b, g2,
                           ci=date(2026, 5, 2), co=date(2026, 5, 4),
                           ref='BKMOVE2')
        r = self.client.post(f'/bookings/{b1.id}/move-room',
                             data={'new_room_id': self.room_b.id})
        self.assertIn(r.status_code, (301, 302))
        # b1 still in room_a
        self.assertEqual(Booking.query.get(b1.id).room_id, self.room_a.id)

    def test_overlap_with_active_block_rejected(self):
        b = _seed_booking(self.room_a, self.guest,
                          ci=date(2026, 5, 1), co=date(2026, 5, 3),
                          ref='BKBLK')
        # Active block on room B during the same dates
        blk = RoomBlock(room_id=self.room_b.id,
                        start_date=date(2026, 4, 30),
                        end_date=date(2026, 5, 5),
                        reason='maintenance')
        db.session.add(blk)
        db.session.commit()
        r = self.client.post(f'/bookings/{b.id}/move-room',
                             data={'new_room_id': self.room_b.id})
        self.assertIn(r.status_code, (301, 302))
        self.assertEqual(Booking.query.get(b.id).room_id, self.room_a.id)


class MoveRoomSuccessTests(_RouteBase):

    def setUp(self):
        super().setUp()
        self._login(self.admin_id)

    def test_clean_move_succeeds(self):
        b = _seed_booking(self.room_a, self.guest,
                          ci=date(2026, 5, 1), co=date(2026, 5, 3),
                          ref='BKOK')
        r = self.client.post(f'/bookings/{b.id}/move-room',
                             data={'new_room_id': self.room_b.id})
        self.assertIn(r.status_code, (301, 302))
        b2 = Booking.query.get(b.id)
        self.assertEqual(b2.room_id, self.room_b.id)
        # Booking status preserved
        self.assertEqual(b2.status, 'confirmed')

    def test_move_writes_audit_row_with_safe_metadata(self):
        b = _seed_booking(self.room_a, self.guest,
                          ci=date(2026, 5, 1), co=date(2026, 5, 3),
                          ref='BKAUD')
        self.client.post(f'/bookings/{b.id}/move-room',
                         data={'new_room_id': self.room_b.id})
        rows = (ActivityLog.query
                .filter(ActivityLog.action == 'booking.room_moved').all())
        self.assertEqual(len(rows), 1)
        meta = json.loads(rows[0].metadata_json or '{}')
        self.assertEqual(meta.get('booking_id'), b.id)
        self.assertEqual(meta.get('booking_ref'), 'BKAUD')
        self.assertEqual(meta.get('old_room_id'), self.room_a.id)
        self.assertEqual(meta.get('new_room_id'), self.room_b.id)
        # Forbidden keys NOT present
        meta_blob = json.dumps(meta)
        self.assertNotIn('phone', meta_blob.lower())
        self.assertNotIn('passport', meta_blob.lower())

    def test_move_does_not_change_invoice_payment_status(self):
        b = _seed_booking(self.room_a, self.guest,
                          ci=date(2026, 5, 1), co=date(2026, 5, 3),
                          ref='BKINV')
        inv = Invoice(booking_id=b.id, invoice_number='INV-X',
                      total_amount=1200.0,
                      payment_status='verified', amount_paid=1200.0)
        db.session.add(inv); db.session.commit()
        before = inv.payment_status
        self.client.post(f'/bookings/{b.id}/move-room',
                         data={'new_room_id': self.room_b.id})
        self.assertEqual(Invoice.query.get(inv.id).payment_status, before)

    def test_move_does_not_call_whatsapp_or_gemini(self):
        b = _seed_booking(self.room_a, self.guest,
                          ci=date(2026, 5, 1), co=date(2026, 5, 3))
        with mock.patch.object(wa, 'send_text_message') as m_send, \
             mock.patch.object(ai_drafts, '_call_provider') as m_ai:
            self.client.post(f'/bookings/{b.id}/move-room',
                             data={'new_room_id': self.room_b.id})
        m_send.assert_not_called()
        m_ai.assert_not_called()


# ─────────────────────────────────────────────────────────────────────
# 3) Update stay — Phase B
# ─────────────────────────────────────────────────────────────────────

class UpdateStayValidationTests(_RouteBase):

    def setUp(self):
        super().setUp()
        self._login(self.admin_id)

    def test_invalid_date_rejected(self):
        b = _seed_booking(self.room_a, self.guest,
                          ci=date(2026, 5, 1), co=date(2026, 5, 3))
        r = self.client.post(f'/bookings/{b.id}/update-stay',
                             data={'new_check_out': 'garbage'})
        self.assertIn(r.status_code, (301, 302))
        self.assertEqual(Booking.query.get(b.id).check_out_date,
                         date(2026, 5, 3))

    def test_check_out_before_check_in_rejected(self):
        b = _seed_booking(self.room_a, self.guest,
                          ci=date(2026, 5, 5), co=date(2026, 5, 8))
        r = self.client.post(f'/bookings/{b.id}/update-stay',
                             data={'new_check_out': '2026-05-04'})
        self.assertIn(r.status_code, (301, 302))
        self.assertEqual(Booking.query.get(b.id).check_out_date,
                         date(2026, 5, 8))

    def test_overlap_with_following_booking_rejected(self):
        b1 = _seed_booking(self.room_a, self.guest,
                           ci=date(2026, 5, 1), co=date(2026, 5, 3),
                           ref='BKFIRST')
        g2 = Guest(first_name='Other', last_name='Guest',
                   phone='+9607000002', email='o@x')
        db.session.add(g2); db.session.commit()
        # Another booking starting where b1 ends
        b2 = _seed_booking(self.room_a, g2,
                           ci=date(2026, 5, 4), co=date(2026, 5, 6),
                           ref='BKAFTER')
        # Try to extend b1 into b2's window
        r = self.client.post(f'/bookings/{b1.id}/update-stay',
                             data={'new_check_out': '2026-05-05'})
        self.assertIn(r.status_code, (301, 302))
        self.assertEqual(Booking.query.get(b1.id).check_out_date,
                         date(2026, 5, 3))


class UpdateStaySuccessTests(_RouteBase):

    def setUp(self):
        super().setUp()
        self._login(self.admin_id)

    def test_extend_stay_succeeds(self):
        b = _seed_booking(self.room_a, self.guest,
                          ci=date(2026, 5, 1), co=date(2026, 5, 3),
                          ref='BKEXT')
        r = self.client.post(f'/bookings/{b.id}/update-stay',
                             data={'new_check_out': '2026-05-05'})
        self.assertIn(r.status_code, (301, 302))
        self.assertEqual(Booking.query.get(b.id).check_out_date,
                         date(2026, 5, 5))

    def test_shorten_stay_succeeds(self):
        b = _seed_booking(self.room_a, self.guest,
                          ci=date(2026, 5, 1), co=date(2026, 5, 5),
                          ref='BKSHRT')
        r = self.client.post(f'/bookings/{b.id}/update-stay',
                             data={'new_check_out': '2026-05-03'})
        self.assertIn(r.status_code, (301, 302))
        self.assertEqual(Booking.query.get(b.id).check_out_date,
                         date(2026, 5, 3))

    def test_audit_includes_old_and_new(self):
        b = _seed_booking(self.room_a, self.guest,
                          ci=date(2026, 5, 1), co=date(2026, 5, 3),
                          ref='BKSTAYAUD')
        self.client.post(f'/bookings/{b.id}/update-stay',
                         data={'new_check_out': '2026-05-05'})
        row = (ActivityLog.query
               .filter(ActivityLog.action == 'booking.stay_updated').first())
        self.assertIsNotNone(row)
        meta = json.loads(row.metadata_json or '{}')
        self.assertEqual(meta.get('old_check_out'), '2026-05-03')
        self.assertEqual(meta.get('new_check_out'), '2026-05-05')

    def test_does_not_change_status(self):
        b = _seed_booking(self.room_a, self.guest,
                          ci=date(2026, 5, 1), co=date(2026, 5, 3),
                          status='confirmed')
        self.client.post(f'/bookings/{b.id}/update-stay',
                         data={'new_check_out': '2026-05-05'})
        self.assertEqual(Booking.query.get(b.id).status, 'confirmed')


# ─────────────────────────────────────────────────────────────────────
# 4) Room blocks — Phase C
# ─────────────────────────────────────────────────────────────────────

class CreateBlockAuthTests(_RouteBase):

    def test_anonymous_blocked(self):
        r = self.client.post(
            f'/board/rooms/{self.room_a.id}/blocks',
            data={'start_date': '2026-05-01', 'end_date': '2026-05-03',
                  'reason': 'maintenance'},
        )
        self.assertIn(r.status_code, (301, 302, 401))
        self.assertEqual(RoomBlock.query.count(), 0)

    def test_staff_blocked(self):
        self._login(self.staff_id)
        r = self.client.post(
            f'/board/rooms/{self.room_a.id}/blocks',
            data={'start_date': '2026-05-01', 'end_date': '2026-05-03',
                  'reason': 'maintenance'},
        )
        self.assertIn(r.status_code, (302, 401, 403))
        self.assertEqual(RoomBlock.query.count(), 0)


class CreateBlockBehaviorTests(_RouteBase):

    def setUp(self):
        super().setUp()
        self._login(self.admin_id)

    def test_clean_block_creates_row(self):
        r = self.client.post(
            f'/board/rooms/{self.room_a.id}/blocks',
            data={'start_date': '2026-05-01', 'end_date': '2026-05-03',
                  'reason': 'maintenance'},
        )
        self.assertIn(r.status_code, (301, 302))
        blocks = RoomBlock.query.all()
        self.assertEqual(len(blocks), 1)
        self.assertEqual(blocks[0].room_id, self.room_a.id)
        self.assertEqual(blocks[0].start_date, date(2026, 5, 1))
        self.assertEqual(blocks[0].end_date, date(2026, 5, 3))
        self.assertEqual(blocks[0].reason, 'maintenance')
        self.assertIsNone(blocks[0].removed_at)

    def test_block_overlapping_active_booking_rejected(self):
        _seed_booking(self.room_a, self.guest,
                      ci=date(2026, 5, 1), co=date(2026, 5, 3),
                      status='confirmed')
        r = self.client.post(
            f'/board/rooms/{self.room_a.id}/blocks',
            data={'start_date': '2026-05-02', 'end_date': '2026-05-05',
                  'reason': 'maintenance'},
        )
        self.assertIn(r.status_code, (301, 302))
        self.assertEqual(RoomBlock.query.count(), 0)

    def test_block_writes_audit_row(self):
        self.client.post(
            f'/board/rooms/{self.room_a.id}/blocks',
            data={'start_date': '2026-05-01', 'end_date': '2026-05-03',
                  'reason': 'owner_hold'},
        )
        row = (ActivityLog.query
               .filter(ActivityLog.action == 'room.block_created').first())
        self.assertIsNotNone(row)
        meta = json.loads(row.metadata_json or '{}')
        self.assertEqual(meta.get('room_id'), self.room_a.id)
        self.assertEqual(meta.get('reason'), 'owner_hold')


class RemoveBlockTests(_RouteBase):

    def setUp(self):
        super().setUp()
        self._login(self.admin_id)

    def test_remove_marks_block_removed(self):
        blk = RoomBlock(room_id=self.room_a.id,
                        start_date=date(2026, 5, 1),
                        end_date=date(2026, 5, 3),
                        reason='maintenance')
        db.session.add(blk); db.session.commit()
        bid = blk.id
        r = self.client.post(f'/board/blocks/{bid}/remove')
        self.assertIn(r.status_code, (301, 302))
        blk2 = RoomBlock.query.get(bid)
        self.assertIsNotNone(blk2.removed_at)

    def test_removed_block_doesnt_block_new_booking(self):
        # Remove a block, then verify a booking can be made over its dates
        blk = RoomBlock(room_id=self.room_a.id,
                        start_date=date(2026, 5, 1),
                        end_date=date(2026, 5, 5),
                        reason='maintenance')
        db.session.add(blk); db.session.commit()
        self.client.post(f'/board/blocks/{blk.id}/remove')

        conflict = board_actions.check_room_block_conflict(
            room_id=self.room_a.id,
            start_date=date(2026, 5, 1),
            end_date=date(2026, 5, 5),
        )
        self.assertIsNone(conflict)

    def test_remove_writes_audit_row(self):
        blk = RoomBlock(room_id=self.room_a.id,
                        start_date=date(2026, 5, 1),
                        end_date=date(2026, 5, 3),
                        reason='maintenance')
        db.session.add(blk); db.session.commit()
        self.client.post(f'/board/blocks/{blk.id}/remove')
        row = (ActivityLog.query
               .filter(ActivityLog.action == 'room.block_removed').first())
        self.assertIsNotNone(row)


# ─────────────────────────────────────────────────────────────────────
# 5) Board still renders + integration smoke
# ─────────────────────────────────────────────────────────────────────

class BoardRenderingWithBlocksTests(_RouteBase):

    def setUp(self):
        super().setUp()
        self._login(self.admin_id)

    def test_board_renders_block_bar(self):
        today = date.today()
        blk = RoomBlock(room_id=self.room_a.id,
                        start_date=today,
                        end_date=today + timedelta(days=2),
                        reason='maintenance')
        db.session.add(blk); db.session.commit()
        r = self.client.get('/board')
        self.assertEqual(r.status_code, 200)
        self.assertIn(b'class="bar-block', r.data)
        self.assertIn(b'Maintenance', r.data)

    def test_board_modals_present(self):
        r = self.client.get('/board')
        self.assertEqual(r.status_code, 200)
        self.assertIn(b'id="modal-move-room"', r.data)
        self.assertIn(b'id="modal-extend-stay"', r.data)
        self.assertIn(b'id="modal-block-room"', r.data)
        self.assertIn(b'id="modal-remove-block"', r.data)

    def test_block_modal_lists_all_rooms(self):
        r = self.client.get('/board')
        # The select inside #modal-block-room should list all rooms
        self.assertIn(b'id="blockRoomSelect"', r.data)
        # 3 rooms seeded → at least 3 options
        self.assertIn(b'#101', r.data)
        self.assertIn(b'#102', r.data)
        self.assertIn(b'#103', r.data)


if __name__ == '__main__':
    unittest.main()
