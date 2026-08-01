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

from pepper_bot.handlers import (cmd_myid, make_ping_handler, make_whitelist_gate,   # noqa: E402
                                 make_bindtopics_handler, make_topics_handler,
                                 make_action_callback, make_reject_reason_handler,
                                 make_authorize_handler, make_revoke_handler,
                                 verify_keyboard)
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
    cq.message.edit_text = AsyncMock()
    cq.message.edit_caption = AsyncMock()
    cq.message.reply_text = AsyncMock(return_value=MagicMock(message_id=99))
    return u


class FakeActionClient:
    def __init__(self, role="manager", confirm=(200, {"ok": True, "by": "Aisha"}),
                 reject=(200, {"ok": True})):
        self.role = role
        self._confirm, self._reject = confirm, reject
        self.confirm_calls, self.reject_calls = [], []
        self.authorize_calls, self.revoke_calls, self.invalidated = [], [], []

    async def whitelist(self, tid):
        return {"allowed": self.role is not None, "role": self.role}

    async def confirm_hold(self, ref, actor_id=None, actor_name=None):
        self.confirm_calls.append((ref, actor_name))
        return self._confirm

    async def reject_hold(self, ref, reason, actor_id=None, actor_name=None):
        self.reject_calls.append((ref, reason, actor_name))
        return self._reject

    async def authorize(self, tid, role, name, added_by=None):
        self.authorize_calls.append((tid, role, name))
        return (200, {"ok": True})

    async def revoke(self, tid):
        self.revoke_calls.append(tid)
        return (200, {"ok": True})

    def invalidate_whitelist(self, tid):
        self.invalidated.append(tid)


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

    async def test_reject_opens_reason_flow(self):
        client = FakeActionClient(role="manager")
        pr = {}
        h = make_action_callback(client, owner_id=None, pending_rejects=pr)
        u = _fake_cq(111, "pv:r:FAY9VDPF", name="Aisha", chat_id=-100)
        await h(u, None)
        u.callback_query.message.reply_text.assert_awaited_once()   # force-reply prompt
        self.assertEqual(pr[(-100, 111)]["ref"], "FAY9VDPF")

    async def test_reject_reason_rejects_and_edits(self):
        client = FakeActionClient(role="manager", reject=(200, {"ok": True}))
        pr = {(-100, 111): {"ref": "FAY9VDPF", "alert_msg_id": 15,
                            "alert_text": "alert", "name": "Aisha"}}
        h = make_reject_reason_handler(client, pr)
        u = MagicMock()
        u.effective_message.chat_id = -100
        u.effective_message.text = "blurry slip"
        u.effective_message.reply_text = AsyncMock()
        u.effective_user.id = 111
        ctx = MagicMock(); ctx.bot.edit_message_text = AsyncMock()
        await h(u, ctx)
        self.assertEqual(client.reject_calls, [("FAY9VDPF", "blurry slip", "Aisha")])
        self.assertIn("REJECTED by Aisha: blurry slip",
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


if __name__ == "__main__":
    unittest.main()
