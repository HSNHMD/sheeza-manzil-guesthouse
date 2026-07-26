"""Booking Engine V2 — the concurrency RACE test (spec §3, blocking).

Two concurrent sessions contend for the LAST room of a type on the same dates;
exactly ONE succeeds, the other gets a clean "no longer available". This proves
the DB-level enforcement (SELECT ... FOR UPDATE on the room_type row) actually
serializes acquisitions — an app-level check alone would let both through.

MUST run against Postgres — SQLite's locking cannot express this race, so the
test is a no-op there. CI provides Postgres via the env var below.

How the ephemeral DB is provided (documented per the brief):
  - CI sets `BEV2_TEST_PG_URL` to a Postgres URL for a THROWAWAY database
    (e.g. a `services: postgres` container, or a per-job created DB).
  - This test calls `db.create_all()` at setUp and `db.drop_all()` at tearDown,
    so it owns and cleans its own schema. It never touches a live/app database.
  - If `BEV2_TEST_PG_URL` is unset, the test SKIPS (with a clear message) so the
    SQLite suite still runs locally; CI must set it for the gate to be real.
"""

from __future__ import annotations

import os
import threading
import unittest
from datetime import date, timedelta

os.environ.setdefault('SECRET_KEY', 'test-secret-do-not-use-in-prod')
os.environ.pop('WHATSAPP_ENABLED', None)

_PG_URL = os.environ.get('BEV2_TEST_PG_URL')

from config import Config                                          # noqa: E402
from app import create_app                                        # noqa: E402
from app.models import db, User, Room, RoomType, Hold, Property   # noqa: E402
from app.services import holds                                    # noqa: E402

_CI = date.today() + timedelta(days=30)
_CO = _CI + timedelta(days=2)


class _PGConfig(Config):
    TESTING = True
    SQLALCHEMY_DATABASE_URI = _PG_URL or 'sqlite:///:memory:'
    WTF_CSRF_ENABLED = False
    WHATSAPP_ENABLED = False


@unittest.skipUnless(_PG_URL, 'BEV2_TEST_PG_URL not set — race test requires Postgres')
class RaceTest(unittest.TestCase):
    def setUp(self):
        self.app = create_app(_PGConfig)
        with self.app.app_context():
            # The ephemeral DB is created fresh by the harness/CI per run, so we
            # only create tables (idempotent). We do NOT drop_all: the schema has
            # a bookings<->booking_groups FK cycle that Postgres can't topo-sort
            # for DROP; dropping the whole *database* is the harness's job.
            db.create_all()
            db.session.add(Property(code='default', name='Race Property'))
            db.session.commit()   # property_id=1 for the FK on room_types
            admin = User(username='race_admin', email='r@x', role='admin')
            admin.set_password('aaaaaaaaaa1')
            db.session.add(admin)
            rt = RoomType(code='RACE', name='RaceType', max_occupancy=2,
                          base_capacity=2, is_active=True)
            db.session.add(rt)
            db.session.commit()
            db.session.add(Room(number='R1', name='T', room_type='RaceType',
                                room_type_id=rt.id, floor=1, capacity=2,
                                price_per_night=600.0, status='available',
                                housekeeping_status='clean'))
            db.session.commit()
            self.rt_id = rt.id

    def tearDown(self):
        with self.app.app_context():
            db.session.remove()   # DB itself is dropped by the harness/CI

    def test_exactly_one_wins_last_room(self):
        results = []
        barrier = threading.Barrier(2)

        def worker():
            # Each thread gets its own app context + scoped db session (its own
            # connection) — a genuine concurrent transaction.
            with self.app.app_context():
                barrier.wait()                     # line both up on the acquire
                try:
                    res = holds.acquire_selection_hold(self.rt_id, _CI, _CO, qty=1)
                    results.append(res['ok'])
                except Exception as exc:           # a serialization error also = loss
                    results.append(False)
                finally:
                    db.session.remove()

        t1 = threading.Thread(target=worker)
        t2 = threading.Thread(target=worker)
        t1.start(); t2.start()
        t1.join(timeout=30); t2.join(timeout=30)

        self.assertEqual(len(results), 2, 'both workers must return')
        self.assertEqual(sum(1 for ok in results if ok), 1,
                         f'exactly one acquire must win, got {results}')
        with self.app.app_context():
            self.assertEqual(
                Hold.query.filter_by(state='active').count(), 1,
                'exactly one active hold must exist')


@unittest.skipUnless(_PG_URL, 'BEV2_TEST_PG_URL not set — race test requires Postgres')
class RaceEndpointTest(unittest.TestCase):
    """Endpoint-level race (spec §B6): two guest SESSIONS POST /book/hold for the
    last room of a type; exactly one is redirected to /book/guest (got the hold),
    the other is bounced back to /book (friendly re-query). Postgres only."""

    def setUp(self):
        self.app = create_app(_PGConfig)
        with self.app.app_context():
            db.create_all()
            db.session.add(Property(code='default', name='Race Property'))
            db.session.commit()
            rt = RoomType(code='RACE', name='RaceType', max_occupancy=2,
                          base_capacity=2, is_active=True)
            db.session.add(rt); db.session.commit()
            db.session.add(Room(number='R1', name='T', room_type='RaceType',
                                room_type_id=rt.id, floor=1, capacity=2,
                                price_per_night=600.0, status='available',
                                housekeeping_status='clean'))
            db.session.commit()
            self.rt_id = rt.id

    def tearDown(self):
        with self.app.app_context():
            db.session.remove()

    def test_two_sessions_one_gets_the_hold(self):
        import threading
        got = []
        barrier = threading.Barrier(2)

        def worker():
            c = self.app.test_client()   # distinct session -> distinct token
            barrier.wait()
            r = c.post('/book/hold', data={
                'check_in': _CI.isoformat(), 'check_out': _CO.isoformat(),
                f'qty_{self.rt_id}': '1'})
            got.append('/book/guest' in r.headers.get('Location', ''))

        t1 = threading.Thread(target=worker); t2 = threading.Thread(target=worker)
        t1.start(); t2.start(); t1.join(30); t2.join(30)
        self.assertEqual(len(got), 2)
        self.assertEqual(sum(1 for x in got if x), 1,
                         f'exactly one session should get the hold, got {got}')
        with self.app.app_context():
            self.assertEqual(Hold.query.filter_by(state='active').count(), 1)


if __name__ == '__main__':
    unittest.main()
