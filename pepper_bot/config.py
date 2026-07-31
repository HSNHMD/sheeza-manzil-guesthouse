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
