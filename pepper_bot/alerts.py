"""Alert text formatting (§7.2) — pure functions, unit-testable.

The alert fields are assembled by the internal API (the bot holds no PMS logic).
These only lay out text. Deliberately NO id/passport numbers.
"""

from __future__ import annotations

from datetime import datetime


def _fmt_deadline(iso):
    if not iso:
        return None
    try:
        return datetime.fromisoformat(iso).strftime('%b %d, %H:%M UTC')
    except ValueError:
        return iso


def format_booking_created(a: dict) -> str:
    src = 'WEB PORTAL' if a.get('source') == 'portal' else 'via bot'
    total_line = f"Total: MVR {a.get('total', 0):.0f}"
    dl = _fmt_deadline(a.get('deadline'))
    if dl:
        total_line += f" · Hold expires: {dl}"
    pay = '🧾 slip uploaded' if a.get('has_slip') else '⏳ awaiting slip'
    return "\n".join([
        f"🆕 Booking #{a.get('ref', '?')} — {src}",
        f"Guest: {a.get('guest_name', '—')} "
        f"({a.get('nationality', '—')} — Green Tax {a.get('green_tax', 'unknown')})",
        f"Stay: {a.get('check_in')} → {a.get('check_out')} · "
        f"{a.get('nights', '?')} night(s) · {a.get('rooms', '—')}",
        f"Guests: {a.get('adults', '?')} adult(s), {a.get('children', 0)} child(ren)",
        total_line,
        f"Payment: {pay}",
    ])


def format_slip_caption(ref, total=None) -> str:
    cap = f"🧾 Slip received for #{ref}"
    if total:
        cap += f"\nAmount due: MVR {total:.0f} — check slip against this"
    return cap
