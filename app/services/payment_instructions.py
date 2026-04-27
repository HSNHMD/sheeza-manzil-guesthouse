"""Sheeza Manzil bank-transfer payment instructions.

Single source of truth for the official payment block shown to guests
in AI-generated drafts. The values are HARD-CODED constants — never
read from environment variables, the database, or any user input.

Updates:
    Edit this file and ship via the standard Phase A/B/C deploy. The
    values live in source so they are reviewable in git history and
    cannot be changed by a runtime config edit.

Privacy:
    The full payment block contains the live account number and is
    therefore considered moderately sensitive. Audit logs MUST NOT
    persist the block text — only a `payment_instructions_used: True`
    boolean flag. Routes that include the block in an AI prompt MUST
    NOT pass the prompt or the resulting draft body to ActivityLog.
"""

from __future__ import annotations


# Bank-account constants. Treat the account number as the canonical
# source of truth — every prompt and template uses these directly.
ACCOUNT_NAME = 'SHEEZA IMAD/MOHAMED S.R.'
ACCOUNT_NUMBER = '7770000212622'

# The exact block to embed verbatim in payment-related AI drafts.
# Triple-quoted string preserves newlines exactly as the model will see them.
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
    """Return the official Sheeza Manzil payment instruction block.

    Returned as a single string with embedded newlines, ready to be
    pasted into an AI prompt or rendered as plain text. Idempotent and
    side-effect free — safe to call from anywhere.
    """
    return PAYMENT_INSTRUCTION_BLOCK
