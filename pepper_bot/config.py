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
        # Single-dictation extraction runs on K3 (moonshotai/kimi-k3) THROUGH the
        # Hermes gateway (the gateway holds the pooled upstream credential — Pepper
        # never holds an OpenRouter key). Config-driven; the live endpoint + a
        # `pepper-k3` alias + reachability are DEPLOY-time wiring (#23). When the
        # flag is off (or base_url/token unset) the flow runs the strict step-by-step
        # fallback — booking creation never depends on the LLM being reachable.
        self.hermes_base_url = os.environ.get("PEPPER_HERMES_BASE_URL") or ""
        self.hermes_model = os.environ.get("PEPPER_HERMES_MODEL",
                                           "moonshotai/kimi-k3")
        # Gateway auth token (bearer). Distinct from the pooled upstream key.
        self.hermes_token = os.environ.get("PEPPER_HERMES_TOKEN") or ""
        self.llm_enabled = (os.environ.get("PEPPER_LLM_ENABLED", "").strip().lower()
                            in ("1", "true", "yes", "on"))
