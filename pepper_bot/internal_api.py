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

    def invalidate(self, key):
        self._store.pop(key, None)


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

    def invalidate_whitelist(self, telegram_id):
        self.cache.invalidate(telegram_id)

    async def authorize(self, telegram_id, role, name, *, added_by=None):
        async with self._client() as client:
            resp = await client.post(self._url("/whitelist"), headers=self._auth(),
                                     json={"telegram_id": telegram_id, "role": role,
                                           "display_name": name, "added_by": added_by})
            return resp.status_code, self._json(resp)

    async def revoke(self, telegram_id):
        async with self._client() as client:
            resp = await client.post(self._url(f"/whitelist/{telegram_id}/revoke"),
                                     headers=self._auth())
            return resp.status_code, self._json(resp)

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

    async def hold_state(self, reference):
        """Authoritative current disposition — for ↩︎ Cancel / timeout re-arm
        decisions. Returns the JSON dict (state / armable / by / reason); falls
        back to a non-armable 'unknown' on any transport error (never re-arm)."""
        async with self._client() as client:
            resp = await client.get(self._url(f"/holds/state?reference={reference}"),
                                     headers=self._auth())
            if resp.status_code == 200:
                return self._json(resp)
            return {"state": "unknown", "armable": False}

    # --- target-aware verify/reject/state (hold OR booking) ---
    async def confirm_target(self, target, *, actor_id=None, actor_name=None):
        """✅ Verify a Target (hold -> confirm; booking -> pending_verification →
        confirmed). Returns (status_code, json)."""
        if target.kind == "booking":
            async with self._client() as client:
                resp = await client.post(
                    self._url(f"/bookings/{target.booking_id}/verify"),
                    headers=self._auth(),
                    json={"actor_id": actor_id, "actor_name": actor_name})
                return resp.status_code, self._json(resp)
        return await self.confirm_hold(target.ref, actor_id=actor_id,
                                       actor_name=actor_name)

    async def reject_target(self, target, reason, *, actor_id=None, actor_name=None):
        """❌ Reject a Target's slip (soft in both cases). Returns (status, json)."""
        if target.kind == "booking":
            async with self._client() as client:
                resp = await client.post(
                    self._url(f"/bookings/{target.booking_id}/reject"),
                    headers=self._auth(),
                    json={"reason": reason, "actor_id": actor_id,
                          "actor_name": actor_name})
                return resp.status_code, self._json(resp)
        return await self.reject_hold(target.ref, reason, actor_id=actor_id,
                                      actor_name=actor_name)

    async def target_state(self, target):
        """Authoritative disposition for a Target — the ↩︎ Cancel / timeout re-arm
        authority. Non-armable 'unknown' on any transport error (never re-arm)."""
        if target.kind == "booking":
            async with self._client() as client:
                resp = await client.get(
                    self._url(f"/bookings/{target.booking_id}/state"),
                    headers=self._auth())
                if resp.status_code == 200:
                    return self._json(resp)
                return {"state": "unknown", "armable": False}
        return await self.hold_state(target.ref)

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

    # ── guided-flow surface (availability / quote / create / slip / snapshots) ─
    async def availability(self, check_in, check_out, *, guests=1):
        """GET /availability -> list of room cards (advisory; no hold)."""
        async with self._client() as client:
            resp = await client.get(
                self._url(f"/availability?check_in={check_in}"
                          f"&check_out={check_out}&guests={guests}"),
                headers=self._auth())
            return resp.json().get("rooms", []) if resp.status_code == 200 else []

    async def quote(self, check_in, check_out, items, *, guests=1):
        async with self._client() as client:
            resp = await client.post(self._url("/quote"), headers=self._auth(),
                                     json={"check_in": check_in, "check_out": check_out,
                                           "items": items, "guests": guests})
            return self._json(resp) if resp.status_code == 200 else {}

    async def create_booking(self, body):
        """POST /bookings -> (status, json). status 201 ok / 400 validation /
        409 availability-vanished. The flow surfaces 4xx verbatim."""
        async with self._client() as client:
            resp = await client.post(self._url("/bookings"), headers=self._auth(),
                                     json=body)
            return resp.status_code, self._json(resp)

    async def booking_slip(self, booking_id, photo_bytes, filename):
        """POST /bookings/<id>/slip (multipart) -> (status, json)."""
        async with self._client() as client:
            resp = await client.post(
                self._url(f"/bookings/{booking_id}/slip"), headers=self._auth(),
                files={"slip": (filename, photo_bytes)})
            return resp.status_code, self._json(resp)

    async def flow_put(self, telegram_id, snapshot):
        async with self._client() as client:
            resp = await client.put(self._url(f"/flows/{telegram_id}"),
                                    headers=self._auth(), json=snapshot)
            return resp.status_code == 200

    async def flow_delete(self, telegram_id):
        async with self._client() as client:
            resp = await client.request(
                "DELETE", self._url(f"/flows/{telegram_id}"), headers=self._auth())
            return resp.status_code == 200

    async def flow_list(self):
        async with self._client() as client:
            resp = await client.get(self._url("/flows"), headers=self._auth())
            return resp.json().get("flows", []) if resp.status_code == 200 else []

    async def get_brand(self):
        """Bank/brand block for the success message — fetched, never hardcoded."""
        async with self._client() as client:
            resp = await client.get(self._url("/brand"), headers=self._auth())
            return self._json(resp) if resp.status_code == 200 else {}
