"""Outbox -> Alerts poller.

Every `interval`s: pull undelivered outbox events from the internal API, post them
into the bound 📥 Alerts topic, then mark delivered. At-least-once by design
(post THEN mark) — the OUTBOX is the durability layer, so a brief bot/Telegram
outage means late delivery, never loss. A per-event failure is retried next cycle
(not marked delivered); it never aborts the loop.
"""

from __future__ import annotations

import asyncio
import logging

from .alerts import format_booking_created, format_slip_caption

log = logging.getLogger("pepper_bot")


async def _deliver(bot, client, chat_id, thread, msgids, ev):
    a = ev.get("alert")
    etype = ev.get("event_type")
    ref = (a or {}).get("ref") or ev.get("reference") or ev.get("booking_id")

    if etype == "slip.uploaded":
        data = await client.slip_bytes(reference=ev.get("reference"),
                                       booking_id=ev.get("booking_id"))
        reply_to = msgids.get(ref)
        caption = format_slip_caption(ref, (a or {}).get("total"))
        if data:
            await bot.send_photo(chat_id=chat_id, message_thread_id=thread,
                                 photo=data[0], caption=caption,
                                 reply_to_message_id=reply_to)
        else:
            await bot.send_message(chat_id=chat_id, message_thread_id=thread,
                                   text=caption + "\n(slip image unavailable)",
                                   reply_to_message_id=reply_to)
        return

    # booking.created (default)
    text = (format_booking_created(a) if a else
            f"🆕 Booking #{ref} — details unavailable (hold may have expired)")
    msg = await bot.send_message(chat_id=chat_id, message_thread_id=thread, text=text)
    if ref is not None:
        msgids.set(ref, msg.message_id)   # remember for slip threading


async def poll_once(bot, client, topics, msgids) -> int:
    events = await client.outbox_undelivered()
    if not events:
        return 0
    alerts = topics.get("alerts")
    if not alerts:
        log.warning("outbox has %d event(s) but no 'alerts' topic bound — leaving "
                    "them undelivered", len(events))
        return 0
    chat_id, thread = alerts["chat_id"], alerts["thread_id"]
    delivered = 0
    for ev in events:
        try:
            await _deliver(bot, client, chat_id, thread, msgids, ev)
            await client.mark_delivered(ev["id"])     # post THEN mark (at-least-once)
            delivered += 1
        except Exception:  # noqa: BLE001 — one bad event must not stop the batch
            log.warning("outbox event %s delivery failed; will retry next cycle",
                        ev.get("id"))
    return delivered


async def poller_loop(bot, client, topics, msgids, interval: float = 5.0):
    log.info("outbox poller started (interval %ss)", interval)
    while True:
        try:
            await poll_once(bot, client, topics, msgids)
        except Exception:  # noqa: BLE001 — never let the loop die
            log.warning("poller cycle error; continuing")
        await asyncio.sleep(interval)
