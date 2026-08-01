"""Verify/reject TARGET — the thing a ✅/❌ acts on: a portal Hold (by reference)
or a bot-created Booking (by id). Phase 3 shipped hold-only with a bare token in
the callback data (`pv:v:<ref>`); Phase 2 generalises the SAME verify/reject UX
over bookings.

Wire compatibility is a hard requirement (every Phase 3 hold test stays green):
a HOLD still serialises to the BARE reference (`FAY9VDPF`), so `pv:v:FAY9VDPF`
is unchanged. A BOOKING serialises to `b:<id>` (`b:42`), giving `pv:v:b:42`. An
explicit hold form `h:<ref>` is also parsed (forward-looking), but never emitted
so nothing regresses.

Telegram caps callback_data at 64 bytes; `pv:rr:<code>:<token>` with a booking
token `b:<id>` stays well under that.
"""

from __future__ import annotations


class Target:
    __slots__ = ('kind', 'ref', 'booking_id')

    def __init__(self, kind, *, ref=None, booking_id=None):
        self.kind = kind                 # 'hold' | 'booking'
        self.ref = ref                   # hold session reference (8-char)
        self.booking_id = booking_id     # booking primary key

    @classmethod
    def hold(cls, ref):
        return cls('hold', ref=ref)

    @classmethod
    def booking(cls, booking_id):
        return cls('booking', booking_id=int(booking_id))

    def token(self) -> str:
        """The callback-data token. Hold => bare ref (Phase 3 wire-compatible);
        booking => 'b:<id>'."""
        if self.kind == 'booking':
            return f'b:{self.booking_id}'
        return self.ref

    def label(self) -> str:
        """Human '#…' label for alert lines."""
        return f'#{self.booking_id}' if self.kind == 'booking' else f'#{self.ref}'

    def __eq__(self, other):
        return (isinstance(other, Target) and self.kind == other.kind
                and self.ref == other.ref and self.booking_id == other.booking_id)

    def __repr__(self):
        return f'<Target {self.kind} {self.ref or self.booking_id}>'


def parse_token(parts) -> Target | None:
    """Parse the trailing token of a split callback-data list into a Target.

    `parts` is the remainder AFTER the tag, e.g. for `pv:v:b:42` the caller passes
    ['b', '42']; for the legacy `pv:v:FAY9VDPF` it passes ['FAY9VDPF']; for the
    explicit `pv:v:h:FAY9VDPF` it passes ['h', 'FAY9VDPF'].
    Returns None on a malformed token.
    """
    if not parts:
        return None
    if len(parts) >= 2 and parts[0] == 'b':
        try:
            return Target.booking(parts[1])
        except (ValueError, TypeError):
            return None
    if len(parts) >= 2 and parts[0] == 'h':
        return Target.hold(parts[1]) if parts[1] else None
    # bare token -> hold reference (Phase 3 compat)
    return Target.hold(parts[0]) if parts[0] else None
