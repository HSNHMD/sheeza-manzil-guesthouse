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
