"""Persistent map: booking reference -> Telegram message_id of its alert, so a
later slip.uploaded event can post the slip as a threaded reply even across a bot
restart. Bot-local state (StateDirectory); capped to avoid unbounded growth.
"""

from __future__ import annotations

import json
import os
import threading


class MsgIdStore:
    def __init__(self, path: str, cap: int = 500):
        self.path = path
        self.cap = cap
        self._lock = threading.Lock()

    def _load(self) -> dict:
        try:
            with open(self.path) as fh:
                return json.load(fh)
        except (FileNotFoundError, ValueError):
            return {}

    def get(self, ref):
        return self._load().get(str(ref))

    def set(self, ref, message_id):
        with self._lock:
            data = self._load()
            data[str(ref)] = message_id
            if len(data) > self.cap:                      # keep the newest `cap`
                for k in list(data.keys())[:-self.cap]:
                    del data[k]
            os.makedirs(os.path.dirname(self.path) or ".", exist_ok=True)
            tmp = f"{self.path}.tmp"
            with open(tmp, "w") as fh:
                json.dump(data, fh)
            os.replace(tmp, self.path)
