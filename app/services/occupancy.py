"""BEv2 occupancy pricing — capacity validation + extra-person fee.

Per-room-type config lives on RoomType:
  base_occupancy   — guests included in the room rate (default 2)
  max_occupancy    — HARD cap per room (existing column)
  extra_person_fee — MVR/night charged per guest above base, per room

Single source of truth for three call sites: portal.submit (validation +
block message), the guest-step summary (fee line + live client hint), and
group_booking (itemized folio charge). Keep them all going through here so
the number the guest sees equals the number the folio charges.

Policy (flagged for owner, do NOT build the exemption now): children count
toward occupancy AND toward the extra-person fee. The config point exists
(extra_person_fee per type) if under-N-free / half-fee is wanted later.

Fee = the CHEAPEST legal distribution of the extra guests across the selected
rooms' spare seats (max-base per room): fill the lowest-fee seats first. By
construction extras <= total spare seats whenever the capacity check passes,
so the distribution always fits.
"""

from __future__ import annotations

import math


def _cfg(rt):
    """(base, max, fee) for a RoomType, defensively defaulted (base 2, max>=base)."""
    base = int(getattr(rt, 'base_occupancy', None) or 2)
    mx = int(getattr(rt, 'max_occupancy', None) or 2)
    if mx < base:
        mx = base
    fee = float(getattr(rt, 'extra_person_fee', None) or 0.0)
    return base, mx, fee


def compute(items, total_guests, nights):
    """Occupancy capacity + extra-person fee for a selection.

    items: [{'room_type_id': int, 'qty': int}] ; total_guests, nights: int.
    Pure read (loads the RoomType rows). Returns a dict with:
      capacity      Σ(max_occupancy × qty)  — the hard cap
      base_total    Σ(base_occupancy × qty) — guests included free
      extras        max(0, total_guests − base_total)
      fee_total     cheapest-distribution extra-person charge (× nights)
      breakdown     [{fee, persons, nights, amount}] grouped by fee tier
      slot_fees     sorted per-seat fees (for the live client hint)
      over_capacity total_guests > capacity
      min_rooms     ceil(total_guests / max_per_room) — the suggestion
      max_per_room  the largest single-room cap among the picked types
    """
    from ..models import RoomType
    ids = [int(i['room_type_id']) for i in items if int(i.get('qty', 0)) > 0]
    rts = ({rt.id: rt for rt in RoomType.query.filter(RoomType.id.in_(ids)).all()}
           if ids else {})
    nights = max(1, int(nights))
    total_guests = int(total_guests)

    capacity = base_total = max_per_room = 0
    slot_fees = []  # one entry per spare seat across all selected rooms
    for i in items:
        qty = int(i.get('qty', 0))
        if qty <= 0:
            continue
        rt = rts.get(int(i['room_type_id']))
        base, mx, fee = _cfg(rt) if rt is not None else (2, 2, 0.0)
        capacity += mx * qty
        base_total += base * qty
        max_per_room = max(max_per_room, mx)
        slot_fees.extend([fee] * ((mx - base) * qty))

    extras = max(0, total_guests - base_total)
    slot_fees.sort()
    charged = slot_fees[:extras]  # extras <= len(slot_fees) once capacity passes

    tiers = {}
    for fee in charged:
        tiers[fee] = tiers.get(fee, 0) + 1
    breakdown = [{'fee': fee, 'persons': n, 'nights': nights,
                  'amount': round(fee * n * nights, 2)}
                 for fee, n in sorted(tiers.items())]
    fee_total = round(sum(b['amount'] for b in breakdown), 2)

    return {
        'capacity': capacity,
        'base_total': base_total,
        'extras': extras,
        'fee_total': fee_total,
        'breakdown': breakdown,
        'slot_fees': slot_fees,
        'over_capacity': (total_guests > capacity) if capacity else False,
        'min_rooms': math.ceil(total_guests / max_per_room) if max_per_room else 0,
        'max_per_room': max_per_room,
        'nights': nights,
    }


def block_message(total_guests, max_per_room, min_rooms):
    """The guest-facing over-capacity message (plain, actionable)."""
    return (f'A maximum of {max_per_room} guests can stay in one room. '
            f'For {total_guests} guests, please select at least '
            f'{min_rooms} rooms.')
