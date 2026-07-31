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
