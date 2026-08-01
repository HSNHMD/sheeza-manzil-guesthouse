"""Command handlers — deliberately telegram/httpx-free so they unit-test with
plain mocks. Handlers use only `update.effective_user.id` and
`update.effective_message.reply_text(...)` (duck-typed).

Design under privacy-mode-ON: only commands, force-reply, and callbacks reach the
bot — no reliance on reading arbitrary group messages.
"""

from __future__ import annotations

from .gate import resolve_access


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


def make_action_callback(client, owner_id, pending_rejects):
    """Handle ✅/❌ taps. Role-gated (manager/owner only; staff -> ephemeral
    'requires manager', no state change). ✅ -> confirm_hold (idempotent; loser
    told who won). ❌ -> force-reply reason flow (see make_reject_reason_handler)."""
    from telegram import ForceReply

    async def on_action(update, context):
        cq = update.callback_query
        parts = (cq.data or "").split(":")
        if len(parts) != 3 or parts[0] != "pv":
            await cq.answer(); return
        _tag, action, ref = parts
        uid = cq.from_user.id
        name = cq.from_user.full_name or cq.from_user.first_name or "staff"
        _allowed, role = await resolve_access(client, owner_id, uid)
        if role not in ("manager", "owner"):
            await cq.answer("Verification requires manager role.", show_alert=True)
            return
        if action == "v":
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
        elif action == "r":
            prompt = await cq.message.reply_text(
                f"Reply with a one-line reason to reject #{ref}:",
                reply_markup=ForceReply(selective=True))
            pending_rejects[(cq.message.chat_id, uid)] = {
                "ref": ref, "alert_msg_id": cq.message.message_id,
                "alert_text": cq.message.text or cq.message.caption or "",
                "prompt_id": prompt.message_id, "name": name}
            await cq.answer("Reply with a reason.")
        else:
            await cq.answer()
    return on_action


def make_reject_reason_handler(client, pending_rejects):
    """The manager's force-reply reason -> reject the hold + edit the alert."""
    async def on_reason(update, context):
        msg = update.effective_message
        key = (msg.chat_id, update.effective_user.id)
        pend = pending_rejects.get(key)
        if not pend:
            return   # not a pending reject for this user -> ignore
        reason = (msg.text or "").strip()
        if not reason:
            await msg.reply_text("Please give a one-line reason to reject.")
            return
        pending_rejects.pop(key, None)
        status, body = await client.reject_hold(
            pend["ref"], reason, actor_id=update.effective_user.id,
            actor_name=pend["name"])
        if status == 200 and body.get("ok"):
            try:
                await context.bot.edit_message_text(
                    chat_id=msg.chat_id, message_id=pend["alert_msg_id"],
                    text=(pend["alert_text"]
                          + f"\n\n❌ REJECTED by {pend['name']}: {reason}").strip())
            except Exception:
                pass
            await msg.reply_text(f"❌ Rejected #{pend['ref']}.")
        elif body.get("already"):
            await msg.reply_text(f"Already handled by {body.get('by', 'someone')}.")
        else:
            await msg.reply_text("Could not reject — please retry.")
    return on_reason


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
