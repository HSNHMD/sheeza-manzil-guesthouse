"""Pure helpers for the Reservation Board prototype.

Splits view math (date ranges, booking placement, status → CSS class)
out of the route handler so it can be unit-tested without a Flask
context. Nothing here writes to the DB or calls external services.

The board is a tape-chart layout:
    rows    = rooms (sticky-left)
    columns = days (sticky-top)
    bars    = bookings spanning their stay
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from typing import Iterable, Optional


# ── View configuration ───────────────────────────────────────────────

# Span-in-days for each named view. Day view spans 1 day but renders
# extra-wide; the other views render the date range natively.
VIEW_SPANS = {
    'day':   1,
    '7d':    7,
    '14d':   14,
    '30d':   30,
}

DEFAULT_VIEW = '14d'

# Pixel width per day cell, by view. Tuned so the board feels dense
# but breathable across the four zoom levels.
VIEW_DAY_WIDTHS = {
    'day':   320,
    '7d':    140,
    '14d':   84,
    '30d':   44,
}


# ── Grouping + density toggles ───────────────────────────────────────

GROUPING_OPTIONS = ('none', 'floor', 'room_type')
DEFAULT_GROUPING = 'none'

DENSITY_OPTIONS = ('standard', 'compact', 'ultra')
DEFAULT_DENSITY = 'standard'

# Per-density day-width multipliers — tightened in the density-overhaul
# sprint so the three modes actually look meaningfully different.
# Standard stays at full base width; compact removes ~30%; ultra removes
# ~58%, making a 30-day × ultra row fit the entire month inside ~530 px.
# Multiplied with VIEW_DAY_WIDTHS to produce the final --day-w value.
DENSITY_DAY_WIDTH_MULT = {
    'standard': 1.00,
    'compact':  0.70,
    'ultra':    0.42,
}

# Per-density row height in px. The vertical-density sprint pushed
# Compact + Ultra tighter so visibly more room rows fit in a viewport
# without losing operational legibility:
#
#   Standard 48: comfortable, default for management-style review
#   Compact  28: ~70% more rooms-per-screen; rail still readable
#   Ultra    22: maximum density; rail collapses to room# + 3-letter
#                code; bars use minimum vertical padding so the
#                colored block dominates the row
#
# Tap-friendliness floor: 22 px is below Apple's 44-pt guideline but
# this is a power-user desktop/tablet view; the drawer-open click
# target on the bar is generous (the whole row is one tap).
DENSITY_ROW_HEIGHT_PX = {
    'standard': 48,
    'compact':  28,
    'ultra':    22,
}

# Per-density room-rail width in px. The room rail is the leftmost
# column carrying room number + type + status. In ultra mode we drop
# the verbose meta sub-line via CSS, so the rail can be much narrower.
DENSITY_ROOM_RAIL_PX = {
    'standard': 232,
    'compact':  176,
    'ultra':    104,
}


# Three-letter abbreviations for the rail in dense modes. Anything not
# in the table renders the first 3 chars of the type name uppercased.
_ROOM_TYPE_SHORT = {
    'standard':         'STD',
    'standard room':    'STD',
    'deluxe':           'DLX',
    'deluxe room':      'DLX',
    'twin':             'TWN',
    'twin room':        'TWN',
    'family':           'FAM',
    'family room':      'FAM',
    'suite':            'STE',
    'junior suite':     'JST',
    'penthouse':        'PNT',
}


def room_type_short(name: str) -> str:
    """Return a 3-letter rail label for the given room type string.

    Falls back to the first 3 chars uppercased when the type isn't in
    the curated abbreviation table. Never returns an empty string —
    we still want SOMETHING in the rail under ultra mode.
    """
    if not name:
        return '???'
    n = name.strip().lower()
    if n in _ROOM_TYPE_SHORT:
        return _ROOM_TYPE_SHORT[n]
    cleaned = ''.join(ch for ch in n if ch.isalpha())[:3]
    return (cleaned or n[:3]).upper()


def normalize_grouping(value) -> str:
    if value in GROUPING_OPTIONS:
        return value
    return DEFAULT_GROUPING


def normalize_density(value) -> str:
    if value in DENSITY_OPTIONS:
        return value
    return DEFAULT_DENSITY


def group_label_for(room, grouping: str) -> str:
    """Return the human-readable group label for a room under ``grouping``."""
    if grouping == 'floor':
        floor = getattr(room, 'floor', None)
        if floor is None:
            return 'Unassigned floor'
        return f'Floor {floor}'
    if grouping == 'room_type':
        rt = (getattr(room, 'room_type', None) or '').strip()
        return rt or 'Untyped'
    return ''


def group_rooms(rooms, grouping: str) -> list:
    """Split a sorted list of rooms into [(label, [rooms]), …] groups.

    For ``grouping='none'`` returns a single ('', rooms) tuple, which
    the template handles as "no header rendered, single block".
    Pure function — never raises on empty input.
    """
    if not rooms:
        return []
    if grouping not in ('floor', 'room_type'):
        return [('', list(rooms))]
    out = []
    current_label = None
    current_bucket = []
    for r in rooms:
        label = group_label_for(r, grouping)
        if label != current_label:
            if current_bucket:
                out.append((current_label, current_bucket))
            current_label = label
            current_bucket = [r]
        else:
            current_bucket.append(r)
    if current_bucket:
        out.append((current_label, current_bucket))
    return out


def filter_state_summary(*, floor=None, room_type=None,
                         booking_status=None, payment_status=None,
                         search=None) -> dict:
    """Return a small status summary describing how many filters are
    active and a friendly label for each active one."""
    active = []
    if floor not in (None, ''):
        active.append(('floor', f'Floor {floor}'))
    if room_type:
        active.append(('room_type', room_type))
    if booking_status:
        active.append(('booking_status',
                       booking_status.replace('_', ' ').title()))
    if payment_status:
        active.append(('payment_status',
                       payment_status.replace('_', ' ').title()))
    if search:
        active.append(('search', f'"{search}"'))
    return {'active': active, 'count': len(active)}

# Booking bar minimum readable width in pixels — below this, only the
# room-letter / first initial is shown.
MIN_BAR_TEXT_WIDTH = 60


def normalize_view(view: Optional[str]) -> str:
    """Coerce a view string into one of the canonical view names."""
    if view in VIEW_SPANS:
        return view
    return DEFAULT_VIEW


def view_span_days(view: str) -> int:
    return VIEW_SPANS.get(view, VIEW_SPANS[DEFAULT_VIEW])


def view_day_width_px(view: str) -> int:
    return VIEW_DAY_WIDTHS.get(view, VIEW_DAY_WIDTHS[DEFAULT_VIEW])


def effective_day_width_px(view: str, density: str = DEFAULT_DENSITY) -> int:
    """Day-cell width in px after applying the density multiplier.

    Use this from the route handler / template instead of the raw
    `view_day_width_px` so 'compact' and 'ultra' modes actually narrow
    the columns. Always returns an integer ≥ 18 — below that the day
    labels stop being legible even on desktop.
    """
    base = view_day_width_px(view)
    mult = DENSITY_DAY_WIDTH_MULT.get(density, 1.0)
    return max(18, int(round(base * mult)))


def density_row_height_px(density: str = DEFAULT_DENSITY) -> int:
    """Row height in px for the chosen density."""
    return DENSITY_ROW_HEIGHT_PX.get(density, DENSITY_ROW_HEIGHT_PX[DEFAULT_DENSITY])


def density_room_rail_px(density: str = DEFAULT_DENSITY) -> int:
    """Room-rail (leftmost column) width in px for the chosen density.

    Ultra trims the rail aggressively because the room-meta sub-line
    is hidden via CSS in that mode — the rail only needs to fit the
    room number and a 3-letter type abbreviation.
    """
    return DENSITY_ROOM_RAIL_PX.get(density, DENSITY_ROOM_RAIL_PX[DEFAULT_DENSITY])


# ── Date range helpers ───────────────────────────────────────────────

def parse_start_date(value, default=None) -> date:
    """Parse YYYY-MM-DD into a date. Falls back to default (or today)."""
    if isinstance(value, date):
        return value
    if not value:
        return default or date.today()
    try:
        return datetime.strptime(value, '%Y-%m-%d').date()
    except (ValueError, TypeError):
        return default or date.today()


def date_range(start: date, span: int) -> list:
    """Return ``span`` consecutive dates beginning at ``start``."""
    if span <= 0:
        span = 1
    return [start + timedelta(days=i) for i in range(span)]


def shift_range(start: date, span: int, direction: int) -> date:
    """Compute the new start date after a prev/next click."""
    return start + timedelta(days=span * (1 if direction > 0 else -1))


# ── Booking placement on the grid ────────────────────────────────────

@dataclass
class BookingBar:
    """A single booking ready to render as a horizontal bar."""

    booking_id:     int
    booking_ref:    str
    guest_name:     str
    num_guests:     int
    nights:         int
    check_in:       date
    check_out:      date

    # Grid positioning (1-indexed CSS grid columns)
    grid_col_start: int
    grid_col_span:  int

    # Status / classification
    booking_status:   str
    payment_status:   str
    bar_color_class:  str
    accent_class:     str
    label:            str
    short_label:      str

    # Computed flags for hover / accessibility
    starts_in_range:  bool = True
    ends_in_range:    bool = True
    visible_days:     int = 0


def _clip_dates(check_in: date, check_out: date,
                window_start: date, window_end: date):
    """Return (effective_start, effective_end, starts_in, ends_in).

    The board grid uses ONE column per *night* — a stay from 2026-04-30
    check-in to 2026-05-02 check-out occupies the columns for 2026-04-30
    and 2026-05-01 (two nights). Check-out day is NOT a column the bar
    occupies, matching how front-desk tape charts work.
    """
    if check_out <= window_start or check_in >= window_end:
        return None, None, False, False
    eff_start = max(check_in, window_start)
    # check_out is exclusive; clip to window_end (also exclusive).
    eff_end = min(check_out, window_end)
    starts_in = check_in >= window_start
    ends_in   = check_out <= window_end
    return eff_start, eff_end, starts_in, ends_in


def make_booking_bar(booking,
                     window_start: date,
                     window_end: date) -> Optional[BookingBar]:
    """Build a BookingBar for a single Booking ORM row.

    Returns None if the booking does not overlap the window. Pure
    function — never raises on a well-formed Booking.
    """
    if booking is None or booking.check_in_date is None \
            or booking.check_out_date is None:
        return None

    eff_start, eff_end, starts_in, ends_in = _clip_dates(
        booking.check_in_date, booking.check_out_date,
        window_start, window_end,
    )
    if eff_start is None:
        return None

    # CSS grid columns are 1-indexed; column 1 is the room column,
    # so date columns start at 2.
    col_start = (eff_start - window_start).days + 2
    col_span  = max(1, (eff_end - eff_start).days)

    booking_status = (booking.status or '').strip()
    inv = getattr(booking, 'invoice', None)
    payment_status = (getattr(inv, 'payment_status', None)
                      if inv else None) or 'not_received'

    bar_color = bar_color_class(booking_status)
    accent    = payment_accent_class(payment_status)

    guest = booking.guest
    full_name = (f'{guest.first_name} {guest.last_name}'.strip()
                 if guest else 'Unknown')
    last_name = (guest.last_name if guest else None) or full_name
    nights = (booking.check_out_date - booking.check_in_date).days

    return BookingBar(
        booking_id=booking.id,
        booking_ref=booking.booking_ref or '',
        guest_name=full_name,
        num_guests=getattr(booking, 'num_guests', None) or 1,
        nights=nights,
        check_in=booking.check_in_date,
        check_out=booking.check_out_date,
        grid_col_start=col_start,
        grid_col_span=col_span,
        booking_status=booking_status,
        payment_status=payment_status,
        bar_color_class=bar_color,
        accent_class=accent,
        label=full_name,
        short_label=last_name,
        starts_in_range=starts_in,
        ends_in_range=ends_in,
        visible_days=col_span,
    )


# ── Status → CSS class mapping ───────────────────────────────────────

# Bar background by booking lifecycle. Tailwind utility classes only —
# no external CSS bundle needed. Each entry is (bg, text, border, hover).
_BAR_COLOR_CLASSES = {
    'new_request':       'bg-violet-100 text-violet-900 border-violet-300 hover:bg-violet-200',
    'pending_payment':   'bg-amber-100 text-amber-900 border-amber-300 hover:bg-amber-200',
    'payment_uploaded':  'bg-yellow-100 text-yellow-900 border-yellow-400 hover:bg-yellow-200',
    'payment_verified':  'bg-sky-100 text-sky-900 border-sky-300 hover:bg-sky-200',
    'confirmed':         'bg-blue-100 text-blue-900 border-blue-400 hover:bg-blue-200',
    'checked_in':        'bg-emerald-100 text-emerald-900 border-emerald-400 hover:bg-emerald-200',
    'checked_out':       'bg-gray-100 text-gray-700 border-gray-300 hover:bg-gray-200',
    'cancelled':         'bg-rose-50 text-rose-700 border-rose-200 hover:bg-rose-100 line-through',
    'rejected':          'bg-rose-50 text-rose-700 border-rose-200 hover:bg-rose-100 line-through',
}

# Legacy / unknown statuses fall back to slate.
_BAR_FALLBACK_CLASS = 'bg-slate-100 text-slate-700 border-slate-300 hover:bg-slate-200'

# Right-edge accent indicating payment trouble.
_PAYMENT_ACCENT_CLASSES = {
    'verified':       'after:bg-emerald-500',
    'pending_review': 'after:bg-amber-500',
    'mismatch':       'after:bg-red-500',
    'rejected':       'after:bg-red-500',
    'not_received':   '',
}


def bar_color_class(booking_status: Optional[str]) -> str:
    return _BAR_COLOR_CLASSES.get(
        (booking_status or '').strip(), _BAR_FALLBACK_CLASS,
    )


def payment_accent_class(payment_status: Optional[str]) -> str:
    return _PAYMENT_ACCENT_CLASSES.get(
        (payment_status or '').strip(), '',
    )


# ── Room status / occupancy ──────────────────────────────────────────

def is_room_occupied_today(room, today: date, bookings) -> bool:
    """True iff any booking on this room covers ``today`` and is
    in an "active" state (checked_in, or confirmed and within window)."""
    for b in bookings:
        if b.room_id != room.id:
            continue
        if b.check_in_date is None or b.check_out_date is None:
            continue
        if b.check_in_date <= today < b.check_out_date:
            if (b.status or '').strip() in (
                'checked_in', 'confirmed', 'payment_verified',
            ):
                return True
    return False


def room_status_badge(room, today: date, bookings) -> dict:
    """Return ``{label, dot_class, text_class}`` describing room state.

    Combines the room's intrinsic status (room.status, room.housekeeping_status)
    with derived occupancy.
    """
    intrinsic = (getattr(room, 'status', None) or '').strip().lower()
    hk = (getattr(room, 'housekeeping_status', None) or '').strip().lower()

    if intrinsic in ('out_of_order', 'maintenance'):
        return {'label': 'Out of order',
                'dot_class':  'bg-slate-700',
                'text_class': 'text-slate-700'}
    if intrinsic == 'blocked':
        return {'label': 'Blocked',
                'dot_class':  'bg-slate-500',
                'text_class': 'text-slate-700'}

    occupied = is_room_occupied_today(room, today, bookings)
    if occupied:
        if hk == 'dirty':
            return {'label': 'Occupied (dirty)',
                    'dot_class':  'bg-orange-500',
                    'text_class': 'text-orange-700'}
        return {'label': 'Occupied',
                'dot_class':  'bg-emerald-500',
                'text_class': 'text-emerald-700'}

    if hk == 'dirty':
        return {'label': 'Dirty',
                'dot_class':  'bg-orange-500',
                'text_class': 'text-orange-700'}
    return {'label': 'Clean',
            'dot_class':  'bg-emerald-500',
            'text_class': 'text-gray-500'}


# ── Aggregate stats for mobile / quick-glance card view ──────────────

def todays_arrivals(bookings, today: date) -> list:
    return [b for b in bookings
            if b.check_in_date == today
            and (b.status or '').strip() not in ('cancelled', 'rejected')]


def todays_departures(bookings, today: date) -> list:
    return [b for b in bookings
            if b.check_out_date == today
            and (b.status or '').strip() not in ('cancelled', 'rejected')]


def in_house_today(bookings, today: date) -> list:
    return [b for b in bookings
            if b.check_in_date is not None
            and b.check_out_date is not None
            and b.check_in_date <= today < b.check_out_date
            and (b.status or '').strip() in (
                'checked_in', 'confirmed', 'payment_verified',
            )]


def pending_payment(bookings) -> list:
    return [b for b in bookings
            if (b.status or '').strip() in (
                'pending_payment', 'new_request', 'payment_uploaded',
            )]
