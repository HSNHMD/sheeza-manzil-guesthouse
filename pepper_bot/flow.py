"""Guided /newbooking flow (spec §6) — a per-user state machine.

Design constraints (hard requirements, do not relax):

* PER-USER state, keyed by telegram_user_id — NOT per-chat. Two staff running
  interleaved flows in the New Booking topic must land two uncontaminated
  bookings. One active flow per user.

* PRIVACY-MODE-ON. Telegram delivers to the bot ONLY commands, replies to the
  bot's own messages, and @mentions. So every open-text step uses ForceReply
  (reply-tagging the initiating user) and answers are matched by
  reply_to_message.message_id == the flow's live prompt id; wherever the answer
  set is small we use PRESET INLINE BUTTONS instead (a silently-eaten free-text
  answer can't stall the flow). A missed/dropped reply RE-PROMPTS once, then
  cleanly cancels — never a silent stall.

* TWO ENTRY PATHS.
  - PRIMARY (build #19): SINGLE DICTATION. `/newbooking` posts one force-reply —
    "dictate the whole booking in one message" — and the one reply is sent to K3
    via the Hermes gateway (pepper_bot.extract) which returns a booking schema. The
    flow seeds the draft, then runs a CLARIFY-LOOP (one question at a time) for any
    unresolved field, then shows the summary. Every extracted value is ECHOED on the
    summary card for human confirmation (no LLM-derived value is trusted un-echoed).
  - FALLBACK: the EXISTING strict step-by-step flow, unchanged. Triggered when the
    extractor is off/unavailable/returns unusable output. The operator types each
    field (YYYY-MM-DD / an ISO code) exactly as shipped in Phase 2.
  User text is DATA — never used to act, never to decide validation. Dates +
  nationality still re-validate through the DETERMINISTIC pepper_bot.llm parser.

* VALIDATION IS THE API'S JOB. The bot does UX-only checks; enforcement is
  POST /bookings (create_group_booking). On an API 4xx/409 the bot surfaces the
  EXACT message and re-asks that field — it never silently fixes data.

* NO HOLD UNTIL CONFIRM. Step-6 availability is advisory; the booking is created
  (status='pending_verification') only on the summary-card ✅ Confirm. If
  availability vanished, create returns 409 → the bot re-runs availability.

* RESTART RECOVERY via the pepper_flows snapshot (through the internal API — the
  bot holds no DB creds). Timeouts: 30 min idle → ping once; 60 min → auto-cancel.

The flow object is deliberately telegram-thin (duck-typed bot/message/cq) so it
unit-tests with plain mocks, exactly like handlers.py.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re

from .llm import normalize_nationality, parse_date, resolve_nationality

log = logging.getLogger("pepper_bot")

# Idle timeouts (spec §6.4). Overridable for tests.
PING_SECONDS = 30 * 60
CANCEL_SECONDS = 60 * 60
_TIMEOUTS_ENABLED = True

# Ordered steps. Each: key, kind ('text'|'choice'), and prompt text.
# 'payment' (bank transfer vs cash) sits just before the summary so the summary
# card shows the chosen method and the confirm creates with the right
# payment_method (cash bookings get a manager-gated Cash-received alert instead
# of the slip flow — closes the un-verifiable-forever leak).
STEP_ORDER = [
    "name", "phone", "nationality", "id_number",
    "check_in", "check_out", "room", "count", "adults", "children",
    "payment", "summary",
]

_PROMPTS = {
    "name": "👤 Guest FIRST and LAST name? (e.g. `Ahmed Hassan`)",
    "phone": "📱 WhatsApp phone number?",
    "nationality": "🌍 Nationality? (e.g. `Maldivian`, `MV`, or an ISO code like `IND`)",
    "id_number": "🪪 ID / passport number?",
    "check_in": "📅 Check-IN date? (`YYYY-MM-DD`, `3 aug`, or `tomorrow`)",
    "check_out": "📅 Check-OUT date? (`YYYY-MM-DD`, `5 aug`, …)",
    "room": "🛏️ Pick a room type:",
    "count": "🔢 How many of that room?",
    "adults": "🧑 How many adults?",
    "children": "🧒 How many children?",
    "payment": "💳 How is the guest paying?",
}

_PAYMENT_LABELS = {"bank_transfer": "Bank transfer", "cash": "Cash"}

_GREEN_MDV = {"MDV"}


def _green_tax_line(code):
    if code in _GREEN_MDV:
        return "Maldivian — Green Tax exempt"
    return "Non-Maldivian — Green Tax applies"


class Flow:
    """One user's in-progress booking draft + cursor."""

    def __init__(self, telegram_id, chat_id, thread_id, name=""):
        self.telegram_id = telegram_id
        self.chat_id = chat_id
        self.thread_id = thread_id
        self.name = name or str(telegram_id)
        self.step = STEP_ORDER[0]
        self.draft: dict = {}          # accumulated answers (JSON-serialisable)
        self.prompt_id = None          # message_id of the live force-reply prompt
        self.reprompted = False
        self.pinged = False            # 30-min idle ping fired?
        self.task = None               # idle-timeout task
        self.editing = None            # field being edited via ✏️, else None
        self.rooms: list = []          # last /availability snapshot (advisory)
        # Single-dictation state (build #19). `dictating` is True while we await the
        # one dictated message; `clarify_queue` is the ordered list of fields the
        # extractor left unresolved that we still need to ask, one at a time;
        # `field_attempts` counts asks per field so a 2nd failure drops to the strict
        # single-field prompt instead of looping.
        self.dictating = False
        self.clarify_queue: list = []
        self.field_attempts: dict = {}

    # --- snapshot (restart recovery) ---
    def to_snapshot(self) -> dict:
        return {"chat_id": self.chat_id, "thread_id": self.thread_id,
                "step": self.step,
                "draft_json": json.dumps({"draft": self.draft, "name": self.name})}

    @classmethod
    def from_snapshot(cls, row) -> "Flow":
        payload = {}
        try:
            payload = json.loads(row.get("draft_json") or "{}")
        except (ValueError, TypeError):
            payload = {}
        f = cls(row["telegram_id"], row.get("chat_id"), row.get("thread_id"),
                name=payload.get("name", ""))
        f.step = row.get("step") or STEP_ORDER[0]
        f.draft = payload.get("draft", {}) or {}
        return f

    def guest_label(self) -> str:
        g = self.draft.get("guest", {})
        n = (g.get("first_name", "") + " " + g.get("last_name", "")).strip()
        return n or "this guest"


def _has_booking_signal(result) -> bool:
    """A booking-shaped extraction resolved at least one CONCRETE field — dates, a
    room/item, guest name, adults, or phone. An all-null 'parse' (e.g. 'thanks',
    'ok noted') is NOT booking-shaped, so the caller nudges instead of opening a
    clarify chain. This is what separates 'booking' from 'genuinely unparseable' when
    every non-Alerts line is fed to the extractor (feed-view catch-all)."""
    d = getattr(result, "data", None) or {}
    if d.get("check_in") or d.get("check_out") or d.get("adults") or d.get("items"):
        return True
    g = d.get("guest") or {}
    return bool(g.get("first_name") or g.get("last_name") or g.get("phone"))


# Cheap pre-gate before spending a K3 call. Bounds: not a one-word ack, not a long
# announcement — a briefing containing EXAMPLE bookings must NOT parse as a booking,
# so the length ceiling drops it before extraction (the Confirm card would gate it
# anyway, but this avoids the wasted K3 call + a spurious card).
_MIN_CANDIDATE_CHARS = 10
_MAX_CANDIDATE_CHARS = 400
_BOOKING_KEYWORDS = frozenset((
    "book", "booking", "reserve", "reservation", "room", "rooms", "night", "nights",
    "deluxe", "dlx", "twin", "suite", "single", "double", "adult", "adults", "pax",
    "guest", "guests", "child", "children", "kid", "kids", "checkin", "check", "cash",
    "transfer", "bank", "arriving", "arrival", "stay", "tonight", "tomorrow", "today",
    "jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "sept", "oct",
    "nov", "dec",
))


def _looks_like_booking_candidate(text: str) -> bool:
    """Cheap pre-gate: only messages of booking LENGTH carrying a booking SIGNAL —
    a digit (dates/phone/counts) or a booking keyword — are worth a K3 classification.
    A one-word ack ('ok', 'thanks') or a long announcement fails here → INSTANT nudge,
    no K3 call. Past the gate, K3 does the real booking-vs-not classification."""
    t = (text or "").strip()
    if not (_MIN_CANDIDATE_CHARS <= len(t) <= _MAX_CANDIDATE_CHARS):
        return False
    tl = t.lower()
    if any(ch.isdigit() for ch in tl):
        return True
    return bool(set(re.findall(r"[a-z']+", tl)) & _BOOKING_KEYWORDS)


class FlowManager:
    """Owns all active flows (per-user) + the persistence/timeout plumbing. The
    bot wires its /newbooking, /nb, /slip, force-reply and nb:* callback handlers
    to these methods."""

    def __init__(self, client, get_brand=None, extractor=None):
        self.client = client
        self.flows: dict = {}                 # telegram_id -> Flow
        # brand fetcher: bank block for the success message (never hardcoded).
        self._get_brand = get_brand or _default_brand
        # Optional single-dictation extractor (K3 via Hermes). When it's present AND
        # enabled we run the dictation path; otherwise every /newbooking runs the
        # strict step-by-step flow (LLM-off ⇒ strict, exactly as Phase 2 shipped).
        self.extractor = extractor

    def _dictation_on(self) -> bool:
        return bool(self.extractor is not None and getattr(self.extractor,
                                                          "enabled", False))

    # ── lifecycle ───────────────────────────────────────────────────────────
    async def start(self, bot, telegram_id, chat_id, thread_id, name):
        """/newbooking. If a flow is open, ask to abandon (yes/no)."""
        existing = self.flows.get(telegram_id)
        if existing is not None:
            await self._ask_abandon(bot, existing)
            return
        await self._begin(bot, telegram_id, chat_id, thread_id, name)

    async def _begin(self, bot, telegram_id, chat_id, thread_id, name):
        f = Flow(telegram_id, chat_id, thread_id, name=name)
        self.flows[telegram_id] = f
        if self._dictation_on():
            await self._prompt_dictation(bot, f)
        else:
            await self._prompt_current(bot, f)
        await self._persist(f)
        self._arm_idle(bot, f)

    # ── single-dictation primary path (build #19) ────────────────────────────
    async def _prompt_dictation(self, bot, f):
        """Post the ONE force-reply that asks the operator to dictate/type the whole
        booking in a single message."""
        from telegram import ForceReply
        f.dictating = True
        f.step = "dictate"
        msg = await bot.send_message(
            chat_id=f.chat_id, message_thread_id=f.thread_id,
            text=(f"@{f.name} 📝 Type the WHOLE booking in one message — "
                  "guest name, nationality, dates, room, guests, payment, phone/ID. "
                  "e.g. `Deluxe, John Smith British, 20-22 sep, 2 adults, transfer, "
                  "7712345`.\n(Or type anything and I'll ask for whatever's missing.)"),
            reply_markup=ForceReply(selective=True))
        f.prompt_id = getattr(msg, "message_id", None)

    async def _consume_dictation(self, bot, f, text):
        """Run the one dictation through the extractor, seed the draft, then either
        clarify the gaps (one question at a time) or go straight to summary. On an
        unusable/None result, announce and drop to the strict step-by-step flow."""
        result = None
        try:
            result = self.extractor.extract(text)
        except Exception:  # noqa: BLE001 — extractor is best-effort; fall back
            result = None
        f.dictating = False
        if result is None or not result.ok:
            await self._fallback_to_strict(bot, f)
            return
        self._seed_draft_from_extract(f, result)
        f.clarify_queue = self._clarify_order(result.unresolved)
        await self._persist(f)
        await self._advance_clarify(bot, f)

    def _clarify_order(self, unresolved) -> list:
        """The queue of fields to clarify, in flow order. ROOM is ALWAYS asked: a
        model-suggested room name is advisory only — the authoritative room pick is
        a human tap against LIVE availability (which also re-validates dates). Any
        extractor-suggested room name rides along as a hint in the prompt.
        `count` is folded into the room step (the room prompt is followed by count
        only when count is itself unresolved)."""
        order = [s for s in STEP_ORDER if s not in ("summary",)]
        want = set(unresolved)
        want.add("room")                       # never trust a model room name as id
        return [s for s in order if s in want]

    async def _fallback_to_strict(self, bot, f):
        """Dictation unavailable / unusable → announce, then run the EXISTING strict
        multi-step flow unchanged from the first step."""
        f.dictating = False
        f.clarify_queue = []
        f.step = STEP_ORDER[0]
        await self._say(bot, f, "🧭 Dictation unavailable — switching to "
                                "step-by-step. I'll ask one field at a time.")
        await self._prompt_current(bot, f)
        await self._persist(f)

    def _seed_draft_from_extract(self, f, result):
        """Map the extractor's cleaned schema into the flow draft (only present
        values; nulls are left for the clarify-loop). Room type is a NAME from the
        model — the room STEP re-resolves it against live availability, so we stash
        the hint and let the clarify-loop confirm the pick."""
        d = result.data
        g = d.get("guest", {})
        gd = f.draft.setdefault("guest", {})
        if g.get("first_name") and g.get("last_name"):
            gd["first_name"] = g["first_name"]
            gd["last_name"] = g["last_name"]
        for k in ("phone", "nationality", "id_number", "id_type"):
            if g.get(k):
                gd[k] = g[k]
        if d.get("check_in"):
            f.draft["check_in"] = d["check_in"]
        if d.get("check_out"):
            f.draft["check_out"] = d["check_out"]
        if d.get("adults") is not None:
            f.draft["adults"] = d["adults"]
        if d.get("children") is not None:
            f.draft["children"] = d["children"]
        if d.get("payment_method"):
            f.draft["payment_method"] = d["payment_method"]
        items = d.get("items") or []
        if items:
            f.draft["count"] = items[0].get("qty", 1)
            if items[0].get("room_type"):
                f.draft["_room_hint"] = items[0]["room_type"]

    async def _advance_clarify(self, bot, f):
        """Ask the NEXT unresolved field (one at a time). When the queue is empty
        every field is known → show the summary card for human confirmation."""
        if f.clarify_queue:
            field = f.clarify_queue[0]
            f.step = field
            f.editing = None
            await self._prompt_current(bot, f)
            await self._persist(f)
            return
        f.step = "summary"
        await self._prompt_summary(bot, f)
        await self._persist(f)

    async def _ask_abandon(self, bot, f):
        from telegram import InlineKeyboardButton, InlineKeyboardMarkup
        kb = InlineKeyboardMarkup([[
            InlineKeyboardButton("Yes, abandon", callback_data="nb:abandon:yes"),
            InlineKeyboardButton("No, keep it", callback_data="nb:abandon:no")]])
        await bot.send_message(
            chat_id=f.chat_id, message_thread_id=f.thread_id,
            text=(f"@{f.name}, you have a draft in progress for "
                  f"{f.guest_label()}. Abandon it and start over?"),
            reply_markup=kb)

    async def cancel(self, bot, f, notice=None):
        self.flows.pop(f.telegram_id, None)
        self._cancel_idle(f)
        await self._forget(f)
        if notice:
            await self._say(bot, f, notice)

    # ── prompting ───────────────────────────────────────────────────────────
    async def _prompt_current(self, bot, f, prefix=""):
        step = f.step
        if step == "room":
            await self._prompt_room(bot, f, prefix)
            return
        if step in ("count", "adults", "children"):
            await self._prompt_choice_numbers(bot, f, step, prefix)
            return
        if step == "payment":
            await self._prompt_payment(bot, f, prefix)
            return
        if step == "summary":
            await self._prompt_summary(bot, f)
            return
        # open-text step -> ForceReply, reply-tagging the user
        from telegram import ForceReply
        text = (prefix + _PROMPTS[step]).strip()
        msg = await bot.send_message(
            chat_id=f.chat_id, message_thread_id=f.thread_id,
            text=f"@{f.name} " + text,
            reply_markup=ForceReply(selective=True))
        f.prompt_id = getattr(msg, "message_id", None)

    async def _prompt_room(self, bot, f, prefix=""):
        from telegram import InlineKeyboardButton, InlineKeyboardMarkup
        ci, co = f.draft.get("check_in"), f.draft.get("check_out")
        rooms = await self.client.availability(ci, co, guests=1)
        f.rooms = rooms
        buttons, lines = [], []
        for r in rooms:
            rid = r.get("room_type_id")
            name = r.get("name", f"type {rid}")
            if r.get("sold_out") or (r.get("available_qty") or 0) <= 0:
                # show WHY it can't be booked (struck), don't hide it
                lines.append(f"~{name}~ (sold out)")
            else:
                lines.append(f"{name} — {r.get('available_qty')} left")
                buttons.append([InlineKeyboardButton(
                    name, callback_data=f"nb:room:{rid}")])
        head = (prefix + _PROMPTS["room"]).strip()
        hint = f.draft.get("_room_hint")
        if hint:
            head = f"{head} (you said “{hint}” — confirm the exact type)"
        body = head + "\n" + "\n".join(lines) if lines else \
            head + "\n(no availability for these dates)"
        kb = InlineKeyboardMarkup(buttons) if buttons else None
        msg = await bot.send_message(chat_id=f.chat_id,
                                     message_thread_id=f.thread_id,
                                     text=f"@{f.name} " + body, reply_markup=kb)
        f.prompt_id = getattr(msg, "message_id", None)

    async def _prompt_choice_numbers(self, bot, f, step, prefix=""):
        from telegram import InlineKeyboardButton, InlineKeyboardMarkup
        lo = 0 if step == "children" else 1
        nums = list(range(lo, lo + 7))
        row = [InlineKeyboardButton(str(n), callback_data=f"nb:{step}:{n}")
               for n in nums]
        kb = InlineKeyboardMarkup([row[:4], row[4:]])
        text = (prefix + _PROMPTS[step]).strip()
        msg = await bot.send_message(chat_id=f.chat_id,
                                     message_thread_id=f.thread_id,
                                     text=f"@{f.name} " + text, reply_markup=kb)
        f.prompt_id = getattr(msg, "message_id", None)

    async def _prompt_payment(self, bot, f, prefix=""):
        from telegram import InlineKeyboardButton, InlineKeyboardMarkup
        kb = InlineKeyboardMarkup([[
            InlineKeyboardButton("🏦 Bank transfer",
                                 callback_data="nb:pay:bank_transfer"),
            InlineKeyboardButton("💵 Cash", callback_data="nb:pay:cash")]])
        text = (prefix + _PROMPTS["payment"]).strip()
        msg = await bot.send_message(chat_id=f.chat_id,
                                     message_thread_id=f.thread_id,
                                     text=f"@{f.name} " + text, reply_markup=kb)
        f.prompt_id = getattr(msg, "message_id", None)

    async def _prompt_summary(self, bot, f):
        from telegram import InlineKeyboardButton, InlineKeyboardMarkup
        card = await self._summary_text(f)
        kb = InlineKeyboardMarkup([[
            InlineKeyboardButton("✅ Confirm", callback_data="nb:confirm"),
            InlineKeyboardButton("✏️ Edit field", callback_data="nb:edit"),
            InlineKeyboardButton("❌ Cancel", callback_data="nb:cancel")]])
        msg = await bot.send_message(chat_id=f.chat_id,
                                     message_thread_id=f.thread_id,
                                     text=card, reply_markup=kb)
        f.prompt_id = getattr(msg, "message_id", None)

    async def _summary_text(self, f) -> str:
        d = f.draft
        g = d.get("guest", {})
        quote = await self.client.quote(
            d["check_in"], d["check_out"],
            [{"room_type_id": d["room_type_id"], "qty": d["count"]}],
            guests=(d.get("adults", 1) + d.get("children", 0)))
        total = (quote or {}).get("total", 0)
        f.draft["_quote_total"] = total
        nat = g.get("nationality", "—")
        method = d.get("payment_method", "bank_transfer")
        return "\n".join([
            "🧾 *Booking summary* — please review",
            f"Guest: {g.get('first_name','')} {g.get('last_name','')}".strip(),
            f"Phone: {g.get('phone','—')}",
            f"Nationality: {nat} ({_green_tax_line(nat)})",
            f"ID/Passport: {g.get('id_number','—')}",
            f"Stay: {d['check_in']} → {d['check_out']}",
            f"Room: {d.get('room_name','type '+str(d.get('room_type_id')))} × {d['count']}",
            f"Guests: {d.get('adults',1)} adult(s), {d.get('children',0)} child(ren)",
            f"Payment: {_PAYMENT_LABELS.get(method, method)}",
            f"Total: MVR {total:.0f}",
            "",
            "On ✅ Confirm the booking is created *pending verification* "
            "(not yet confirmed) — a bank transfer is confirmed when the slip is "
            "verified; cash is confirmed when a manager taps 💵 Cash received.",
        ])

    # ── inbound: force-reply text ────────────────────────────────────────────
    async def handle_reply(self, bot, message, telegram_id) -> bool:
        """A reply to the bot's live force-reply prompt. Returns True if it was
        consumed by a flow (so the caller doesn't also treat it as a reject
        reason). Matched by BOTH user id AND reply-to-prompt-id — the anti-
        contamination guarantee."""
        f = self.flows.get(telegram_id)
        if f is None:
            return False
        reply_to = getattr(message, "reply_to_message", None)
        rid = getattr(reply_to, "message_id", None)
        if f.prompt_id is None or rid != f.prompt_id:
            return False               # not a reply to THIS flow's live prompt
        text = (getattr(message, "text", "") or "").strip()
        self._touch_idle(bot, f)
        if f.dictating:
            await self._consume_dictation(bot, f, text)
        else:
            await self._consume_text(bot, f, text)
        return True

    async def handle_group_text(self, bot, message, telegram_id) -> bool:
        """Feed-view catch-all routing (widened): an ACTIVE flow is continued (from
        anywhere in the group); a plain line with no open flow is CLASSIFIED by the
        extractor and, if booking-shaped, STARTS a booking. Returns False for
        genuinely unparseable text so the caller nudges — a stray line never starts a
        flow. Anti-contamination rides on one-draft-per-user (#24)."""
        text = (getattr(message, "text", "") or "").strip()
        f = self.flows.get(telegram_id)
        if f is not None:
            self._touch_idle(bot, f)
            if f.dictating:
                await self._consume_dictation(bot, f, text)
            else:
                await self._consume_text(bot, f, text)
            return True
        if text:
            return await self._begin_from_text(bot, telegram_id, message, text)
        return False

    async def _begin_from_text(self, bot, telegram_id, message, text) -> bool:
        """Try to start a booking from a plain group line. CLASSIFY FIRST — extract
        BEFORE creating any flow. If the text yields a real booking signal, open the
        flow and go to the summary/clarify card; if it's unparseable (or dictation is
        off, so we can't classify), return False so the caller nudges — a stray line
        never leaves a half-started flow. Creation still happens ONLY on ✅ Confirm."""
        if not self._dictation_on():
            return False                     # can't classify without the LLM; /newbooking still works
        if not _looks_like_booking_candidate(text):
            return False                     # cheap pre-gate: too short/long / no signal → instant nudge, no K3
        try:
            result = self.extractor.extract(text)
        except Exception:  # noqa: BLE001 — extractor is best-effort
            result = None
        if result is None or not _has_booking_signal(result):
            return False                     # genuinely unparseable → caller nudges
        chat_id = getattr(message, "chat_id", None)
        thread_id = getattr(message, "message_thread_id", None)
        u = getattr(message, "from_user", None)
        name = (getattr(u, "full_name", None) or getattr(u, "first_name", None)
                or str(telegram_id))
        f = Flow(telegram_id, chat_id, thread_id, name=name)
        self.flows[telegram_id] = f
        self._arm_idle(bot, f)
        self._seed_draft_from_extract(f, result)
        f.clarify_queue = self._clarify_order(result.unresolved)
        await self._persist(f)
        await self._advance_clarify(bot, f)
        return True

    async def _consume_text(self, bot, f, text):
        step = f.editing or f.step
        handler = getattr(self, f"_set_{step}", None)
        if handler is None:
            # #22/#25 — NEVER go silent. At the summary card the only valid actions
            # are the inline buttons; re-point the operator there instead of dropping
            # the message. Any other parserless state gets a generic re-prompt.
            if step == "summary":
                await self._say(bot, f, "👉 Please tap ✅ Confirm, ✏️ Edit, or ❌ "
                                        "Cancel on the card above — or /cancel to "
                                        "start over.")
            else:
                await self._say(bot, f, "🤔 I didn't catch that — use the buttons "
                                        "above, or /cancel to start over.")
            return
        ok, echo = await handler(f, text)
        if not ok:
            # Clarify-loop 2-strikes rule (build #19): a 2nd failure on the SAME
            # field drops to a strict single-field prompt (an explicit, format-only
            # ask) instead of re-asking the fuzzy prompt forever.
            f.field_attempts[step] = f.field_attempts.get(step, 0) + 1
            if f.field_attempts[step] >= 2:
                await self._prompt_strict_field(bot, f, step, reason=echo)
            else:
                await self._prompt_current(bot, f, prefix=f"⚠️ {echo}\n")
            return
        f.field_attempts.pop(step, None)
        if echo:
            await self._say(bot, f, echo)     # echo parsed value for confirmation
        await self._advance(bot, f)

    # Strict single-field prompts used after a 2nd failure on a clarify field —
    # unambiguous, format-only asks (never fuzzy).
    _STRICT_FIELD_PROMPTS = {
        "name": "Type the guest's FIRST and LAST name exactly, space-separated: "
                "`Firstname Lastname`.",
        "phone": "Type the phone number using digits only, e.g. `9607712345`.",
        "nationality": "Type an ISO country code: `MDV` for Maldivian, `IND`, "
                       "`GBR`, `USA`, …",
        "id_number": "Type the ID / passport number exactly.",
        "check_in": "Type the check-IN date as `YYYY-MM-DD`, e.g. `2026-09-20`.",
        "check_out": "Type the check-OUT date as `YYYY-MM-DD`, e.g. `2026-09-22`.",
    }

    async def _prompt_strict_field(self, bot, f, step, reason=""):
        """After 2 failures, re-ask a text field with an explicit format-only prompt
        (choice fields already re-render their buttons, so those never reach here)."""
        from telegram import ForceReply
        strict = self._STRICT_FIELD_PROMPTS.get(step, _PROMPTS.get(step, ""))
        head = (f"⚠️ {reason}\n" if reason else "")
        msg = await bot.send_message(
            chat_id=f.chat_id, message_thread_id=f.thread_id,
            text=f"@{f.name} {head}{strict}".strip(),
            reply_markup=ForceReply(selective=True))
        f.prompt_id = getattr(msg, "message_id", None)

    # field setters: return (ok, echo_or_reason)
    async def _set_name(self, f, text):
        parts = text.split()
        if len(parts) < 2:
            return False, "Please give BOTH first and last name."
        g = f.draft.setdefault("guest", {})
        g["first_name"] = parts[0]
        g["last_name"] = " ".join(parts[1:])
        return True, None

    async def _set_phone(self, f, text):
        if not any(c.isdigit() for c in text):
            return False, "That doesn't look like a phone number."
        f.draft.setdefault("guest", {})["phone"] = text
        return True, None

    async def _set_nationality(self, f, text):
        p = resolve_nationality(text)
        if not p.ok:
            return False, ("Couldn't read that nationality — type an ISO code "
                           "like `MDV` or `IND`.")
        f.draft.setdefault("guest", {})["nationality"] = p.value
        return True, f"Nationality: {p.value} — {_green_tax_line(p.value)}."

    async def _set_id_number(self, f, text):
        if not text:
            return False, "Please give the ID / passport number."
        f.draft.setdefault("guest", {})["id_number"] = text
        return True, None

    async def _set_check_in(self, f, text):
        p = parse_date(text)
        if not p.ok:
            return False, "Couldn't read that date — try `YYYY-MM-DD`."
        f.draft["check_in"] = p.value.isoformat()
        return True, f"Check-in: {p.value.isoformat()} — is that right?"

    async def _set_check_out(self, f, text):
        p = parse_date(text)
        if not p.ok:
            return False, "Couldn't read that date — try `YYYY-MM-DD`."
        if "check_in" in f.draft and p.value.isoformat() <= f.draft["check_in"]:
            return False, "Check-out must be AFTER check-in."
        f.draft["check_out"] = p.value.isoformat()
        return True, f"Check-out: {p.value.isoformat()} — is that right?"

    # ── inbound: nb:* callbacks ──────────────────────────────────────────────
    async def handle_callback(self, bot, cq, telegram_id) -> bool:
        """Handle a nb:* inline tap. Returns True if consumed."""
        data = (getattr(cq, "data", "") or "")
        if not data.startswith("nb:"):
            return False
        parts = data.split(":")
        sub = parts[1] if len(parts) > 1 else ""
        f = self.flows.get(telegram_id)

        if sub == "abandon":
            await self._answer(cq)
            if f is None:
                return True
            if parts[2] == "yes":
                await self.cancel(bot, f)
                await self._begin(bot, f.telegram_id, f.chat_id, f.thread_id, f.name)
            return True

        if f is None:
            await self._answer(cq, "That draft is no longer active.")
            return True
        self._touch_idle(bot, f)

        if sub == "room":
            rid = int(parts[2])
            r = next((x for x in f.rooms if x.get("room_type_id") == rid), None)
            f.draft["room_type_id"] = rid
            f.draft["room_name"] = (r or {}).get("name", f"type {rid}")
            await self._answer(cq, "Room selected.")
            await self._advance(bot, f)
            return True

        if sub in ("count", "adults", "children"):
            f.draft[sub] = int(parts[2])
            await self._answer(cq)
            await self._advance(bot, f)
            return True

        if sub == "pay":
            method = parts[2]
            if method not in ("bank_transfer", "cash"):
                await self._answer(cq); return True
            f.draft["payment_method"] = method
            await self._answer(cq, f"{_PAYMENT_LABELS[method]} selected.")
            await self._advance(bot, f)
            return True

        if sub == "confirm":
            # Confirm only acts from the summary card — a stale/duplicate tap on
            # an earlier step is ignored (guards against creating a booking before
            # the payment method is chosen).
            if f.step != "summary":
                await self._answer(cq)
                return True
            await self._answer(cq)
            await self._do_confirm(bot, f, cq)
            return True

        if sub == "cancel":
            await self._answer(cq, "Cancelled.")
            await self.cancel(bot, f, notice="❌ Booking draft cancelled.")
            return True

        if sub == "edit":
            await self._answer(cq)
            await self._offer_edit_menu(bot, f)
            return True

        if sub == "editf":       # a specific field chosen from the edit menu
            field = parts[2]
            f.editing = field
            await self._answer(cq, f"Editing {field}.")
            await self._prompt_current_for_field(bot, f, field)
            return True

        await self._answer(cq)
        return True

    async def _offer_edit_menu(self, bot, f):
        from telegram import InlineKeyboardButton, InlineKeyboardMarkup
        fields = ["name", "phone", "nationality", "id_number",
                  "check_in", "check_out", "room", "count", "adults", "children",
                  "payment"]
        rows, row = [], []
        for fld in fields:
            row.append(InlineKeyboardButton(fld, callback_data=f"nb:editf:{fld}"))
            if len(row) == 3:
                rows.append(row); row = []
        if row:
            rows.append(row)
        await bot.send_message(chat_id=f.chat_id, message_thread_id=f.thread_id,
                               text=f"@{f.name} which field to edit?",
                               reply_markup=InlineKeyboardMarkup(rows))

    async def _prompt_current_for_field(self, bot, f, field):
        """Re-prompt a single field for editing (choice fields re-show buttons)."""
        saved = f.step
        f.step = field
        await self._prompt_current(bot, f)
        f.step = saved

    # ── advance / edit return ────────────────────────────────────────────────
    async def _advance(self, bot, f):
        if f.editing:
            # after editing one field, jump straight back to the summary
            f.editing = None
            f.step = "summary"
            await self._prompt_current(bot, f)
            await self._persist(f)
            return
        # CLARIFY-LOOP (build #19): if we're filling extractor gaps, the just-set
        # field is the head of the queue — pop it and ask the NEXT gap (one at a
        # time). Empty queue ⇒ summary. This bypasses the linear STEP_ORDER walk.
        if f.clarify_queue:
            if f.clarify_queue[0] == f.step:
                f.clarify_queue.pop(0)
            await self._advance_clarify(bot, f)
            return
        idx = STEP_ORDER.index(f.step) if f.step in STEP_ORDER else -1
        f.step = STEP_ORDER[min(idx + 1, len(STEP_ORDER) - 1)]
        await self._prompt_current(bot, f)
        await self._persist(f)

    # ── confirm -> create (pending_verification) ─────────────────────────────
    async def _do_confirm(self, bot, f, cq=None):
        d = f.draft
        g = d.get("guest", {})
        body = {
            "items": [{"room_type_id": d["room_type_id"], "qty": d["count"]}],
            "check_in": d["check_in"], "check_out": d["check_out"],
            "adults": d.get("adults", 1), "children": d.get("children", 0),
            "guest": g, "status": "pending_verification",
            "payment_method": d.get("payment_method", "bank_transfer"),
        }
        status, resp = await self.client.create_booking(body)
        if status == 201 and resp.get("ok"):
            bid = resp["booking_ids"][0]
            brand = await self._resolve_brand()
            await self._say(bot, f, self._success_message(bid, d, brand))
            await self.cancel(bot, f)   # flow done; snapshot cleared
            return
        # 400 = a field the bot must re-ask (surface EXACT message, no silent fix)
        if status == 400:
            msg = resp.get("error", "validation failed")
            await self._reask_from_error(bot, f, msg)
            return
        # 409 = availability vanished (or capacity) — re-run availability
        reasons = "; ".join(resp.get("reasons", ["could not create"]))
        await self._say(bot, f,
                        f"⚠️ Couldn't confirm: {reasons}\nRe-checking availability…")
        f.step = "room"
        await self._prompt_current(bot, f)
        await self._persist(f)

    async def _reask_from_error(self, bot, f, message):
        """Map an API validation message to the field to re-ask (never silently
        fix). Falls back to re-showing the summary if we can't localise it."""
        low = message.lower()
        field = None
        if "nationality" in low:
            field = "nationality"
        elif "adult" in low:
            field = "adults"
        if field:
            f.editing = field
            await self._say(bot, f, f"⚠️ {message}")
            await self._prompt_current_for_field(bot, f, field)
        else:
            await self._say(bot, f, f"⚠️ {message}")
            await self._prompt_summary(bot, f)

    async def _resolve_brand(self):
        """Fetch the brand/bank block; support a sync OR async fetcher (the real
        client method is async; test fakes pass a sync callable)."""
        try:
            res = self._get_brand()
            if asyncio.iscoroutine(res):
                res = await res
            return res or {}
        except Exception:  # noqa: BLE001
            return {}

    def _success_message(self, booking_id, d, brand=None) -> str:
        brand = brand or {}
        total = d.get('_quote_total', 0)
        head = [f"✅ Booking #{booking_id} created — *pending verification*.",
                f"Total: MVR {total:.0f}", ""]
        if d.get("payment_method") == "cash":
            # Cash: NO bank block, NO slip. A manager taps 💵 Cash received on the
            # Alerts post to confirm — that's the cash booking's confirm path.
            return "\n".join(head + [
                f"Collect MVR {total:.0f} in cash from the guest.",
                "A manager taps 💵 Cash received on the Alerts post to confirm "
                "this booking.",
            ])
        # Bank transfer: the get_brand() bank block + slip instructions.
        bank = "\n".join([
            f"Bank: {brand.get('bank_name','—')}",
            f"Account name: {brand.get('bank_account_name','—')}",
            f"Account number: {brand.get('bank_account_number','—')}",
        ])
        return "\n".join(head + [
            "Forward the guest the payment details:",
            bank,
            "",
            f"When the slip arrives, reply to this message with the photo, "
            f"or send `/slip {booking_id}` with the photo attached.",
        ])

    # ── slip attach (step 10) ────────────────────────────────────────────────
    async def attach_slip(self, bot, message, booking_id, photo_bytes, filename):
        """Route a slip photo (reply or /slip <id>) to POST /bookings/<id>/slip."""
        status, resp = await self.client.booking_slip(booking_id, photo_bytes,
                                                      filename)
        if status == 200 and resp.get("ok"):
            await message.reply_text(
                f"🧾 Slip attached to booking #{booking_id}. "
                "It's now in the Alerts topic for verification.")
        else:
            await message.reply_text(
                "Couldn't attach the slip: "
                + str(resp.get("error", "please retry")))

    # ── persistence (restart recovery) ───────────────────────────────────────
    async def _persist(self, f):
        try:
            await self.client.flow_put(f.telegram_id, f.to_snapshot())
        except Exception:  # noqa: BLE001 — snapshot is best-effort; never break UX
            log.warning("flow snapshot persist failed for %s", f.telegram_id)

    async def _forget(self, f):
        try:
            await self.client.flow_delete(f.telegram_id)
        except Exception:  # noqa: BLE001
            pass

    async def resume_all(self, bot):
        """On startup: reload open snapshots and resume each with a step-aware
        message so nothing is silently lost across a restart."""
        try:
            rows = await self.client.flow_list()
        except Exception:  # noqa: BLE001
            rows = []
        for row in rows:
            f = Flow.from_snapshot(row)
            self.flows[f.telegram_id] = f
            step_no = STEP_ORDER.index(f.step) + 1 if f.step in STEP_ORDER else 1
            await self._say(bot, f,
                            f"🔄 I restarted; @{f.name}, we were at step {step_no}.")
            if f.step == "dictate":
                # Persisted mid-dictation (build #19): 'dictate' is not a strict-step
                # key, so _prompt_current would KeyError — re-post the dictation
                # prompt to resume that state cleanly.
                await self._prompt_dictation(bot, f)
            else:
                await self._prompt_current(bot, f)
            self._arm_idle(bot, f)

    # ── idle timeout (ping @30m, auto-cancel @60m) ───────────────────────────
    def _arm_idle(self, bot, f):
        if not _TIMEOUTS_ENABLED:
            return
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            f.task = None
            return
        f.task = loop.create_task(self._idle_timer(bot, f))

    def _cancel_idle(self, f):
        if f.task is not None and not f.task.done():
            f.task.cancel()
        f.task = None

    def _touch_idle(self, bot, f):
        """Any activity resets the idle clock."""
        self._cancel_idle(f)
        f.pinged = False
        self._arm_idle(bot, f)

    async def _idle_timer(self, bot, f):
        try:
            await asyncio.sleep(PING_SECONDS)
            if self.flows.get(f.telegram_id) is not f:
                return
            f.pinged = True
            await self._say(bot, f, f"⏳ @{f.name}, still there? This draft for "
                                    f"{f.guest_label()} auto-cancels in 30 min.")
            await asyncio.sleep(CANCEL_SECONDS - PING_SECONDS)
            if self.flows.get(f.telegram_id) is not f:
                return
            await self.cancel(bot, f, notice=(
                "⌛ Booking draft auto-cancelled after 60 min idle "
                "(no booking created, no hold placed)."))
        except asyncio.CancelledError:
            return

    # ── small telegram helpers ───────────────────────────────────────────────
    async def _say(self, bot, f, text):
        try:
            await bot.send_message(chat_id=f.chat_id,
                                   message_thread_id=f.thread_id, text=text)
        except Exception:  # noqa: BLE001
            pass

    @staticmethod
    async def _answer(cq, text=None):
        try:
            if text:
                await cq.answer(text)
            else:
                await cq.answer()
        except Exception:  # noqa: BLE001
            pass


def _default_brand():
    """Fallback brand fetcher used when the manager isn't given one — the bot
    wires the real internal-API-backed fetcher. Empty block is honest (the
    primary confirms prod property_settings is populated)."""
    return {}
