"""Runtime config from env (all secrets come from /etc/pepper/pepper.env)."""

from __future__ import annotations

import os


class Config:
    def __init__(self):
        self.bot_token = os.environ["TELEGRAM_BOT_TOKEN"]
        self.internal_token = os.environ["PEPPER_INTERNAL_TOKEN"]
        self.socket_path = os.environ.get(
            "PEPPER_SOCKET", "/run/pepper/pepper.sock")
        # Owner is env-only so a DB compromise can't grant owner (spec §4.2/§8).
        self.owner_id = os.environ.get("PEPPER_OWNER_ID") or None
        # Bot-local state (systemd StateDirectory) for topic bindings.
        self.state_dir = os.environ.get("PEPPER_STATE_DIR", "/var/lib/pepper-bot")
        self.topics_path = os.path.join(self.state_dir, "topics.json")
        self.msgids_path = os.path.join(self.state_dir, "msgids.json")
        self.poll_interval = float(os.environ.get("PEPPER_POLL_INTERVAL", "5"))
        # OpenRouter (Gemini Flash) key for date/nationality PARSING only. When
        # unset the flow degrades to strict-format parsing (never breaks). Read
        # by pepper_bot.llm directly from env (PEPPER_OPENROUTER_KEY); surfaced
        # here for visibility / a future disable flag.
        self.openrouter_key = os.environ.get("PEPPER_OPENROUTER_KEY") or None
