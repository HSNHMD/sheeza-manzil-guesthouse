"""Outbox -> Alerts poller.

Every `interval`s: pull undelivered outbox events, post them into the bound 📥
Alerts topic, then mark delivered. **An event is marked delivered ONLY when its
intended message was actually posted.** If the slip image can't be fetched, or a
booking alert can't be rendered, or Telegram/the API errors, the event is left
UNDELIVERED and retried next cycle — no excuse-text fallback (a silent retry that
eventually posts the real thing beats a posted excuse that closes the event).

If an event keeps failing past `giveup_seconds` (1h), it stays undelivered
(visible in the outbox) and is logged LOUDLY once — an undelivered row we can see
beats a delivered lie.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime

from .alerts import format_booking_created, format_slip_caption
from .handlers import verify_keyboard
from .target import Target

log = logging.getLogger("pepper_bot")

GIVEUP_SECONDS = 3600


class Poller:
    def __init__(self, client, topics, msgids, giveup_seconds: int = GIVEUP_SECONDS):
        self.client = client
        self.topics = topics
        self.msgids = msgids
        self.giveup_seconds = giveup_seconds
        self._loud: set = set()   # event ids already loudly logged (log once)

    async def poll_once(self, bot) -> int:
        events = await self.client.outbox_undelivered()
        if not events:
            return 0
        alerts = self.topics.get("alerts")
        if not alerts:
            log.warning("outbox has %d event(s) but no 'alerts' topic bound", len(events))
            return 0
        chat_id, thread = alerts["chat_id"], alerts["thread_id"]
        delivered = 0
        for ev in events:
            try:
                if await self._deliver(bot, chat_id, thread, ev):
                    await self.client.mark_delivered(ev["id"])   # only on real delivery
                    delivered += 1
                    self._loud.discard(ev["id"])
                else:
                    self._maybe_loud(ev)          # unrenderable/unfetchable -> retry
            except Exception:  # noqa: BLE001 — transient error -> retry, NEVER mark
                log.warning("outbox event %s errored; will retry", ev.get("id"))
                self._maybe_loud(ev)
        return delivered

    async def _deliver(self, bot, chat_id, thread, ev) -> bool:
        """True only if the intended message was actually posted (safe to mark).
        False => leave undelivered and retry — NO excuse-text fallback."""
        a = ev.get("alert")
        ref = (a or {}).get("ref") or ev.get("reference") or ev.get("booking_id")
        if ev.get("event_type") == "slip.uploaded":
            data = await self.client.slip_bytes(reference=ev.get("reference"),
                                                booking_id=ev.get("booking_id"))
            if not data:
                return False          # slip not fetchable yet -> retry
            # The ✅ Verify / ❌ Reject buttons live on the SLIP alert — it has a
            # slip by definition (slip guard, bot side). Arm them against the RIGHT
            # target: a bot-created booking (by id) OR a portal hold (by ref), so
            # the tap routes to the matching verify/reject endpoint.
            bid = (a or {}).get("booking_id") or ev.get("booking_id")
            hold_ref = (a or {}).get("ref") if not bid else None
            hold_ref = hold_ref or (ev.get("reference") if not bid else None)
            if bid is not None:
                target = Target.booking(bid)
            elif hold_ref is not None:
                target = Target.hold(hold_ref)
            else:
                target = None
            await bot.send_photo(
                chat_id=chat_id, message_thread_id=thread, photo=data[0],
                caption=format_slip_caption(ref, (a or {}).get("total")),
                reply_to_message_id=self.msgids.get(ref),
                reply_markup=verify_keyboard(target) if target is not None else None)
            return True
        # booking.created — notification only; NO buttons (no slip to verify yet).
        if not a:
            return False              # can't render yet -> retry
        msg = await bot.send_message(chat_id=chat_id, message_thread_id=thread,
                                     text=format_booking_created(a))
        if ref is not None:
            self.msgids.set(ref, msg.message_id)
        return True

    def _maybe_loud(self, ev):
        try:
            age = (datetime.utcnow()
                   - datetime.fromisoformat(ev["created_at"])).total_seconds()
        except Exception:  # noqa: BLE001
            return
        if age > self.giveup_seconds and ev["id"] not in self._loud:
            self._loud.add(ev["id"])
            log.error("OUTBOX EVENT %s (%s ref=%s) STILL UNDELIVERED after %.0fs — "
                      "needs attention; left undelivered (visible), NOT marked",
                      ev["id"], ev.get("event_type"), ev.get("reference"), age)


async def poller_loop(bot, client, topics, msgids, interval: float = 5.0):
    poller = Poller(client, topics, msgids)
    log.info("outbox poller started (interval %ss)", interval)
    while True:
        try:
            await poller.poll_once(bot)
        except Exception:  # noqa: BLE001 — never let the loop die
            log.warning("poller cycle error; continuing")
        await asyncio.sleep(interval)
