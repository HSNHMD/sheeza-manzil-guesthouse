"""Tests for the Reservation Board density overhaul.

Pins the tightened constants + the new helpers so any future drift
is caught early. Three groups of tests:

  - DensityKnobsTests        — the tightened multipliers + room-rail
                               + row-height tables produce the
                               expected pixel values.
  - RoomTypeShortTests       — the 3-letter abbreviation helper maps
                               common types correctly and falls back
                               cleanly for unknown ones.
  - BoardRendersAtEachDensityTests
                             — /board?density=<each> renders 200 with
                               the expected --room-w / --row-h values
                               in the inline CSS, and exposes the
                               focus-toggle button.
"""

from __future__ import annotations

import os
import re
import unittest

for _v in ('DATABASE_URL', 'AI_DRAFT_PROVIDER', 'AI_DRAFT_MODEL',
           'GEMINI_API_KEY', 'ANTHROPIC_API_KEY',
           'WHATSAPP_ENABLED', 'WHATSAPP_TOKEN'):
    os.environ.pop(_v, None)
os.environ.setdefault('SECRET_KEY', 'test-secret-do-not-use-in-prod')

from config import Config                           # noqa: E402
from app import create_app                          # noqa: E402
from app.models import db, User                     # noqa: E402


class _TestConfig(Config):
    TESTING = True
    SQLALCHEMY_DATABASE_URI = 'sqlite:///:memory:'
    WTF_CSRF_ENABLED = False
    WHATSAPP_ENABLED = False


# ── Density knobs ─────────────────────────────────────────────────

class DensityKnobsTests(unittest.TestCase):
    """The tightened multipliers / row heights / rail widths must all
    fall in the documented bands. If a future edit relaxes any of
    them the test catches it immediately."""

    def test_day_width_multipliers_tightened(self):
        from app.services.board import DENSITY_DAY_WIDTH_MULT
        # Standard untouched.
        self.assertEqual(DENSITY_DAY_WIDTH_MULT['standard'], 1.00)
        # Compact tightened from 0.75 → ≤ 0.72.
        self.assertLessEqual(DENSITY_DAY_WIDTH_MULT['compact'], 0.72)
        # Ultra tightened from 0.55 → ≤ 0.45.
        self.assertLessEqual(DENSITY_DAY_WIDTH_MULT['ultra'], 0.45)

    def test_row_heights_tightened(self):
        # Vertical-density sprint pushed these tighter so significantly
        # more rooms fit per viewport. Bands chosen so future drift
        # back toward roomy values fails this test.
        from app.services.board import DENSITY_ROW_HEIGHT_PX
        # Standard stays comfortable for management review.
        self.assertLessEqual(DENSITY_ROW_HEIGHT_PX['standard'], 50)
        self.assertGreaterEqual(DENSITY_ROW_HEIGHT_PX['standard'], 44)
        # Compact: ~70% more rooms per screen than standard.
        self.assertLessEqual(DENSITY_ROW_HEIGHT_PX['compact'], 30)
        # Ultra: maximum density, but keep ≥ 20 px so colored
        # bars + payment dots remain visible.
        self.assertLessEqual(DENSITY_ROW_HEIGHT_PX['ultra'], 24)
        self.assertGreaterEqual(DENSITY_ROW_HEIGHT_PX['ultra'], 20)

    def test_room_rail_widths_tightened(self):
        from app.services.board import (
            DENSITY_ROOM_RAIL_PX, density_room_rail_px,
        )
        self.assertEqual(DENSITY_ROOM_RAIL_PX['standard'], 232)
        self.assertLessEqual(DENSITY_ROOM_RAIL_PX['compact'], 180)
        self.assertLessEqual(DENSITY_ROOM_RAIL_PX['ultra'], 110)
        self.assertEqual(density_room_rail_px('ultra'),
                         DENSITY_ROOM_RAIL_PX['ultra'])

    def test_effective_day_width_clamped_at_18(self):
        from app.services.board import effective_day_width_px
        # 30d × ultra rounds to 18 px (44 × 0.42 ≈ 18); we must never
        # drop below the 18-px floor that keeps day labels legible.
        self.assertGreaterEqual(effective_day_width_px('30d', 'ultra'), 18)


# ── room_type_short ───────────────────────────────────────────────

class RoomTypeShortTests(unittest.TestCase):

    def test_known_types_map_to_curated_codes(self):
        from app.services.board import room_type_short
        self.assertEqual(room_type_short('Standard'),       'STD')
        self.assertEqual(room_type_short('Standard Room'),  'STD')
        self.assertEqual(room_type_short('Deluxe Room'),    'DLX')
        self.assertEqual(room_type_short('Twin Room'),      'TWN')
        self.assertEqual(room_type_short('Family Room'),    'FAM')
        self.assertEqual(room_type_short('Suite'),          'STE')

    def test_unknown_type_falls_back_to_first_three_letters(self):
        from app.services.board import room_type_short
        self.assertEqual(room_type_short('Bungalow'),       'BUN')
        self.assertEqual(room_type_short('villa-on-water'), 'VIL')
        self.assertEqual(room_type_short('OVERWATER'),      'OVE')

    def test_empty_input_returns_placeholder(self):
        from app.services.board import room_type_short
        self.assertEqual(room_type_short(''), '???')
        self.assertEqual(room_type_short(None), '???')


# ── End-to-end render at each density ─────────────────────────────

class BoardRendersAtEachDensityTests(unittest.TestCase):

    def setUp(self):
        self.app = create_app(_TestConfig)
        self.ctx = self.app.app_context()
        self.ctx.push()
        db.create_all()
        admin = User(username='admin', email='a@x', role='admin')
        admin.set_password('aaaaaaaaaa1')
        db.session.add(admin)
        db.session.commit()
        self.client = self.app.test_client()
        with self.client.session_transaction() as sess:
            sess['_user_id'] = str(admin.id)
            sess['_fresh'] = True

    def tearDown(self):
        db.session.remove()
        db.drop_all()
        self.ctx.pop()

    def test_each_density_renders_with_expected_room_w(self):
        # Pull the inline `--room-w: NNNpx` from each rendered page
        # and assert the value matches DENSITY_ROOM_RAIL_PX.
        from app.services.board import DENSITY_ROOM_RAIL_PX
        for density, expected in DENSITY_ROOM_RAIL_PX.items():
            with self.subTest(density=density):
                r = self.client.get(f'/board?density={density}')
                self.assertEqual(r.status_code, 200)
                m = re.search(rb'--room-w:\s*(\d+)px', r.data)
                self.assertIsNotNone(m,
                    f'--room-w not found in /board?density={density}')
                self.assertEqual(int(m.group(1)), expected)

    def test_focus_toggle_button_present(self):
        r = self.client.get('/board?density=standard')
        self.assertEqual(r.status_code, 200)
        self.assertIn(b'id="focusToggleBtn"', r.data)
        self.assertIn(b'aria-pressed="false"', r.data)

    def test_ultra_hides_room_meta_via_data_attr(self):
        # The cascade rule:
        #   .board-grid[data-density="ultra"] .room-cell .room-meta { display:none }
        # is in the inline <style>; just confirm the data attribute
        # is set so the rule applies.
        r = self.client.get('/board?density=ultra')
        self.assertEqual(r.status_code, 200)
        self.assertIn(b'data-density="ultra"', r.data)
        # Ultra page width math: --room-w should be the tight value
        m = re.search(rb'--room-w:\s*(\d+)px', r.data)
        self.assertIsNotNone(m)
        self.assertLessEqual(int(m.group(1)), 110)


if __name__ == '__main__':
    unittest.main()
