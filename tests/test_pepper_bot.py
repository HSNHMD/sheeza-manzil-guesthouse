"""Pepper bot skeleton — acceptance tests (no live Telegram).

Locks in the Phase 1 skeleton behaviour:
  * /myid answers ANYONE (pre-whitelist, id-display only)
  * /ping answers ONLY whitelisted ids
  * an unlisted user gets TOTAL SILENCE (no reply, no error)
  * the gate fails CLOSED when the internal API is unreachable
  * owner (env) is authorized without any API call
  * whitelist result is cached (the 60s TTL)
"""

from __future__ import annotations

import unittest
from unittest import IsolatedAsyncioTestCase
from unittest.mock import AsyncMock, MagicMock

import os
import tempfile

import pytest
pytest.importorskip("telegram")   # the bot tests run in the bot venv (PTB present)

from pepper_bot import handlers as _h                            # noqa: E402
from pepper_bot.handlers import (cmd_myid, make_ping_handler, make_whitelist_gate,   # noqa: E402
                                 make_bindtopics_handler, make_topics_handler,
                                 make_action_callback, make_reject_reason_handler,
                                 make_reason_command_handler, reason_menu_keyboard,
                                 REJECT_REASONS, _reject_timeout,
                                 make_authorize_handler, make_revoke_handler,
                                 verify_keyboard, cash_keyboard)

_h._TIMEOUTS_ENABLED = False   # don't spawn real 120s timers during unit tests
from pepper_bot.gate import resolve_access                       # noqa: E402
from pepper_bot.internal_api import _TTLCache                    # noqa: E402
from pepper_bot.topics import TopicStore                         # noqa: E402
from pepper_bot.msgids import MsgIdStore                         # noqa: E402
from pepper_bot.alerts import format_booking_created, format_slip_caption  # noqa: E402
from pepper_bot.poller import Poller                             # noqa: E402

_HAS_PTB = True


def fake_update(uid, text="", chat_id=None, thread_id=None):
    u = MagicMock()
    u.effective_user.id = uid
    u.effective_chat.id = chat_id
    u.effective_message.text = text
    u.effective_message.message_thread_id = thread_id
    u.effective_message.reply_text = AsyncMock()
    return u


class FakeCtx:
    def __init__(self, args=None):
        self.args = args or []


class FakeClient:
    def __init__(self, allowed, role=None, raise_exc=False):
        self.allowed, self.role, self.raise_exc = allowed, role, raise_exc
        self.calls = 0

    async def whitelist(self, telegram_id):
        self.calls += 1
        if self.raise_exc:
            raise RuntimeError("internal API unreachable")
        return {"allowed": self.allowed, "role": self.role}


class MyIdTest(IsolatedAsyncioTestCase):
    async def test_myid_answers_anyone(self):
        upd = fake_update(999)               # unlisted
        await cmd_myid(upd, None)
        upd.effective_message.reply_text.assert_awaited_once()
        self.assertIn("999", upd.effective_message.reply_text.await_args.args[0])


class PingTest(IsolatedAsyncioTestCase):
    async def test_ping_answers_whitelisted(self):
        h = make_ping_handler(FakeClient(True, "staff"), owner_id=None)
        upd = fake_update(111)
        await h(upd, None)
        upd.effective_message.reply_text.assert_awaited_once_with("pong")

    async def test_ping_total_silence_for_unlisted(self):
        h = make_ping_handler(FakeClient(False), owner_id=None)
        upd = fake_update(999)
        await h(upd, None)
        upd.effective_message.reply_text.assert_not_awaited()   # silence

    async def test_ping_fail_closed_on_api_error(self):
        h = make_ping_handler(FakeClient(False, raise_exc=True), owner_id=None)
        upd = fake_update(999)
        await h(upd, None)
        upd.effective_message.reply_text.assert_not_awaited()   # silence, not error

    async def test_owner_answers_without_api_call(self):
        c = FakeClient(False)                 # API would deny
        h = make_ping_handler(c, owner_id="777")
        upd = fake_update(777)
        await h(upd, None)
        upd.effective_message.reply_text.assert_awaited_once_with("pong")
        self.assertEqual(c.calls, 0)          # owner short-circuits


class GateTest(IsolatedAsyncioTestCase):
    async def test_myid_passes_gate_without_api(self):
        c = FakeClient(False)
        gate = make_whitelist_gate(c, owner_id=None)
        await gate(fake_update(999, text="/myid"), None)   # no raise
        self.assertEqual(c.calls, 0)

    @unittest.skipUnless(_HAS_PTB, "python-telegram-bot not installed (bot venv only)")
    async def test_gate_blocks_unlisted_silently(self):
        from telegram.ext import ApplicationHandlerStop
        gate = make_whitelist_gate(FakeClient(False), owner_id=None)
        with self.assertRaises(ApplicationHandlerStop):
            await gate(fake_update(999, text="/ping"), None)

    async def test_gate_allows_whitelisted(self):
        gate = make_whitelist_gate(FakeClient(True), owner_id=None)
        await gate(fake_update(111, text="/ping"), None)     # no raise


class ResolveAccessTest(IsolatedAsyncioTestCase):
    async def test_fail_closed_on_exception(self):
        allowed, role = await resolve_access(
            FakeClient(True, raise_exc=True), owner_id=None, telegram_id=5)
        self.assertFalse(allowed)
        self.assertIsNone(role)


class BindTopicsTest(IsolatedAsyncioTestCase):
    def _store(self):
        self._tmp = tempfile.mkdtemp()
        return TopicStore(os.path.join(self._tmp, "topics.json"))

    async def test_owner_binds_topic(self):
        store = self._store()
        h = make_bindtopics_handler(FakeClient(False), owner_id="135", store=store)
        upd = fake_update(135, chat_id=-1001, thread_id=42)
        await h(upd, FakeCtx(args=["alerts"]))
        self.assertEqual(store.get("alerts"), {"chat_id": -1001, "thread_id": 42})
        upd.effective_message.reply_text.assert_awaited_once()

    async def test_non_owner_silent_and_no_write(self):
        store = self._store()
        # whitelisted STAFF (not owner) -> silence, nothing stored
        h = make_bindtopics_handler(FakeClient(True, "staff"), owner_id=None, store=store)
        upd = fake_update(222, chat_id=-1001, thread_id=42)
        await h(upd, FakeCtx(args=["alerts"]))
        upd.effective_message.reply_text.assert_not_awaited()
        self.assertEqual(store.all(), {})

    async def test_bad_label_rejected(self):
        store = self._store()
        h = make_bindtopics_handler(FakeClient(False), owner_id="135", store=store)
        upd = fake_update(135, chat_id=-1001, thread_id=42)
        await h(upd, FakeCtx(args=["bogus"]))
        self.assertEqual(store.all(), {})
        upd.effective_message.reply_text.assert_awaited_once()   # usage hint

    async def test_no_topic_context_rejected(self):
        store = self._store()
        h = make_bindtopics_handler(FakeClient(False), owner_id="135", store=store)
        upd = fake_update(135, chat_id=-1001, thread_id=None)     # not in a topic
        await h(upd, FakeCtx(args=["alerts"]))
        self.assertEqual(store.all(), {})

    async def test_topics_lists_bindings(self):
        store = self._store()
        store.set_topic("alerts", -1001, 42)
        h = make_topics_handler(FakeClient(False), owner_id="135", store=store)
        upd = fake_update(135)
        await h(upd, FakeCtx())
        upd.effective_message.reply_text.assert_awaited_once()
        self.assertIn("alerts", upd.effective_message.reply_text.await_args.args[0])


class TopicStoreTest(unittest.TestCase):
    def test_roundtrip_and_atomic(self):
        d = tempfile.mkdtemp()
        s = TopicStore(os.path.join(d, "topics.json"))
        self.assertEqual(s.all(), {})
        s.set_topic("alerts", -100, 7)
        s.set_topic("general", -100, 9)
        self.assertEqual(s.get("alerts"), {"chat_id": -100, "thread_id": 7})
        self.assertEqual(set(s.all().keys()), {"alerts", "general"})
        # reload from a fresh instance (persisted to disk)
        self.assertEqual(TopicStore(os.path.join(d, "topics.json")).get("general"),
                         {"chat_id": -100, "thread_id": 9})


_ALERT = {"source": "portal", "ref": "ABC12345", "guest_name": "Ahmed Hassan",
          "nationality": "MDV", "green_tax": "exempt", "check_in": "2026-09-05",
          "check_out": "2026-09-07", "nights": 2, "rooms": "1× Deluxe",
          "adults": 2, "children": 0, "total": 1200.0,
          "deadline": "2026-08-01T17:10:00", "has_slip": False}


class AlertFormatTest(unittest.TestCase):
    def test_booking_created_fields(self):
        t = format_booking_created(_ALERT)
        for frag in ("#ABC12345", "WEB PORTAL", "Ahmed Hassan",
                     "Green Tax exempt", "2026-09-05", "1× Deluxe",
                     "MVR 1200", "Aug 01, 17:10 UTC", "awaiting slip"):
            self.assertIn(frag, t)

    def test_no_pii_in_alert(self):
        t = format_booking_created({**_ALERT, "id_number": "A1234567"}).lower()
        self.assertNotIn("passport", t)
        self.assertNotIn("a1234567", t)   # formatter never reads id fields

    def test_has_slip_toggle(self):
        self.assertIn("slip uploaded",
                      format_booking_created({**_ALERT, "has_slip": True}))

    def test_expired_hold_not_actionable(self):
        t = format_booking_created({**_ALERT, "expired": True})
        self.assertIn("Hold EXPIRED", t)
        self.assertNotIn("Hold expires:", t)   # never reads as still open

    def test_slip_caption(self):
        c = format_slip_caption("ABC12345", 1200.0)
        self.assertIn("#ABC12345", c)
        self.assertIn("MVR 1200", c)


class FakeOutboxClient:
    def __init__(self, events, slip=None):
        self.events = events
        self.slip = slip
        self.marked = []

    async def outbox_undelivered(self):
        return self.events

    async def mark_delivered(self, eid):
        self.marked.append(eid)
        return True

    async def slip_bytes(self, reference=None, booking_id=None):
        return self.slip


class FakeBot:
    def __init__(self, message_id=555, fail=False):
        self.message_id, self.fail = message_id, fail
        self.messages, self.photos = [], []

    async def send_message(self, **kw):
        if self.fail:
            raise RuntimeError("telegram unreachable")
        self.messages.append(kw)
        m = MagicMock(); m.message_id = self.message_id; return m

    async def send_photo(self, **kw):
        self.photos.append(kw)
        m = MagicMock(); m.message_id = self.message_id + 1; return m


def _stores():
    d = tempfile.mkdtemp()
    topics = TopicStore(os.path.join(d, "t.json"))
    msgids = MsgIdStore(os.path.join(d, "m.json"))
    return topics, msgids


def _now_iso():
    from datetime import datetime
    return datetime.utcnow().isoformat()


def _old_iso(seconds):
    from datetime import datetime, timedelta
    return (datetime.utcnow() - timedelta(seconds=seconds)).isoformat()


class PollerTest(IsolatedAsyncioTestCase):
    async def test_booking_created_posted_and_marked(self):
        topics, msgids = _stores()
        topics.set_topic("alerts", -100, 7)
        ev = {"id": 1, "event_type": "booking.created", "reference": "ABC12345",
              "booking_id": None, "alert": _ALERT, "created_at": _now_iso()}
        bot, client = FakeBot(555), FakeOutboxClient([ev])
        n = await Poller(client, topics, msgids).poll_once(bot)
        self.assertEqual(n, 1)
        self.assertEqual(len(bot.messages), 1)
        self.assertEqual(bot.messages[0]["message_thread_id"], 7)
        self.assertIn("Ahmed Hassan", bot.messages[0]["text"])
        self.assertIsNone(bot.messages[0].get("reply_markup"))   # NO buttons on booking
        self.assertEqual(client.marked, [1])
        self.assertEqual(msgids.get("ABC12345"), 555)   # remembered for slip

    async def test_cash_booking_created_carries_cash_received_button(self):
        # A CASH booking.created alert gets the manager-gated 💵 Cash received
        # button (no slip is coming); a bank-transfer one does NOT.
        topics, msgids = _stores()
        topics.set_topic("alerts", -100, 7)
        cash_alert = {**_ALERT, "source": "bot", "booking_id": 42,
                      "payment_method": "cash", "ref": "BKCASH01"}
        ev = {"id": 8, "event_type": "booking.created", "reference": None,
              "booking_id": 42, "alert": cash_alert, "created_at": _now_iso()}
        bot, client = FakeBot(600), FakeOutboxClient([ev])
        await Poller(client, topics, msgids).poll_once(bot)
        mk = bot.messages[0].get("reply_markup")
        self.assertIsNotNone(mk)                                  # cash -> button
        self.assertEqual(mk.inline_keyboard[0][0].callback_data, "pv:cash:b:42")

    async def test_bank_booking_created_has_no_button(self):
        topics, msgids = _stores()
        topics.set_topic("alerts", -100, 7)
        bank_alert = {**_ALERT, "source": "bot", "booking_id": 43,
                      "payment_method": "bank_transfer", "ref": "BKBANK01"}
        ev = {"id": 9, "event_type": "booking.created", "reference": None,
              "booking_id": 43, "alert": bank_alert, "created_at": _now_iso()}
        bot, client = FakeBot(601), FakeOutboxClient([ev])
        await Poller(client, topics, msgids).poll_once(bot)
        self.assertIsNone(bot.messages[0].get("reply_markup"))    # bank -> no button

    async def test_slip_threaded_reply(self):
        topics, msgids = _stores()
        topics.set_topic("alerts", -100, 7)
        msgids.set("ABC12345", 555)                      # original alert msg id
        ev = {"id": 2, "event_type": "slip.uploaded", "reference": "ABC12345",
              "booking_id": None, "alert": {"ref": "ABC12345", "total": 1200.0},
              "created_at": _now_iso()}
        bot = FakeBot(555)
        client = FakeOutboxClient([ev], slip=(b"PNGDATA", "image/png"))
        await Poller(client, topics, msgids).poll_once(bot)
        self.assertEqual(len(bot.photos), 1)
        self.assertEqual(bot.photos[0]["reply_to_message_id"], 555)
        self.assertEqual(bot.photos[0]["photo"], b"PNGDATA")
        self.assertIsNotNone(bot.photos[0].get("reply_markup"))   # buttons on the SLIP
        self.assertEqual(client.marked, [2])

    async def test_no_alerts_topic_leaves_undelivered(self):
        topics, msgids = _stores()                       # nothing bound
        ev = {"id": 3, "event_type": "booking.created", "reference": "X",
              "booking_id": None, "alert": _ALERT, "created_at": _now_iso()}
        bot, client = FakeBot(), FakeOutboxClient([ev])
        n = await Poller(client, topics, msgids).poll_once(bot)
        self.assertEqual(n, 0)
        self.assertEqual(bot.messages, [])
        self.assertEqual(client.marked, [])              # NOT marked -> retried

    async def test_telegram_failure_not_marked(self):
        topics, msgids = _stores()
        topics.set_topic("alerts", -100, 7)
        ev = {"id": 4, "event_type": "booking.created", "reference": "X",
              "booking_id": None, "alert": _ALERT, "created_at": _now_iso()}
        bot, client = FakeBot(fail=True), FakeOutboxClient([ev])
        n = await Poller(client, topics, msgids).poll_once(bot)
        self.assertEqual(n, 0)
        self.assertEqual(client.marked, [])              # failure -> retried, not lost

    async def test_slip_fetch_fail_retries_no_fallback_no_mark(self):
        topics, msgids = _stores()
        topics.set_topic("alerts", -100, 7)
        ev = {"id": 5, "event_type": "slip.uploaded", "reference": "ABC12345",
              "booking_id": None, "alert": {"ref": "ABC12345", "total": 1200.0},
              "created_at": _now_iso()}
        bot = FakeBot()
        client = FakeOutboxClient([ev], slip=None)       # image not fetchable
        n = await Poller(client, topics, msgids).poll_once(bot)
        self.assertEqual(n, 0)
        self.assertEqual(bot.photos, [])                 # nothing posted
        self.assertEqual(bot.messages, [])               # NO "unavailable" excuse text
        self.assertEqual(client.marked, [])              # NOT marked -> retried

    async def test_booking_no_alert_retries_no_fallback_no_mark(self):
        topics, msgids = _stores()
        topics.set_topic("alerts", -100, 7)
        ev = {"id": 6, "event_type": "booking.created", "reference": "X",
              "booking_id": None, "alert": None, "created_at": _now_iso()}
        bot, client = FakeBot(), FakeOutboxClient([ev])
        n = await Poller(client, topics, msgids).poll_once(bot)
        self.assertEqual(n, 0)
        self.assertEqual(bot.messages, [])               # NO "details unavailable" text
        self.assertEqual(client.marked, [])              # NOT marked -> retried

    async def test_giveup_logs_loud_after_1h_but_stays_undelivered(self):
        topics, msgids = _stores()
        topics.set_topic("alerts", -100, 7)
        ev = {"id": 7, "event_type": "slip.uploaded", "reference": "X",
              "booking_id": None, "alert": {"ref": "X"},
              "created_at": _old_iso(4000)}              # >1h old, still failing
        bot = FakeBot()
        client = FakeOutboxClient([ev], slip=None)
        poller = Poller(client, topics, msgids)
        with self.assertLogs("pepper_bot", level="ERROR") as cm:
            await poller.poll_once(bot)
        self.assertTrue(any("UNDELIVERED" in m for m in cm.output))
        self.assertEqual(client.marked, [])              # stays undelivered (visible)
        # loud only ONCE
        with self.assertRaises(AssertionError):
            with self.assertLogs("pepper_bot", level="ERROR"):
                await poller.poll_once(bot)


class MsgIdStoreTest(unittest.TestCase):
    def test_roundtrip_and_cap(self):
        d = tempfile.mkdtemp()
        s = MsgIdStore(os.path.join(d, "m.json"), cap=3)
        for i in range(5):
            s.set(f"ref{i}", 100 + i)
        self.assertIsNone(s.get("ref0"))                 # evicted (cap 3)
        self.assertEqual(s.get("ref4"), 104)
        self.assertLessEqual(len(s._load()), 3)


def _fake_cq(uid, data, name="Aisha", chat_id=-100, msg_id=15, text="alert"):
    u = MagicMock()
    cq = u.callback_query
    cq.data = data
    cq.from_user.id = uid
    cq.from_user.full_name = name
    cq.answer = AsyncMock()
    cq.message.chat_id = chat_id
    cq.message.message_id = msg_id
    cq.message.text = text
    cq.message.caption = None
    cq.message.message_thread_id = 7
    cq.message.edit_text = AsyncMock()
    cq.message.edit_caption = AsyncMock()
    cq.message.edit_reply_markup = AsyncMock()
    cq.message.reply_text = AsyncMock(return_value=MagicMock(message_id=99))
    return u


def _fake_ctx():
    """A context whose .bot has async edit/send methods (for the reject sub-menu)."""
    ctx = MagicMock()
    ctx.bot.edit_message_text = AsyncMock()
    ctx.bot.edit_message_caption = AsyncMock()
    ctx.bot.edit_message_reply_markup = AsyncMock()
    ctx.bot.send_message = AsyncMock(return_value=MagicMock(message_id=42))
    return ctx


class FakeActionClient:
    def __init__(self, role="manager", confirm=(200, {"ok": True, "by": "Aisha"}),
                 reject=(200, {"ok": True}), state=None):
        self.role = role
        self._confirm, self._reject = confirm, reject
        self._state = state or {"state": "pending", "armable": True}
        self.confirm_calls, self.reject_calls = [], []
        self.authorize_calls, self.revoke_calls, self.invalidated = [], [], []
        self.state_calls = []

    async def whitelist(self, tid):
        return {"allowed": self.role is not None, "role": self.role}

    async def confirm_hold(self, ref, actor_id=None, actor_name=None):
        self.confirm_calls.append((ref, actor_name))
        return self._confirm

    async def reject_hold(self, ref, reason, actor_id=None, actor_name=None):
        self.reject_calls.append((ref, reason, actor_name))
        return self._reject

    # target-aware forms (hold OR booking). For a hold, record the bare ref so the
    # existing Phase 3 assertions (confirm_calls == [("FAY9VDPF", ...)]) still hold;
    # for a booking, record its token 'b:<id>'.
    @staticmethod
    def _tok(target):
        return target.ref if target.kind == "hold" else f"b:{target.booking_id}"

    async def confirm_target(self, target, actor_id=None, actor_name=None,
                             cash=False):
        self.confirm_calls.append((self._tok(target), actor_name)
                                  if not cash else
                                  (self._tok(target), actor_name, "cash"))
        return self._confirm

    async def reject_target(self, target, reason, actor_id=None, actor_name=None):
        return await self.reject_hold(self._tok(target), reason, actor_id=actor_id,
                                      actor_name=actor_name)

    async def target_state(self, target):
        return await self.hold_state(self._tok(target))

    async def authorize(self, tid, role, name, added_by=None):
        self.authorize_calls.append((tid, role, name))
        return (200, {"ok": True})

    async def revoke(self, tid):
        self.revoke_calls.append(tid)
        return (200, {"ok": True})

    def invalidate_whitelist(self, tid):
        self.invalidated.append(tid)

    async def hold_state(self, reference):
        self.state_calls.append(reference)
        return self._state


class AuthorizeTest(IsolatedAsyncioTestCase):
    def _msg_update(self, uid):
        u = MagicMock()
        u.effective_user.id = uid
        u.effective_message.reply_text = AsyncMock()
        return u

    async def test_owner_authorizes_staff_and_invalidates(self):
        client = FakeActionClient()
        h = make_authorize_handler(client, owner_id="111")
        u = self._msg_update(111)
        await h(u, FakeCtx(args=["555", "staff", "Zoe", "Q"]))
        self.assertEqual(client.authorize_calls, [(555, "staff", "Zoe Q")])
        self.assertIn(555, client.invalidated)          # cache cleared -> immediate
        u.effective_message.reply_text.assert_awaited()

    async def test_non_owner_silent_no_authorize(self):
        client = FakeActionClient(role="manager")        # whitelisted but not owner
        h = make_authorize_handler(client, owner_id=None)
        u = self._msg_update(222)
        await h(u, FakeCtx(args=["555", "staff", "Zoe"]))
        self.assertEqual(client.authorize_calls, [])
        u.effective_message.reply_text.assert_not_awaited()

    async def test_bad_role_rejected(self):
        client = FakeActionClient()
        h = make_authorize_handler(client, owner_id="111")
        u = self._msg_update(111)
        await h(u, FakeCtx(args=["555", "admin", "Zoe"]))
        self.assertEqual(client.authorize_calls, [])
        self.assertIn("manager", u.effective_message.reply_text.await_args.args[0].lower())

    async def test_owner_revokes(self):
        client = FakeActionClient()
        h = make_revoke_handler(client, owner_id="111")
        u = self._msg_update(111)
        await h(u, FakeCtx(args=["555"]))
        self.assertEqual(client.revoke_calls, [555])
        self.assertIn(555, client.invalidated)


class VerifyKeyboardTest(unittest.TestCase):
    def test_two_buttons_with_callback_data(self):
        row = verify_keyboard("FAY9VDPF").inline_keyboard[0]
        self.assertEqual(len(row), 2)
        self.assertEqual(row[0].callback_data, "pv:v:FAY9VDPF")
        self.assertEqual(row[1].callback_data, "pv:r:FAY9VDPF")

    def test_booking_target_uses_b_token(self):
        from pepper_bot.target import Target
        row = verify_keyboard(Target.booking(42)).inline_keyboard[0]
        self.assertEqual(row[0].callback_data, "pv:v:b:42")
        self.assertEqual(row[1].callback_data, "pv:r:b:42")
        # reason menu, booking target -> pv:rr:<code>:b:<id> (still ≤ 64 bytes)
        kb = reason_menu_keyboard(Target.booking(42)).inline_keyboard
        codes = [b.callback_data for r in kb for b in r]
        self.assertIn("pv:rr:amt:b:42", codes)
        self.assertIn("pv:ro:b:42", codes)
        self.assertIn("pv:rc:b:42", codes)
        self.assertTrue(all(len(c.encode()) <= 64 for c in codes))

    def test_hold_ref_token_is_backward_compatible(self):
        # a bare hold ref (Phase 3 wire) is unchanged whether passed as str or Target
        from pepper_bot.target import Target
        self.assertEqual(verify_keyboard("FAY9VDPF").inline_keyboard[0][0].callback_data,
                         verify_keyboard(Target.hold("FAY9VDPF")).inline_keyboard[0][0].callback_data)

    def test_cash_keyboard_single_manager_button(self):
        from pepper_bot.target import Target
        kb = cash_keyboard(Target.booking(42)).inline_keyboard
        self.assertEqual(len(kb), 1)
        self.assertEqual(len(kb[0]), 1)                       # single button
        self.assertEqual(kb[0][0].callback_data, "pv:cash:b:42")
        self.assertIn("Cash", kb[0][0].text)

    def test_reason_menu_presets_other_and_cancel(self):
        kb = reason_menu_keyboard("FAY9VDPF").inline_keyboard
        codes = [btn.callback_data for row in kb for btn in row]
        self.assertIn("pv:rr:amt:FAY9VDPF", codes)
        self.assertIn("pv:rr:noslip:FAY9VDPF", codes)
        self.assertIn("pv:ro:FAY9VDPF", codes)                # ✍️ Other
        self.assertIn("pv:rc:FAY9VDPF", codes)                # ↩︎ Cancel
        # every preset code maps to a guest-facing sentence
        for code in ("blur", "amt", "acct", "noslip"):
            self.assertRegex(REJECT_REASONS[code], r"please")
        # callback_data stays within Telegram's 64-byte cap
        self.assertTrue(all(len(c.encode()) <= 64 for c in codes))


class ActionCallbackTest(IsolatedAsyncioTestCase):
    async def test_staff_tap_denied_no_state_change(self):
        client = FakeActionClient(role="staff")
        h = make_action_callback(client, owner_id=None, pending_rejects={})
        u = _fake_cq(222, "pv:v:FAY9VDPF")
        await h(u, None)
        self.assertIn("manager", u.callback_query.answer.await_args.args[0].lower())
        self.assertEqual(client.confirm_calls, [])                 # NO confirm
        u.callback_query.message.edit_text.assert_not_awaited()

    async def test_manager_verify_confirms_and_edits_in_place(self):
        client = FakeActionClient(role="manager",
                                  confirm=(200, {"ok": True, "by": "Aisha"}))
        h = make_action_callback(client, owner_id=None, pending_rejects={})
        u = _fake_cq(111, "pv:v:FAY9VDPF", name="Aisha")
        await h(u, None)
        self.assertEqual(client.confirm_calls, [("FAY9VDPF", "Aisha")])
        u.callback_query.message.edit_text.assert_awaited_once()
        self.assertIn("CONFIRMED by Aisha",
                      u.callback_query.message.edit_text.await_args.args[0])

    async def test_verify_loser_told_who_won(self):
        client = FakeActionClient(
            role="manager", confirm=(409, {"ok": False, "already": True, "by": "Aisha"}))
        h = make_action_callback(client, owner_id=None, pending_rejects={})
        u = _fake_cq(333, "pv:v:FAY9VDPF", name="Bob")
        await h(u, None)
        self.assertIn("Aisha", u.callback_query.answer.await_args.args[0])
        self.assertIn("CONFIRMED by Aisha",
                      u.callback_query.message.edit_text.await_args.args[0])

    async def test_owner_can_verify(self):
        client = FakeActionClient(role="staff")   # API role staff, but env owner
        h = make_action_callback(client, owner_id="111", pending_rejects={})
        u = _fake_cq(111, "pv:v:FAY9VDPF")         # uid matches owner env
        await h(u, None)
        self.assertEqual(len(client.confirm_calls), 1)   # owner bypasses role check

    async def test_manager_verify_booking_target_routes_to_booking(self):
        # A bot-created booking's ✅ Verify (pv:v:b:<id>) routes to the booking
        # verify path — same UX, different target.
        client = FakeActionClient(role="manager",
                                  confirm=(200, {"ok": True, "by": "Aisha"}))
        h = make_action_callback(client, owner_id=None, pending_rejects={})
        u = _fake_cq(111, "pv:v:b:42", name="Aisha")
        await h(u, None)
        self.assertEqual(client.confirm_calls, [("b:42", "Aisha")])   # booking token
        u.callback_query.message.edit_text.assert_awaited_once()
        self.assertIn("CONFIRMED by Aisha",
                      u.callback_query.message.edit_text.await_args.args[0])

    async def test_manager_cash_received_confirms_cash_mode(self):
        # 💵 Cash received (pv:cash:b:<id>) routes through state (armable pending)
        # -> confirm_target(cash=True); the alert edits to the DISTINCT cash
        # signature "💵 CASH confirmed by <name>".
        client = FakeActionClient(role="manager",
                                  state={"state": "pending", "armable": True,
                                         "payment_method": "cash"},
                                  confirm=(200, {"ok": True, "by": "Aisha",
                                                 "method": "cash"}))
        h = make_action_callback(client, owner_id=None, pending_rejects={})
        u = _fake_cq(111, "pv:cash:b:42", name="Aisha")
        await h(u, None)
        self.assertEqual(client.state_calls, ["b:42"])             # state checked FIRST
        self.assertEqual(client.confirm_calls, [("b:42", "Aisha", "cash")])
        edit = u.callback_query.message.edit_text.await_args.args[0]
        self.assertIn("CASH confirmed by Aisha", edit)             # distinct signature
        self.assertNotIn("✅ CONFIRMED by Aisha", edit)            # NOT the bank one

    async def test_cash_and_bank_signatures_differ(self):
        # Bank verify -> "✅ CONFIRMED by X"; cash -> "💵 CASH confirmed by X".
        bank = FakeActionClient(role="manager",
                                confirm=(200, {"ok": True, "by": "Aisha"}))
        hb = make_action_callback(bank, owner_id=None, pending_rejects={})
        ub = _fake_cq(111, "pv:v:b:42", name="Aisha")
        await hb(ub, None)
        bank_line = ub.callback_query.message.edit_text.await_args.args[0]

        cash = FakeActionClient(role="manager",
                                state={"state": "pending", "armable": True},
                                confirm=(200, {"ok": True, "by": "Aisha"}))
        hc = make_action_callback(cash, owner_id=None, pending_rejects={})
        uc = _fake_cq(111, "pv:cash:b:42", name="Aisha")
        await hc(uc, None)
        cash_line = uc.callback_query.message.edit_text.await_args.args[0]

        self.assertNotEqual(bank_line, cash_line)                  # ledger-distinct
        self.assertIn("✅ CONFIRMED by Aisha", bank_line)
        self.assertIn("💵 CASH confirmed by Aisha", cash_line)

    async def test_stale_cash_tap_on_confirmed_shows_truth_no_fire(self):
        # A tap on an OLD cash alert for a booking already confirmed by someone
        # else -> shows "CONFIRMED by X", does NOT fire the cash confirm.
        client = FakeActionClient(role="manager",
                                  state={"state": "confirmed", "armable": False,
                                         "by": "Bob"})
        h = make_action_callback(client, owner_id=None, pending_rejects={})
        u = _fake_cq(111, "pv:cash:b:42", name="Aisha")
        await h(u, None)
        self.assertEqual(client.state_calls, ["b:42"])
        self.assertEqual(client.confirm_calls, [])                 # DID NOT fire
        self.assertIn("CONFIRMED by Bob",
                      u.callback_query.message.edit_text.await_args.args[0])

    async def test_stale_cash_tap_on_cancelled_shows_truth_no_fire(self):
        client = FakeActionClient(role="manager",
                                  state={"state": "cancelled", "armable": False})
        h = make_action_callback(client, owner_id=None, pending_rejects={})
        u = _fake_cq(111, "pv:cash:b:42", name="Aisha")
        await h(u, None)
        self.assertEqual(client.confirm_calls, [])                 # DID NOT fire
        self.assertIn("cancelled",
                      u.callback_query.message.edit_text.await_args.args[0].lower())

    async def test_staff_cash_tap_bounces_no_confirm(self):
        # A staff-role tap on 💵 Cash received is denied (manager-gated) — no confirm.
        client = FakeActionClient(role="staff")
        h = make_action_callback(client, owner_id=None, pending_rejects={})
        u = _fake_cq(222, "pv:cash:b:42")
        await h(u, None)
        self.assertIn("manager", u.callback_query.answer.await_args.args[0].lower())
        self.assertEqual(client.confirm_calls, [])                 # NO confirm
        self.assertEqual(client.state_calls, [])                   # gated before state
        u.callback_query.message.edit_text.assert_not_awaited()

    async def test_preset_reason_rejects_booking_target(self):
        client = FakeActionClient(role="manager", reject=(200, {"ok": True}))
        pr = {}
        h = make_action_callback(client, owner_id=None, pending_rejects=pr)
        ctx = _fake_ctx()
        u = _fake_cq(111, "pv:rr:amt:b:42", name="Aisha", chat_id=-100)
        await h(u, ctx)
        ref, reason, actor = client.reject_calls[0]
        self.assertEqual(ref, "b:42")                       # booking token
        self.assertEqual(reason, REJECT_REASONS["amt"])
        self.assertEqual(pr, {})

    async def test_reject_opens_reason_menu(self):
        client = FakeActionClient(role="manager")
        pr = {}
        h = make_action_callback(client, owner_id=None, pending_rejects=pr)
        u = _fake_cq(111, "pv:r:FAY9VDPF", name="Aisha", chat_id=-100)
        await h(u, _fake_ctx())
        # ❌ Reject swaps the keyboard to the preset menu (NOT a force-reply prompt).
        u.callback_query.message.edit_reply_markup.assert_awaited_once()
        self.assertEqual(pr[(-100, 111)]["ref"], "FAY9VDPF")
        self.assertEqual(pr[(-100, 111)]["stage"], "menu")
        self.assertEqual(client.reject_calls, [])                   # nothing rejected yet

    async def test_preset_reason_rejects_with_guest_facing_text(self):
        client = FakeActionClient(role="manager", reject=(200, {"ok": True}))
        pr = {}
        h = make_action_callback(client, owner_id=None, pending_rejects=pr)
        ctx = _fake_ctx()
        u = _fake_cq(111, "pv:rr:amt:FAY9VDPF", name="Aisha", chat_id=-100)
        await h(u, ctx)
        # The GUEST-facing sentence lands on the hold, NOT the terse button label.
        ref, reason, actor = client.reject_calls[0]
        self.assertEqual(ref, "FAY9VDPF")
        self.assertEqual(reason, REJECT_REASONS["amt"])
        self.assertNotIn("Amount doesn't match", reason)            # not the raw label
        self.assertIn("SLIP REJECTED by Aisha",
                      ctx.bot.edit_message_text.await_args.kwargs["text"])
        self.assertEqual(pr, {})                                    # pending cleared

    async def test_other_opens_typed_prompt(self):
        client = FakeActionClient(role="manager")
        pr = {}
        h = make_action_callback(client, owner_id=None, pending_rejects=pr)
        u = _fake_cq(111, "pv:ro:FAY9VDPF", name="Aisha", chat_id=-100)
        await h(u, _fake_ctx())
        u.callback_query.message.reply_text.assert_awaited_once()   # force-reply prompt
        self.assertEqual(pr[(-100, 111)]["stage"], "await_text")

    async def test_reason_command_completes_pending(self):
        client = FakeActionClient(role="manager", reject=(200, {"ok": True}))
        pr = {(-100, 111): {"ref": "FAY9VDPF", "alert_msg_id": 15, "alert_text": "a",
                            "name": "Aisha", "stage": "await_text", "task": None}}
        h = make_reason_command_handler(client, pr)
        msg = MagicMock(); msg.chat_id = -100
        msg.reply_text = AsyncMock()
        u = MagicMock(); u.effective_message = msg; u.effective_user.id = 111
        ctx = _fake_ctx(); ctx.args = ["amount", "wrong"]
        await h(u, ctx)
        self.assertEqual(client.reject_calls, [("FAY9VDPF", "amount wrong", "Aisha")])
        self.assertEqual(pr, {})

    async def test_reason_command_without_pending_hints(self):
        client = FakeActionClient(role="manager")
        h = make_reason_command_handler(client, {})
        msg = MagicMock(); msg.chat_id = -100
        msg.reply_text = AsyncMock()
        u = MagicMock(); u.effective_message = msg; u.effective_user.id = 111
        ctx = _fake_ctx(); ctx.args = ["blah"]
        await h(u, ctx)
        self.assertEqual(client.reject_calls, [])
        self.assertIn("no pending", msg.reply_text.await_args.args[0].lower())

    async def test_cancel_rearms_when_still_pending(self):
        client = FakeActionClient(role="manager",
                                  state={"state": "pending", "armable": True})
        pr = {(-100, 111): {"ref": "FAY9VDPF", "alert_msg_id": 15, "alert_text": "a",
                            "name": "Aisha", "stage": "menu", "task": None,
                            "chat_id": -100}}
        h = make_action_callback(client, owner_id=None, pending_rejects=pr)
        ctx = _fake_ctx()
        u = _fake_cq(111, "pv:rc:FAY9VDPF", name="Aisha", chat_id=-100)
        await h(u, ctx)
        self.assertEqual(client.state_calls, ["FAY9VDPF"])          # went through state
        ctx.bot.edit_message_reply_markup.assert_awaited_once()     # re-armed ✅/❌
        self.assertEqual(pr, {})

    async def test_cancel_does_not_rearm_dead_buttons(self):
        # Someone else confirmed while the reject was pending -> Cancel must NOT
        # re-arm; it shows current state and clears the buttons.
        client = FakeActionClient(role="manager",
                                  state={"state": "confirmed", "armable": False,
                                         "by": "Bob"})
        pr = {(-100, 111): {"ref": "FAY9VDPF", "alert_msg_id": 15, "alert_text": "a",
                            "name": "Aisha", "stage": "menu", "task": None,
                            "chat_id": -100}}
        h = make_action_callback(client, owner_id=None, pending_rejects=pr)
        ctx = _fake_ctx()
        u = _fake_cq(111, "pv:rc:FAY9VDPF", name="Aisha", chat_id=-100)
        await h(u, ctx)
        self.assertEqual(client.state_calls, ["FAY9VDPF"])
        ctx.bot.edit_message_reply_markup.assert_not_awaited()      # NO re-arm
        self.assertIn("CONFIRMED by Bob",
                      ctx.bot.edit_message_text.await_args.kwargs["text"])

    async def test_timeout_reprompts_once_then_disarms(self):
        client = FakeActionClient(role="manager",
                                  state={"state": "pending", "armable": True})
        pr = {(-100, 111): {"ref": "FAY9VDPF", "alert_msg_id": 15, "alert_text": "a",
                            "name": "Aisha", "stage": "await_text", "reprompted": False,
                            "task": None, "chat_id": -100, "thread_id": 7}}
        bot = MagicMock()
        bot.send_message = AsyncMock(return_value=MagicMock(message_id=42))
        bot.edit_message_text = AsyncMock(); bot.edit_message_caption = AsyncMock()
        bot.edit_message_reply_markup = AsyncMock()
        orig = _h.TIMEOUT_SECONDS
        _h.TIMEOUT_SECONDS = 0.0
        try:
            # first lapse -> re-prompt once (still pending, reprompted flips True)
            await _reject_timeout(bot, client, pr, (-100, 111))
            self.assertTrue(pr[(-100, 111)]["reprompted"])
            bot.send_message.assert_awaited_once()
            self.assertIn("SWIPE TO REPLY",
                          bot.send_message.await_args.kwargs["text"])
            # second lapse -> clean disarm through the state check
            await _reject_timeout(bot, client, pr, (-100, 111))
            self.assertEqual(pr, {})                                # pending cleared
            self.assertIn("FAY9VDPF", client.state_calls)           # state consulted
            bot.edit_message_reply_markup.assert_awaited()          # re-armed on pending
        finally:
            _h.TIMEOUT_SECONDS = orig

    async def test_reject_reason_rejects_and_edits(self):
        client = FakeActionClient(role="manager", reject=(200, {"ok": True}))
        pr = {(-100, 111): {"ref": "FAY9VDPF", "alert_msg_id": 15, "task": None,
                            "alert_text": "alert", "name": "Aisha"}}
        h = make_reject_reason_handler(client, pr)
        u = MagicMock()
        u.effective_message.chat_id = -100
        u.effective_message.text = "blurry slip"
        u.effective_message.reply_text = AsyncMock()
        u.effective_user.id = 111
        ctx = _fake_ctx()
        await h(u, ctx)
        self.assertEqual(client.reject_calls, [("FAY9VDPF", "blurry slip", "Aisha")])
        self.assertIn("SLIP REJECTED by Aisha: blurry slip",
                      ctx.bot.edit_message_text.await_args.kwargs["text"])
        self.assertEqual(pr, {})                       # cleared

    async def test_reject_reason_ignored_without_pending(self):
        client = FakeActionClient(role="manager")
        h = make_reject_reason_handler(client, {})
        u = MagicMock()
        u.effective_message.chat_id = -100
        u.effective_message.text = "random chatter"
        u.effective_message.reply_text = AsyncMock()
        u.effective_user.id = 111
        await h(u, MagicMock())
        self.assertEqual(client.reject_calls, [])      # ignored


class TTLCacheTest(unittest.TestCase):
    def test_put_then_get(self):
        c = _TTLCache(60)
        self.assertIsNone(c.get("k"))
        c.put("k", {"allowed": True})
        self.assertEqual(c.get("k"), {"allowed": True})

    def test_expired_entry_returns_none(self):
        c = _TTLCache(0)          # ttl 0 -> expires immediately
        c.put("k", "v")
        self.assertIsNone(c.get("k"))


class UpdateLoggerTest(IsolatedAsyncioTestCase):
    """#25 metadata-only inbound-update logger: from/chat/topic/verb, never the body."""

    async def test_logs_metadata_only_no_body(self):
        from pepper_bot.handlers import make_update_logger
        h = make_update_logger()
        upd = fake_update(111, text="/ask what is John Smith passport A123",
                          chat_id=-100, thread_id=4)
        upd.effective_user.is_bot = False
        upd.callback_query = None
        with self.assertLogs("pepper_bot.updates", level="INFO") as cm:
            await h(upd, None)
        line = cm.output[0]
        self.assertIn("from=111", line)
        self.assertIn("chat=-100", line)
        self.assertIn("thread=4", line)
        self.assertIn("/ask", line)
        self.assertNotIn("John Smith", line)   # NO body / PII
        self.assertNotIn("A123", line)

    async def test_skips_bot_origin_updates(self):
        from pepper_bot.handlers import make_update_logger
        h = make_update_logger()
        upd = fake_update(999, text="🆕 Booking alert", chat_id=-100, thread_id=3)
        upd.effective_user.is_bot = True       # ingested alert post / other bot
        upd.callback_query = None
        with self.assertNoLogs("pepper_bot.updates", level="INFO"):
            await h(upd, None)


if __name__ == "__main__":
    unittest.main()
