"""Authorization gate — whitelist-before-everything.

`resolve_access` is the single choke point: owner is checked from env first (a DB
compromise can't grant owner), then the internal-API whitelist. Any transport
error FAILS CLOSED (deny) — an unreachable API must never accidentally authorize.
"""

from __future__ import annotations


async def resolve_access(client, owner_id, telegram_id):
    """Return (allowed: bool, role: str|None)."""
    if owner_id and str(telegram_id) == str(owner_id):
        return True, "owner"
    try:
        data = await client.whitelist(telegram_id)
    except Exception:
        return False, None  # fail closed
    return bool(data.get("allowed")), data.get("role")
