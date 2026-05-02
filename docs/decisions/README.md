# Architectural decisions (ADRs)

> A short journal of major architectural choices. The "why" lives
> here; the "what" lives in code.

## Format

We use lightweight ADRs. Each file in this folder:

- Filename: `NNNN-short-kebab-title.md` (4-digit zero-padded sequence).
- One decision per file. Don't bundle.
- Sections (in order):
  1. **Status** — Proposed / Accepted / Superseded by NNNN /
     Deprecated.
  2. **Context** — what was true when the decision was made; what
     forces drove it.
  3. **Decision** — what was chosen, in plain language.
  4. **Consequences** — what becomes easier; what becomes harder;
     what's now off the table.
  5. **Alternatives considered** (optional but valuable) — what was
     rejected and why.

Keep each ADR under one screen of text. If it's longer, you're
hiding the decision in detail.

## When to write an ADR

Write one when:
- A choice constrains future code in a way that isn't obvious from
  reading the code.
- A choice was made by elimination (we said "no" to several things
  before saying "yes" to one).
- A non-trivial sprint introduces a new pattern that future sprints
  will follow.

Don't write one for:
- Routine library upgrades.
- Single-file refactors.
- "We did X because the framework forces it."

## When to supersede an ADR

If a later decision overrides an earlier one:
1. Set the earlier ADR's status to **`Superseded by NNNN`**.
2. Add a one-line link at the top of the earlier ADR pointing to the
   new one.
3. Don't delete the old ADR — the audit trail matters more than
   tidiness.

## Numbering

| Range | Reserved for |
|---|---|
| 0001-0099 | Foundation decisions (current) |
| 0100-0199 | Channel manager / OTA decisions |
| 0200-0299 | Multi-property decisions |
| 0300+ | Free for future categories |

## Current ADRs

- [0001 — Reservation Board as front-office spine](0001-reservation-board-as-front-office-spine.md)
- [0002 — Folio as money spine](0002-folio-as-money-spine.md)
- [0003 — Business date separate from server time](0003-business-date-separate-from-server-time.md)
- [0004 — Staging-first workflow](0004-staging-first-workflow.md)
- [0005 — Stay segments for mid-stay room change](0005-stay-segments-for-mid-stay-room-change.md)
- [0006 — Property-aware schema before multi-property UI](0006-property-aware-before-multi-property.md)
