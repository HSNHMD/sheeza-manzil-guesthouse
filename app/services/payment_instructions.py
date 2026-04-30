"""Bank-transfer payment instructions for AI drafts + confirmation pages.

Property Settings Foundation V1 (migration 0c5e7f3b842a) made the
underlying values editable via the `property_settings` singleton row.
This module is now a thin wrapper that reads from that row at call
time, with the original hard-coded constants kept as a last-resort
fallback so existing prompts keep rendering even mid-migration.

Privacy:
    The block contains a live bank-account number and is treated as
    moderately sensitive. Audit logs MUST NOT persist the block text —
    only a `payment_instructions_used: True` boolean flag. Routes that
    include the block in an AI prompt MUST NOT pass the prompt or the
    resulting draft body to ActivityLog.
"""

from __future__ import annotations


# Hard-coded fallbacks — only used when the DB lookup fails (test DB
# before migrations, transient error). Production / staging always
# read from the DB-backed settings via property_settings service.
ACCOUNT_NAME = 'SHEEZA IMAD/MOHAMED S.R.'
ACCOUNT_NUMBER = '7770000212622'

PAYMENT_INSTRUCTION_BLOCK = (
    'Bank Transfer Details\n'
    '\n'
    f'Account Name: {ACCOUNT_NAME}\n'
    f'Account Number: {ACCOUNT_NUMBER}\n'
    '\n'
    'Please send the payment slip after transfer so we can verify your '
    'booking.'
)


def get_payment_instruction_block() -> str:
    """Return the canonical payment instruction block.

    Reads from `property_settings.payment_instructions_text` (or the
    bank fields, composed) when available. Falls back to the constants
    above if the DB is unavailable. Idempotent and side-effect free —
    safe to call from anywhere.
    """
    try:
        from .property_settings import (
            get_payment_instruction_block as _db_block,
        )
        return _db_block()
    except Exception:
        return PAYMENT_INSTRUCTION_BLOCK
