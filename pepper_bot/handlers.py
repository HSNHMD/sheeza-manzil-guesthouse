"""Command handlers — deliberately telegram/httpx-free so they unit-test with
plain mocks. Handlers use only `update.effective_user.id` and
`update.effective_message.reply_text(...)` (duck-typed).

Design under privacy-mode-ON: only commands, force-reply, and callbacks reach the
bot — no reliance on reading arbitrary group messages.
"""

from __future__ import annotations

import asyncio

from .gate import resolve_access

# Reject-reason timeout: a pending reject that gets no reason auto-cancels so the
# alert never sits half-armed. First lapse -> one re-prompt; second -> disarm.
TIMEOUT_SECONDS = 120
_TIMEOUTS_ENABLED = True   # tests flip this off to avoid scheduling real timers

# Preset reject reasons. Button label (manager-facing, terse) -> guest-facing
# sentence that is what actually lands on the hold + the guest's status page.
# ✍️ Other… collects a custom sentence (typed verbatim, already guest-facing).
REJECT_REASONS = {
    "blur":   "The payment slip is unclear — please re-send a clear photo of the full slip.",
    "amt":    "The amount doesn't match — please send the full transfer slip.",
    "acct":   "The transfer went to the wrong account — please re-send to the correct account and upload the new slip.",
    "noslip": "That doesn't look like a payment slip — please upload your bank transfer slip.",
}
_REASON_LABELS = {
    "blur": "Blurry / unclear", "amt": "Amount doesn't match",
    "acct": "Wrong account", "noslip": "Not a slip",
}


async def cmd_myid(update, context):
    """/myid — works for ANYONE (pre-whitelist), ID-display only. This is the one
    command that bypasses the whitelist, so a new staff member can fetch the ID
    the owner then /authorizes. It reveals only the caller's own id."""
    uid = update.effective_user.id
    await update.effective_message.reply_text(f"Your Telegram ID: {uid}")


def make_ping_handler(client, owner_id):
    """/ping — whitelist-gated. Whitelisted → 'pong'. Unlisted → TOTAL SILENCE
    (no reply, no error — an error confirms the bot exists and invites probing)."""
    async def cmd_ping(update, context):
        uid = update.effective_user.id
        allowed, _role = await resolve_access(client, owner_id, uid)
        if not allowed:
            return  # silence
        await update.effective_message.reply_text("pong")
    return cmd_ping


_VALID_LABELS = ("alerts", "newbooking", "general")


def make_bindtopics_handler(client, owner_id, store):
    """/bindtopics <alerts|newbooking|general> — OWNER-only. Run INSIDE the target
    forum topic; captures that topic's chat_id + message_thread_id and persists it.
    Non-owner (even whitelisted staff) → silence."""
    async def cmd_bindtopics(update, context):
        allowed, role = await resolve_access(
            client, owner_id, update.effective_user.id)
        if role != "owner":
            return  # silence
        args = list(getattr(context, "args", None) or [])
        label = args[0].lower() if args else ""
        if label not in _VALID_LABELS:
            await update.effective_message.reply_text(
                "Usage: /bindtopics <alerts|newbooking|general> — run inside the topic.")
            return
        thread_id = getattr(update.effective_message, "message_thread_id", None)
        if thread_id is None:
            await update.effective_message.reply_text(
                "Run /bindtopics inside a forum TOPIC (no topic detected here).")
            return
        chat_id = update.effective_chat.id
        store.set_topic(label, chat_id, thread_id)
        await update.effective_message.reply_text(
            f"Bound '{label}' → chat {chat_id}, topic {thread_id}.")
    return cmd_bindtopics


def make_topics_handler(client, owner_id, store):
    """/topics — OWNER-only. Show the current topic bindings."""
    async def cmd_topics(update, context):
        allowed, role = await resolve_access(
            client, owner_id, update.effective_user.id)
        if role != "owner":
            return
        data = store.all()
        if not data:
            await update.effective_message.reply_text("No topics bound yet.")
            return
        lines = [f"{k}: chat {v['chat_id']}, topic {v['thread_id']}"
                 for k, v in sorted(data.items())]
        await update.effective_message.reply_text("\n".join(lines))
    return cmd_topics


def verify_keyboard(reference):
    """✅ Verify / ❌ Reject inline keyboard for a booking alert (by reference)."""
    from telegram import InlineKeyboardButton, InlineKeyboardMarkup
    return InlineKeyboardMarkup([[
        InlineKeyboardButton("✅ Verify", callback_data=f"pv:v:{reference}"),
        InlineKeyboardButton("❌ Reject", callback_data=f"pv:r:{reference}"),
    ]])


async def _finalize_alert(message, outcome_line):
    """Append the outcome to the alert and drop the buttons (edit in place)."""
    base = message.text or message.caption or ""
    new = (base + "\n\n" + outcome_line).strip()
    try:
        await message.edit_text(new)          # text alert: also clears the keyboard
    except Exception:
        try:
            await message.edit_caption(new)
        except Exception:
            pass


def reason_menu_keyboard(reference):
    """Preset reject reasons + ✍️ Other… + ↩︎ Cancel (replaces ✅/❌ in place)."""
    from telegram import InlineKeyboardButton, InlineKeyboardMarkup
    b = InlineKeyboardButton
    return InlineKeyboardMarkup([
        [b(_REASON_LABELS["blur"], callback_data=f"pv:rr:blur:{reference}"),
         b(_REASON_LABELS["amt"], callback_data=f"pv:rr:amt:{reference}")],
        [b(_REASON_LABELS["acct"], callback_data=f"pv:rr:acct:{reference}"),
         b(_REASON_LABELS["noslip"], callback_data=f"pv:rr:noslip:{reference}")],
        [b("✍️ Other…", callback_data=f"pv:ro:{reference}"),
         b("↩︎ Cancel", callback_data=f"pv:rc:{reference}")],
    ])


def _pend_from_message(message, name, thread_id):
    return {"ref": None, "alert_msg_id": message.message_id,
            "alert_text": message.text or message.caption or "",
            "thread_id": thread_id, "name": name,
            "stage": "menu", "reprompted": False, "task": None}


async def _edit_alert_by_id(bot, chat_id, message_id, text):
    """Edit an alert to `text` whether it's a text message or a photo (caption)."""
    try:
        await bot.edit_message_text(chat_id=chat_id, message_id=message_id, text=text)
    except Exception:
        try:
            await bot.edit_message_caption(chat_id=chat_id, message_id=message_id,
                                           caption=text)
        except Exception:
            pass


async def _notify(bot, pend, text):
    try:
        await bot.send_message(chat_id=pend["chat_id"],
                               message_thread_id=pend.get("thread_id"), text=text)
    except Exception:
        pass


async def _complete_reject(bot, client, pend, reason, actor_id):
    """Reject through the idempotent endpoint + edit the alert in place. Returns
    (ok, by): ok True on reject; else `by` names who already handled it (or None)."""
    ref, name = pend["ref"], pend["name"]
    status, body = await client.reject_hold(ref, reason, actor_id=actor_id,
                                            actor_name=name)
    if status == 200 and body.get("ok"):
        await _edit_alert_by_id(bot, pend["chat_id"], pend["alert_msg_id"],
            (pend["alert_text"] + f"\n\n❌ SLIP REJECTED by {name}: {reason}").strip())
        return True, None
    if body.get("already"):
        by = body.get("by", "someone")
        await _edit_alert_by_id(bot, pend["chat_id"], pend["alert_msg_id"],
            (pend["alert_text"] + f"\n\n✅ Already handled by {by}").strip())
        return False, by
    return False, None


async def _resolve_to_state(bot, client, pend, note=None):
    """↩︎ Cancel / timeout: go through the authoritative hold state. Re-arm ✅/❌
    ONLY if it's still an armable pending hold; otherwise show the real current
    state and drop the buttons — never re-arm dead buttons. Returns the state."""
    st = await client.hold_state(pend["ref"])
    state, ref, mid = st.get("state"), pend["ref"], pend["alert_msg_id"]
    if state == "pending" and st.get("armable"):
        try:
            await bot.edit_message_reply_markup(
                chat_id=pend["chat_id"], message_id=mid,
                reply_markup=verify_keyboard(ref))
        except Exception:
            pass
        if note:
            await _notify(bot, pend, f"{note} #{ref} re-armed (✅ / ❌).")
        return "pending"
    line = {
        "confirmed": f"✅ CONFIRMED by {st.get('by', 'someone')}",
        "slip_rejected": f"❌ SLIP already rejected: {st.get('reason') or '—'}",
        "expired": "⌛ Hold expired.",
        "awaiting_slip": "⏳ Awaiting a slip.",
    }.get(state, "ℹ️ State changed — buttons cleared.")
    await _edit_alert_by_id(bot, pend["chat_id"], mid,
                            (pend["alert_text"] + "\n\n" + line).strip())
    return state


def _cancel_timeout(pend):
    task = pend.get("task") if pend else None
    if task is not None and not task.done():
        task.cancel()


def _schedule_timeout(bot, client, pending_rejects, key):
    pend = pending_rejects.get(key)
    if pend is None or not _TIMEOUTS_ENABLED:
        return
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        pend["task"] = None            # no loop (unit tests) -> timeout inert
        return
    pend["task"] = loop.create_task(
        _reject_timeout(bot, client, pending_rejects, key))


async def _reject_timeout(bot, client, pending_rejects, key):
    """After TIMEOUT_SECONDS of no reason: re-prompt once (await-text stage), then
    on the next lapse disarm cleanly through the state check."""
    from telegram import ForceReply
    try:
        await asyncio.sleep(TIMEOUT_SECONDS)
    except asyncio.CancelledError:
        return
    pend = pending_rejects.get(key)
    if pend is None:
        return
    if pend.get("stage") == "await_text" and not pend.get("reprompted"):
        pend["reprompted"] = True
        try:
            p = await bot.send_message(
                chat_id=pend["chat_id"], message_thread_id=pend.get("thread_id"),
                text=(f"⏳ Still need a reject reason for #{pend['ref']}. "
                      "SWIPE TO REPLY to THIS message, or send /reason <text>. "
                      "Auto-cancels in 2 min."),
                reply_markup=ForceReply(selective=True))
            pend["prompt_id"] = p.message_id
        except Exception:
            pass
        _schedule_timeout(bot, client, pending_rejects, key)
        return
    pending_rejects.pop(key, None)
    await _resolve_to_state(bot, client, pend, note="⌛ Reject timed out —")


def make_action_callback(client, owner_id, pending_rejects):
    """Handle ✅/❌ + the reject sub-menu taps. Role-gated (manager/owner; staff ->
    ephemeral 'requires manager', no state change).
      pv:v:<ref>          ✅ Verify  -> confirm_hold (idempotent; loser told who won)
      pv:r:<ref>          ❌ Reject  -> swap keyboard to the preset-reason menu
      pv:rr:<code>:<ref>  preset     -> reject with the mapped GUEST-facing sentence
      pv:ro:<ref>         ✍️ Other…  -> force-reply prompt (reply OR /reason <text>)
      pv:rc:<ref>         ↩︎ Cancel  -> state-checked re-arm (never dead buttons)
    """
    from telegram import ForceReply

    async def on_action(update, context):
        cq = update.callback_query
        parts = (cq.data or "").split(":")
        if len(parts) < 3 or parts[0] != "pv":
            await cq.answer(); return
        tag = parts[1]
        if tag == "rr":
            if len(parts) != 4:
                await cq.answer(); return
            code, ref = parts[2], parts[3]
        else:
            code, ref = None, parts[2]
        uid = cq.from_user.id
        name = cq.from_user.full_name or cq.from_user.first_name or "staff"
        _allowed, role = await resolve_access(client, owner_id, uid)
        if role not in ("manager", "owner"):
            await cq.answer("Verification requires manager role.", show_alert=True)
            return
        key = (cq.message.chat_id, uid)
        thread_id = getattr(cq.message, "message_thread_id", None)

        if tag == "v":
            _cancel_timeout(pending_rejects.pop(key, None))   # a pending reject is moot
            status, body = await client.confirm_hold(ref, actor_id=uid, actor_name=name)
            if status == 200 and body.get("ok"):
                await cq.answer("Confirmed ✅")
                await _finalize_alert(cq.message, f"✅ CONFIRMED by {name}")
            elif body.get("already"):
                by = body.get("by", "someone")
                await cq.answer(f"Already confirmed by {by}.", show_alert=True)
                await _finalize_alert(cq.message, f"✅ CONFIRMED by {by}")
            else:
                await cq.answer(
                    "; ".join(body.get("reasons", ["could not confirm"]))[:190],
                    show_alert=True)
            return

        if tag == "r":                                  # show the reason menu
            pend = _pend_from_message(cq.message, name, thread_id)
            pend["ref"], pend["chat_id"] = ref, cq.message.chat_id
            pending_rejects[key] = pend
            try:
                await cq.message.edit_reply_markup(
                    reply_markup=reason_menu_keyboard(ref))
            except Exception:
                pass
            _schedule_timeout(context.bot, client, pending_rejects, key)
            await cq.answer("Pick a reason — or ✍️ Other / ↩︎ Cancel.")
            return

        if tag == "rr":                                 # preset -> guest-facing text
            reason = REJECT_REASONS.get(code)
            if reason is None:
                await cq.answer(); return
            pend = pending_rejects.pop(key, None) or _pend_from_message(
                cq.message, name, thread_id)
            _cancel_timeout(pend)
            pend["ref"], pend["chat_id"] = ref, cq.message.chat_id
            ok, by = await _complete_reject(context.bot, client, pend, reason, uid)
            await cq.answer("Rejected ❌" if ok else
                            (f"Already handled by {by}." if by else "Could not reject."),
                            show_alert=not ok)
            return

        if tag == "ro":                                 # ✍️ Other -> typed reason
            pend = pending_rejects.get(key) or _pend_from_message(
                cq.message, name, thread_id)
            pend["ref"], pend["chat_id"], pend["stage"] = ref, cq.message.chat_id, "await_text"
            pending_rejects[key] = pend
            _cancel_timeout(pend)
            try:
                p = await cq.message.reply_text(
                    f"Reply with a one-line reason to reject #{ref} "
                    "(shown to the guest), or send /reason <text>:",
                    reply_markup=ForceReply(selective=True))
                pend["prompt_id"] = p.message_id
            except Exception:
                pass
            _schedule_timeout(context.bot, client, pending_rejects, key)
            await cq.answer("Type the reason.")
            return

        if tag == "rc":                                 # ↩︎ Cancel -> state-checked
            pend = pending_rejects.pop(key, None) or _pend_from_message(
                cq.message, name, thread_id)
            _cancel_timeout(pend)
            pend["ref"], pend["chat_id"] = ref, cq.message.chat_id
            state = await _resolve_to_state(context.bot, client, pend)
            await cq.answer("Re-armed ✅/❌." if state == "pending"
                            else "State changed — see alert.",
                            show_alert=(state != "pending"))
            return

        await cq.answer()
    return on_action


async def _finish_typed_reason(client, pending_rejects, context, msg, uid, reason):
    """Shared completion for a typed reason (force-reply OR /reason command)."""
    key = (msg.chat_id, uid)
    pend = pending_rejects.pop(key, None)
    if not pend:
        return
    _cancel_timeout(pend)
    pend["chat_id"] = msg.chat_id
    ok, by = await _complete_reject(context.bot, client, pend, reason, uid)
    if ok:
        await msg.reply_text(f"❌ Rejected #{pend['ref']}.")
    elif by:
        await msg.reply_text(f"Already handled by {by}.")
    else:
        await msg.reply_text("Could not reject — please retry.")


def make_reject_reason_handler(client, pending_rejects):
    """A reply to the ✍️ Other prompt -> reject with the typed reason."""
    async def on_reason(update, context):
        msg = update.effective_message
        uid = update.effective_user.id
        if (msg.chat_id, uid) not in pending_rejects:
            return   # not a pending reject for this user -> ignore
        reason = (msg.text or "").strip()
        if not reason:
            await msg.reply_text("Please give a one-line reason to reject.")
            return
        await _finish_typed_reason(client, pending_rejects, context, msg, uid, reason)
    return on_reason


def make_reason_command_handler(client, pending_rejects):
    """/reason <text> — privacy-mode-proof typed reason from ANY topic, no reply
    threading needed. Completes the caller's pending reject."""
    async def cmd_reason(update, context):
        msg = update.effective_message
        uid = update.effective_user.id
        reason = " ".join(getattr(context, "args", None) or []).strip()
        if (msg.chat_id, uid) not in pending_rejects:
            await msg.reply_text("No pending rejection — tap ❌ Reject on an alert first.")
            return
        if not reason:
            await msg.reply_text("Usage: /reason <one-line reason shown to the guest>")
            return
        await _finish_typed_reason(client, pending_rejects, context, msg, uid, reason)
    return cmd_reason


def make_authorize_handler(client, owner_id):
    """/authorize <telegram_id> <manager|staff> <name> — OWNER only. Silent for
    non-owners (whitelist-first means only listed users reach here anyway)."""
    async def cmd_authorize(update, context):
        _allowed, role = await resolve_access(
            client, owner_id, update.effective_user.id)
        if role != "owner":
            return
        args = list(getattr(context, "args", None) or [])
        if len(args) < 2:
            await update.effective_message.reply_text(
                "Usage: /authorize <telegram_id> <manager|staff> <name>")
            return
        try:
            tid = int(args[0])
        except ValueError:
            await update.effective_message.reply_text("telegram_id must be a number.")
            return
        role_arg = args[1].lower()
        if role_arg not in ("manager", "staff"):
            await update.effective_message.reply_text("Role must be manager or staff.")
            return
        name = " ".join(args[2:]) or str(tid)
        status, body = await client.authorize(
            tid, role_arg, name, added_by=update.effective_user.id)
        if status == 200 and body.get("ok"):
            client.invalidate_whitelist(tid)     # take effect immediately
            await update.effective_message.reply_text(
                f"Authorized {name} ({tid}) as {role_arg}.")
        else:
            await update.effective_message.reply_text(
                "Could not authorize: " + str(body.get("error", "error")))
    return cmd_authorize


def make_revoke_handler(client, owner_id):
    """/revoke <telegram_id> — OWNER only."""
    async def cmd_revoke(update, context):
        _allowed, role = await resolve_access(
            client, owner_id, update.effective_user.id)
        if role != "owner":
            return
        args = list(getattr(context, "args", None) or [])
        if not args:
            await update.effective_message.reply_text("Usage: /revoke <telegram_id>")
            return
        try:
            tid = int(args[0])
        except ValueError:
            await update.effective_message.reply_text("telegram_id must be a number.")
            return
        status, body = await client.revoke(tid)
        if status == 200 and body.get("ok"):
            client.invalidate_whitelist(tid)
            await update.effective_message.reply_text(f"Revoked {tid}.")
        elif body.get("not_found"):
            await update.effective_message.reply_text(f"{tid} is not on the whitelist.")
        else:
            await update.effective_message.reply_text("Could not revoke.")
    return cmd_revoke


def make_whitelist_gate(client, owner_id):
    """Group -1 pre-handler: enforces whitelist-before-everything for EVERY update
    except /myid. Unlisted → stop propagation silently, so no downstream handler
    (present or future) ever runs for a stranger. Raises ApplicationHandlerStop.
    """
    async def gate(update, context):
        msg = getattr(update, "effective_message", None)
        text = (getattr(msg, "text", None) or "")
        if text.startswith("/myid"):
            return  # allow the pre-whitelist ID lookup through
        user = getattr(update, "effective_user", None)
        uid = getattr(user, "id", None)
        allowed, _role = await resolve_access(client, owner_id, uid)
        if not allowed:
            from telegram.ext import ApplicationHandlerStop
            raise ApplicationHandlerStop  # silent drop
    return gate
