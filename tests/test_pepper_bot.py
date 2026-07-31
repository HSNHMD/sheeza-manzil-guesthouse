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

from pepper_bot.handlers import (cmd_myid, make_ping_handler, make_whitelist_gate,
                                 make_bindtopics_handler, make_topics_handler)
from pepper_bot.gate import resolve_access
from pepper_bot.internal_api import _TTLCache
from pepper_bot.topics import TopicStore

try:  # the gate's silent-drop path imports telegram.ext; skip only that test w/o PTB
    import telegram  # noqa: F401
    _HAS_PTB = True
except ImportError:
    _HAS_PTB = False


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
