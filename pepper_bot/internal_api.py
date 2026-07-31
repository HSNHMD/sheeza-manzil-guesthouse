"""Client for the Pepper internal API over the unix socket (bearer auth).

The bot holds no DB creds — the whitelist (and later booking/verify) all go
through here. httpx is imported lazily so the pure logic (gate/handlers) can be
unit-tested without httpx or a running socket.
"""

from __future__ import annotations

import time


class _TTLCache:
    """Tiny per-key TTL cache (the 60s whitelist cache)."""

    def __init__(self, ttl_seconds: int = 60):
        self._ttl = ttl_seconds
        self._store: dict = {}

    def get(self, key):
        hit = self._store.get(key)
        if hit and hit[0] > time.monotonic():
            return hit[1]
        return None

    def put(self, key, value):
        self._store[key] = (time.monotonic() + self._ttl, value)


class InternalAPIClient:
    def __init__(self, socket_path: str, token: str,
                 base_url: str = "http://pepper", ttl_seconds: int = 60):
        self.socket_path = socket_path
        self.token = token
        self.base_url = base_url.rstrip("/")
        self.cache = _TTLCache(ttl_seconds)

    async def whitelist(self, telegram_id: int) -> dict:
        """Return {'allowed': bool, 'role': str|None}. Cached for ttl_seconds.
        Never raises for a normal deny — only propagates transport errors so the
        caller can fail closed."""
        cached = self.cache.get(telegram_id)
        if cached is not None:
            return cached
        import httpx  # lazy
        transport = httpx.AsyncHTTPTransport(uds=self.socket_path)
        async with httpx.AsyncClient(transport=transport, timeout=5.0) as client:
            resp = await client.get(
                f"{self.base_url}/api/internal/pepper/whitelist/{telegram_id}",
                headers={"Authorization": f"Bearer {self.token}"},
            )
            data = (resp.json() if resp.status_code == 200
                    else {"allowed": False, "role": None})
        self.cache.put(telegram_id, data)
        return data
