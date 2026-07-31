"""Topic-binding store — persists which Telegram forum topic is Alerts / New
Booking / General, captured by /bindtopics. Bot-LOCAL state (a JSON file in the
systemd StateDirectory), NOT the PMS DB: topic IDs are the bot's own operational
config, not property data, so this doesn't need the internal API / DB creds.

Atomic writes (tmp + os.replace) so a crash mid-write can't corrupt it.
"""

from __future__ import annotations

import json
import os
import threading


class TopicStore:
    def __init__(self, path: str):
        self.path = path
        self._lock = threading.Lock()

    def _load(self) -> dict:
        try:
            with open(self.path) as fh:
                return json.load(fh)
        except (FileNotFoundError, ValueError):
            return {}

    def all(self) -> dict:
        return self._load()

    def get(self, label: str):
        return self._load().get(label)

    def set_topic(self, label: str, chat_id: int, thread_id: int) -> dict:
        with self._lock:
            data = self._load()
            data[label] = {"chat_id": chat_id, "thread_id": thread_id}
            os.makedirs(os.path.dirname(self.path) or ".", exist_ok=True)
            tmp = f"{self.path}.tmp"
            with open(tmp, "w") as fh:
                json.dump(data, fh)
            os.replace(tmp, self.path)   # atomic
            return data[label]
