"""Deterministic parsing helpers for the guided booking flow — dates + nationality.

This module is the DETERMINISTIC strict/loose parser (`parse_date_strict`,
`normalize_nationality`) that needs NO network and NO LLM. It is the source of
truth for correctness and the ALWAYS-AVAILABLE path used by the strict step-by-step
fallback flow: staff type `YYYY-MM-DD` / an ISO country code and booking creation
never depends on any model being reachable.

The single-dictation LLM path (K3 via the Hermes gateway) lives in
`pepper_bot.extract` — it is the ONLY LLM path in Pepper. There is deliberately no
per-field LLM assist here anymore (the old OpenRouter/Gemini date+nationality
helper was removed with the direct-key code path): one LLM surface, one ledger tag.

INJECTION POSTURE (spec §6.3): all user text is DATA, never instructions. This
module only ever maps a scalar (a date phrase / a nationality token) and the flow
ECHOES the result back for human confirmation before it enters the summary card.

`parse_date` / `resolve_nationality` return a `Parsed` carrying the value, the
method used ('strict' | None), and whether confirmation is advised.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date, datetime, timedelta


# ── Result type ─────────────────────────────────────────────────────────────

@dataclass
class Parsed:
    value: object            # a date, an ISO country code, or None
    method: str | None       # 'strict' | None (unparsed) — LLM assist removed
    needs_confirm: bool       # echo-for-confirmation advised (always True when parsed)

    @property
    def ok(self) -> bool:
        return self.value is not None


# ── Nationality ─────────────────────────────────────────────────────────────

# Common synonyms → ISO-ish code the PMS uses. Deliberately small + explicit;
# 'MDV' matches the internal API's Green-Tax exemption set. Anything already a
# 2/3-letter code is upper-cased and passed through.
_NAT_SYNONYMS = {
    'maldivian': 'MDV', 'maldives': 'MDV', 'mv': 'MDV', 'mdv': 'MDV',
    'local': 'MDV', 'dhivehi': 'MDV',
    'indian': 'IND', 'india': 'IND',
    'sri lankan': 'LKA', 'srilankan': 'LKA', 'sri lanka': 'LKA',
    'bangladeshi': 'BGD', 'bangladesh': 'BGD',
    'british': 'GBR', 'uk': 'GBR', 'england': 'GBR',
    'american': 'USA', 'usa': 'USA', 'us': 'USA',
    'german': 'DEU', 'germany': 'DEU',
    'chinese': 'CHN', 'china': 'CHN',
    'russian': 'RUS', 'russia': 'RUS',
    'french': 'FRA', 'france': 'FRA',
    'italian': 'ITA', 'italy': 'ITA',
}


def normalize_nationality(text: str) -> str | None:
    """Deterministic nationality → code. Returns a code or None (never raises).
    Already-a-code (2–3 alpha) is upper-cased & passed through."""
    if not text:
        return None
    t = text.strip().lower()
    if not t:
        return None
    if t in _NAT_SYNONYMS:
        return _NAT_SYNONYMS[t]
    # bare ISO code the operator typed directly
    if re.fullmatch(r'[a-z]{2,3}', t):
        return t.upper()
    return None


# ── Dates ───────────────────────────────────────────────────────────────────

_MONTHS = {m: i for i, m in enumerate(
    ['jan', 'feb', 'mar', 'apr', 'may', 'jun', 'jul', 'aug', 'sep', 'oct',
     'nov', 'dec'], start=1)}
_WEEKDAYS = {d: i for i, d in enumerate(
    ['monday', 'tuesday', 'wednesday', 'thursday', 'friday', 'saturday',
     'sunday'])}


def _year_for(month: int, day: int, today: date) -> int:
    """Pick the year for a month/day with no year: this year, unless it's already
    passed (then next year), so '3 aug' typed in December means next August."""
    candidate = date(today.year, month, day)
    return today.year if candidate >= today else today.year + 1


def parse_date_strict(text: str, *, today: date | None = None) -> date | None:
    """Deterministic date parse — NO LLM. Handles:
      YYYY-MM-DD / YYYY/MM/DD, DD/MM/YYYY, DD/MM, '3 aug', 'aug 3', 'today',
      'tomorrow', 'day after tomorrow', 'next <weekday>', 'in N days'.
    Returns a date or None (never raises). DD/MM ordering is assumed (Maldives
    convention), matching the portal.
    """
    if not text:
        return None
    today = today or date.today()
    t = text.strip().lower()
    if not t:
        return None

    if t in ('today', 'tonight'):
        return today
    if t == 'tomorrow':
        return today + timedelta(days=1)
    if t in ('day after tomorrow', 'overmorrow'):
        return today + timedelta(days=2)

    m = re.fullmatch(r'in (\d{1,3}) days?', t)
    if m:
        return today + timedelta(days=int(m.group(1)))

    m = re.fullmatch(r'(?:next|this)\s+([a-z]+)', t)
    if m and m.group(1) in _WEEKDAYS:
        target = _WEEKDAYS[m.group(1)]
        delta = (target - today.weekday()) % 7
        delta = delta or 7                       # 'next' => at least a week out
        return today + timedelta(days=delta)

    # ISO / slashed with explicit year
    m = re.fullmatch(r'(\d{4})[-/](\d{1,2})[-/](\d{1,2})', t)
    if m:
        return _safe_date(int(m.group(1)), int(m.group(2)), int(m.group(3)))
    m = re.fullmatch(r'(\d{1,2})[-/](\d{1,2})[-/](\d{4})', t)   # DD/MM/YYYY
    if m:
        return _safe_date(int(m.group(3)), int(m.group(2)), int(m.group(1)))
    m = re.fullmatch(r'(\d{1,2})[-/](\d{1,2})', t)              # DD/MM (this/next yr)
    if m:
        day, mon = int(m.group(1)), int(m.group(2))
        if 1 <= mon <= 12 and 1 <= day <= 31:
            return _safe_date(_year_for(mon, day, today), mon, day)

    # '3 aug' / '3rd aug' / 'aug 3' / 'august 3'
    m = re.fullmatch(r'(\d{1,2})(?:st|nd|rd|th)?\s+([a-z]+)', t)
    if m:
        mon = _month_num(m.group(2))
        if mon:
            day = int(m.group(1))
            return _safe_date(_year_for(mon, day, today), mon, day)
    m = re.fullmatch(r'([a-z]+)\s+(\d{1,2})(?:st|nd|rd|th)?', t)
    if m:
        mon = _month_num(m.group(1))
        if mon:
            day = int(m.group(2))
            return _safe_date(_year_for(mon, day, today), mon, day)
    return None


def _month_num(word: str):
    return _MONTHS.get(word[:3]) if word[:3] in _MONTHS else None


def _safe_date(y, mo, d):
    try:
        return date(y, mo, d)
    except ValueError:
        return None


# ── Public parse entry points (deterministic only) ──────────────────────────

def parse_date(text: str, *, today: date | None = None) -> Parsed:
    """Parse a date phrase deterministically. Returns a Parsed; needs_confirm is
    True on any successful parse so the flow echoes the ISO date back. A phrase the
    strict parser can't resolve returns an unparsed Parsed and the flow re-asks —
    there is no LLM assist here (the ONE LLM path is pepper_bot.extract)."""
    d = parse_date_strict(text, today=today)
    if d is not None:
        return Parsed(d, 'strict', True)
    return Parsed(None, None, False)


def resolve_nationality(text: str) -> Parsed:
    """Nationality → code, deterministic map only. Always echoes back for
    confirmation. An unknown demonym returns unparsed and the flow re-asks."""
    code = normalize_nationality(text)
    if code:
        return Parsed(code, 'strict', True)
    return Parsed(None, None, False)
