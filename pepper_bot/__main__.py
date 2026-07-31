"""Pepper bot entrypoint — long-polling, single instance.

`python -m pepper_bot`. Runtime-only (imports python-telegram-bot); the pure
logic in gate/handlers is tested separately without PTB.

Resilience: ONE poller (systemd single instance) so Telegram never sees two
getUpdates loops (409). PTB's updater auto-retries getUpdates through network
blips. `drop_pending_updates=True` means a restart never re-processes or
duplicates a backlog of old updates — alert durability is the OUTBOX's job
(Phase 0), not getUpdates. An error handler swallows handler errors so one bad
update can't crash the loop.
"""

from __future__ import annotations

import logging

from telegram.ext import Application, CommandHandler, TypeHandler

from .config import Config
from .internal_api import InternalAPIClient
from .topics import TopicStore
from .handlers import (cmd_myid, make_ping_handler, make_whitelist_gate,
                       make_bindtopics_handler, make_topics_handler)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s %(message)s",
)
# CRITICAL: httpx/httpcore log the full request URL at INFO, and Telegram URLs
# embed the bot token (…/bot<TOKEN>/getUpdates). Silence them so the token NEVER
# lands in logs (journald or elsewhere).
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("httpcore").setLevel(logging.WARNING)
log = logging.getLogger("pepper_bot")


async def _on_error(update, context):
    # Never let a handler error crash the poller. No PII at INFO.
    log.warning("handler error: %s", type(context.error).__name__)


def build_application(cfg: Config | None = None) -> Application:
    cfg = cfg or Config()
    client = InternalAPIClient(cfg.socket_path, cfg.internal_token)
    store = TopicStore(cfg.topics_path)
    app = Application.builder().token(cfg.bot_token).build()
    # group -1: whitelist-before-everything (except /myid).
    app.add_handler(TypeHandler(object, make_whitelist_gate(client, cfg.owner_id)),
                    group=-1)
    # group 0: the actual commands.
    app.add_handler(CommandHandler("myid", cmd_myid))
    app.add_handler(CommandHandler("ping", make_ping_handler(client, cfg.owner_id)))
    app.add_handler(CommandHandler("bindtopics",
                                   make_bindtopics_handler(client, cfg.owner_id, store)))
    app.add_handler(CommandHandler("topics",
                                   make_topics_handler(client, cfg.owner_id, store)))
    app.add_error_handler(_on_error)
    return app


def main():
    app = build_application()
    log.info("pepper bot starting (long-poll, single instance)")
    app.run_polling(drop_pending_updates=True,
                    allowed_updates=["message", "callback_query"])


if __name__ == "__main__":
    main()
