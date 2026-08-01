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

    def _auth(self):
        return {"Authorization": f"Bearer {self.token}"}

    def _client(self):
        import httpx  # lazy
        return httpx.AsyncClient(
            transport=httpx.AsyncHTTPTransport(uds=self.socket_path), timeout=10.0)

    def _url(self, path):
        return f"{self.base_url}/api/internal/pepper{path}"

    async def whitelist(self, telegram_id: int) -> dict:
        """Return {'allowed': bool, 'role': str|None}. Cached for ttl_seconds.
        Never raises for a normal deny — only propagates transport errors so the
        caller can fail closed."""
        cached = self.cache.get(telegram_id)
        if cached is not None:
            return cached
        async with self._client() as client:
            resp = await client.get(self._url(f"/whitelist/{telegram_id}"),
                                    headers=self._auth())
            data = (resp.json() if resp.status_code == 200
                    else {"allowed": False, "role": None})
        self.cache.put(telegram_id, data)
        return data

    async def outbox_undelivered(self) -> list:
        async with self._client() as client:
            resp = await client.get(self._url("/outbox?undelivered=1"),
                                    headers=self._auth())
            return resp.json().get("events", []) if resp.status_code == 200 else []

    async def mark_delivered(self, event_id) -> bool:
        async with self._client() as client:
            resp = await client.post(self._url(f"/outbox/{event_id}/delivered"),
                                     headers=self._auth())
            return resp.status_code == 200

    async def confirm_hold(self, reference, *, actor_id=None, actor_name=None):
        """✅ Verify — confirm the pending hold. Returns (status_code, json)."""
        async with self._client() as client:
            resp = await client.post(self._url("/holds/verify"), headers=self._auth(),
                                     json={"reference": reference, "actor_id": actor_id,
                                           "actor_name": actor_name})
            return resp.status_code, self._json(resp)

    async def reject_hold(self, reference, reason, *, actor_id=None, actor_name=None):
        """❌ Reject — release the pending hold with a reason. Returns (status, json)."""
        async with self._client() as client:
            resp = await client.post(self._url("/holds/reject"), headers=self._auth(),
                                     json={"reference": reference, "reason": reason,
                                           "actor_id": actor_id, "actor_name": actor_name})
            return resp.status_code, self._json(resp)

    @staticmethod
    def _json(resp):
        try:
            return resp.json()
        except Exception:
            return {}

    async def slip_bytes(self, *, reference=None, booking_id=None):
        """Return (bytes, content_type) for the slip image, or None."""
        q = (f"reference={reference}" if reference
             else f"booking_id={booking_id}")
        async with self._client() as client:
            resp = await client.get(self._url(f"/slip?{q}"), headers=self._auth())
            if resp.status_code == 200:
                return resp.content, resp.headers.get("content-type", "image/jpeg")
            return None
