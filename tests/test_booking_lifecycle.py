"""Unit tests for app/booking_lifecycle.py.

Run any of:
    python -m unittest tests.test_booking_lifecycle
    venv/bin/python -m unittest tests.test_booking_lifecycle
    venv/bin/python tests/test_booking_lifecycle.py        (direct invocation)

These tests are deliberately lightweight — pure-function coverage with no
DB or HTTP dependencies. They exist so a future regression to the lifecycle
vocabulary, valid-pair set, legacy normalization, or display helpers is
caught immediately.
"""

import os
import sys
import unittest

# Make the project root importable when running this file directly
_PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _PROJECT_ROOT not in sys.path:
    sys.path.insert(0, _PROJECT_ROOT)

from app.booking_lifecycle import (  # noqa: E402
    BOOKING_STATUSES,
    PAYMENT_STATUSES,
    VALID_STATUS_PAIRS,
    CONFIRMABLE_FROM,
    can_confirm,
    is_valid_booking_status,
    is_valid_payment_status,
    is_valid_status_pair,
    normalize_legacy_payment_status,
    get_status_label,
    get_status_badge_class,
)


class _FakeInvoice:
    """Minimal stub mimicking the invoice attributes the helpers read."""

    def __init__(self, balance_due):
        self.balance_due = balance_due


class TestVocabularies(unittest.TestCase):
    """The canonical tuples must contain exactly the 9 + 5 expected values."""

    def test_booking_statuses_is_exactly_the_9_canonical_values(self):
        expected = {
            'new_request', 'pending_payment', 'payment_uploaded',
            'payment_verified', 'confirmed', 'checked_in',
            'checked_out', 'cancelled', 'rejected',
        }
        self.assertEqual(set(BOOKING_STATUSES), expected)
        self.assertEqual(len(BOOKING_STATUSES), 9)

    def test_payment_statuses_is_exactly_the_5_canonical_values(self):
        expected = {
            'not_received', 'pending_review', 'verified',
            'rejected', 'mismatch',
        }
        self.assertEqual(set(PAYMENT_STATUSES), expected)
        self.assertEqual(len(PAYMENT_STATUSES), 5)

    def test_partial_is_NOT_a_canonical_payment_status(self):
        """Design rule: partial must never be a stored payment_status."""
        self.assertNotIn('partial', PAYMENT_STATUSES)
        self.assertNotIn('paid', PAYMENT_STATUSES)
        self.assertNotIn('unpaid', PAYMENT_STATUSES)


class TestValidStatusPairs(unittest.TestCase):
    """The valid-pair frozenset must reject impossible combinations."""

    def test_every_booking_status_appears_in_at_least_one_pair(self):
        booking_in_pairs = {b for b, _ in VALID_STATUS_PAIRS}
        self.assertEqual(booking_in_pairs, set(BOOKING_STATUSES))

    def test_every_payment_status_appears_in_at_least_one_pair(self):
        payment_in_pairs = {p for _, p in VALID_STATUS_PAIRS}
        self.assertEqual(payment_in_pairs, set(PAYMENT_STATUSES))

    def test_explicitly_invalid_pairs_are_rejected(self):
        """User-specified invalid combinations must return False."""
        invalid = [
            ('confirmed',  'not_received'),
            ('checked_in', 'not_received'),
            ('checked_out','not_received'),
            ('payment_verified', 'pending_review'),
            ('new_request', 'verified'),
            ('confirmed', 'pending_review'),
        ]
        for pair in invalid:
            with self.subTest(pair=pair):
                self.assertFalse(is_valid_status_pair(*pair),
                                 f'{pair} must be invalid but is_valid_status_pair returned True')

    def test_explicitly_valid_pairs_accepted(self):
        valid = [
            ('payment_uploaded', 'pending_review'),
            ('payment_uploaded', 'mismatch'),
            ('payment_verified', 'verified'),
            ('confirmed', 'verified'),
            ('checked_in', 'verified'),
            ('checked_out', 'verified'),
            ('cancelled', 'verified'),
            ('cancelled', 'not_received'),
            ('rejected', 'rejected'),
            ('rejected', 'mismatch'),
            ('new_request', 'not_received'),
            ('pending_payment', 'not_received'),
        ]
        for pair in valid:
            with self.subTest(pair=pair):
                self.assertTrue(is_valid_status_pair(*pair),
                                f'{pair} must be valid but is_valid_status_pair returned False')

    def test_garbage_pair_is_invalid(self):
        self.assertFalse(is_valid_status_pair('garbage', 'verified'))
        self.assertFalse(is_valid_status_pair('confirmed', 'garbage'))
        self.assertFalse(is_valid_status_pair(None, None))


class TestSingleStatusValidators(unittest.TestCase):
    """is_valid_booking_status() and is_valid_payment_status()."""

    def test_canonical_values_are_valid(self):
        for s in BOOKING_STATUSES:
            self.assertTrue(is_valid_booking_status(s))
        for s in PAYMENT_STATUSES:
            self.assertTrue(is_valid_payment_status(s))

    def test_legacy_values_are_invalid_at_canonical_check(self):
        """is_valid_payment_status('paid') should be False — partial/paid/unpaid
        are LEGACY values, not canonical values, even if normalize_legacy_*
        will accept them."""
        for legacy in ('unpaid', 'partial', 'paid'):
            self.assertFalse(is_valid_payment_status(legacy))
        for legacy in ('unconfirmed', 'pending_verification'):
            self.assertFalse(is_valid_booking_status(legacy))

    def test_garbage_returns_false(self):
        self.assertFalse(is_valid_booking_status('garbage'))
        self.assertFalse(is_valid_payment_status('garbage'))
        self.assertFalse(is_valid_booking_status(None))
        self.assertFalse(is_valid_payment_status(None))


class TestLegacyNormalization(unittest.TestCase):
    """normalize_legacy_payment_status: old→new mapping with safe fallback."""

    def test_unpaid_maps_to_not_received(self):
        self.assertEqual(normalize_legacy_payment_status('unpaid'), 'not_received')

    def test_paid_maps_to_verified(self):
        self.assertEqual(normalize_legacy_payment_status('paid'), 'verified')

    def test_partial_maps_to_verified(self):
        """Partial is intentionally collapsed to 'verified'; 'partial' as a
        display state is derived from balance_due > 0, not stored separately."""
        self.assertEqual(normalize_legacy_payment_status('partial'), 'verified')

    def test_canonical_values_pass_through_unchanged(self):
        for s in PAYMENT_STATUSES:
            self.assertEqual(normalize_legacy_payment_status(s), s)

    def test_unknown_returns_None_not_crash(self):
        self.assertIsNone(normalize_legacy_payment_status('garbage'))
        self.assertIsNone(normalize_legacy_payment_status(''))
        self.assertIsNone(normalize_legacy_payment_status(None))


class TestStatusLabel(unittest.TestCase):
    """get_status_label: covers every example from the design spec."""

    def setUp(self):
        self.inv_due  = _FakeInvoice(balance_due=600)
        self.inv_zero = _FakeInvoice(balance_due=0)

    def test_user_spec_examples_verbatim(self):
        """All examples from the user's design spec, verbatim."""
        cases = [
            (('new_request',      'not_received',    None),         'New Request'),
            (('pending_payment',  'not_received',    None),         'Pending Payment'),
            (('payment_uploaded', 'pending_review',  None),         'Payment Uploaded / Needs Review'),
            (('payment_verified', 'verified',        self.inv_due), 'Partial Payment Verified'),
            (('payment_verified', 'verified',        self.inv_zero),'Payment Verified'),
            (('confirmed',        'verified',        self.inv_zero),'Confirmed'),
            (('confirmed',        'verified',        self.inv_due), 'Confirmed / Balance Due'),
            (('cancelled',        'not_received',    None),         'Cancelled'),
            (('rejected',         'rejected',        None),         'Rejected'),
            (('rejected',         'mismatch',        None),         'Rejected'),
        ]
        for inputs, expected in cases:
            with self.subTest(inputs=inputs):
                self.assertEqual(get_status_label(*inputs), expected)

    def test_balance_due_drives_partial_display(self):
        """When booking is verified/confirmed AND balance > 0, label says so."""
        self.assertEqual(get_status_label('confirmed', 'verified', self.inv_due),
                         'Confirmed / Balance Due')
        self.assertEqual(get_status_label('confirmed', 'verified', self.inv_zero),
                         'Confirmed')
        self.assertEqual(get_status_label('checked_in', 'verified', self.inv_due),
                         'Checked In / Balance Due')
        self.assertEqual(get_status_label('checked_in', 'verified', self.inv_zero),
                         'Checked In')

    def test_legacy_inputs_render_correctly(self):
        """Old DB rows render with the new vocabulary's labels."""
        self.assertEqual(get_status_label('unconfirmed', 'unpaid', None),
                         'Pending Payment')
        self.assertEqual(get_status_label('pending_verification', 'unpaid', None),
                         'Payment Uploaded / Needs Review')
        # Legacy 'partial' on a confirmed booking with balance due
        self.assertEqual(get_status_label('confirmed', 'partial', self.inv_due),
                         'Confirmed / Balance Due')
        # Legacy 'paid' on a confirmed booking with zero balance
        self.assertEqual(get_status_label('confirmed', 'paid', self.inv_zero),
                         'Confirmed')

    def test_mismatch_payment_shows_distinct_label(self):
        self.assertEqual(get_status_label('payment_uploaded', 'mismatch', None),
                         'Payment Uploaded / Amount Mismatch')

    def test_cancelled_rejected_terminal_states(self):
        """Cancelled/rejected labels do not vary with payment_status or invoice."""
        for p in PAYMENT_STATUSES:
            self.assertEqual(get_status_label('cancelled', p, None), 'Cancelled')
            self.assertEqual(get_status_label('rejected',  p, None), 'Rejected')

    def test_unknown_input_does_not_crash(self):
        """Garbage input must produce a fallback label, never an exception."""
        # Should not raise:
        label = get_status_label('mystery_status', 'who_knows', None)
        self.assertIsInstance(label, str)
        self.assertTrue(len(label) > 0)
        # Should not raise on None:
        label = get_status_label(None, None, None)
        self.assertIsInstance(label, str)


class TestStatusBadgeClass(unittest.TestCase):
    """get_status_badge_class returns valid Tailwind class strings."""

    def setUp(self):
        self.inv_due  = _FakeInvoice(balance_due=600)
        self.inv_zero = _FakeInvoice(balance_due=0)

    def test_returns_string_with_bg_prefix(self):
        """Every output must be a Tailwind bg-* class set."""
        samples = [
            ('new_request', 'not_received', None),
            ('payment_uploaded', 'pending_review', None),
            ('payment_uploaded', 'mismatch', None),
            ('payment_verified', 'verified', self.inv_zero),
            ('payment_verified', 'verified', self.inv_due),
            ('confirmed', 'verified', self.inv_zero),
            ('confirmed', 'verified', self.inv_due),
            ('checked_in', 'verified', None),
            ('checked_out', 'verified', None),
            ('cancelled', 'verified', None),
            ('rejected', 'rejected', None),
            ('unconfirmed', 'unpaid', None),     # legacy
            ('confirmed', 'partial', self.inv_due),  # legacy
        ]
        for inputs in samples:
            with self.subTest(inputs=inputs):
                cls = get_status_badge_class(*inputs)
                self.assertIsInstance(cls, str)
                self.assertIn('bg-', cls, f'no bg-* class in: {cls!r}')

    def test_balance_due_changes_color(self):
        """confirmed + verified should be GREEN with no balance, YELLOW with balance."""
        green = get_status_badge_class('confirmed', 'verified', self.inv_zero)
        yellow = get_status_badge_class('confirmed', 'verified', self.inv_due)
        self.assertIn('green', green)
        self.assertIn('yellow', yellow)

    def test_mismatch_uses_orange(self):
        cls = get_status_badge_class('payment_uploaded', 'mismatch', None)
        self.assertIn('orange', cls)

    def test_terminal_states_use_gray(self):
        for b in ('cancelled', 'rejected'):
            cls = get_status_badge_class(b, 'verified', None)
            self.assertIn('gray', cls)

    def test_unknown_input_does_not_crash(self):
        cls = get_status_badge_class('mystery', 'who_knows', None)
        self.assertIsInstance(cls, str)
        self.assertIn('bg-', cls)


class TestPartialDerivedFromInvoice(unittest.TestCase):
    """Critical design rule: partial-payment is DERIVED from balance_due,
    never stored as payment_status."""

    def test_no_stored_partial_value_required_for_partial_display(self):
        """A row with payment_status='verified' and balance_due > 0 must
        produce a 'partial' display label and color WITHOUT needing to
        store 'partial' anywhere."""
        inv_due = _FakeInvoice(balance_due=200)
        label = get_status_label('confirmed', 'verified', inv_due)
        cls   = get_status_badge_class('confirmed', 'verified', inv_due)
        self.assertEqual(label, 'Confirmed / Balance Due')
        self.assertIn('yellow', cls)

    def test_zero_balance_treated_as_fully_paid(self):
        inv_zero = _FakeInvoice(balance_due=0)
        label = get_status_label('confirmed', 'verified', inv_zero)
        cls   = get_status_badge_class('confirmed', 'verified', inv_zero)
        self.assertEqual(label, 'Confirmed')
        self.assertIn('green', cls)

    def test_negative_balance_treated_as_fully_paid_too(self):
        """Defensive — if amount_paid > total_amount, balance_due may be
        negative; still considered fully paid."""
        inv_neg = _FakeInvoice(balance_due=-50)
        label = get_status_label('confirmed', 'verified', inv_neg)
        self.assertEqual(label, 'Confirmed')

    def test_no_invoice_means_no_balance_due(self):
        """Booking without an invoice should not assume a balance due."""
        label = get_status_label('confirmed', 'verified', None)
        self.assertEqual(label, 'Confirmed')

    def test_invoice_with_invalid_balance_does_not_crash(self):
        """Non-numeric balance_due (bug or stale data) must not raise."""
        class BrokenInvoice:
            balance_due = 'not-a-number'
        # Should not raise:
        label = get_status_label('confirmed', 'verified', BrokenInvoice())
        self.assertIsInstance(label, str)


class TestCanConfirm(unittest.TestCase):
    """can_confirm() — admin Confirm precondition (Phase 0 unblocker).

    Pre-confirmation source states (both legacy and new vocab) must be
    accepted; post-confirmation and terminal states must be refused.
    """

    def test_allows_new_vocab_pre_confirmation_states(self):
        """The four new-vocabulary pre-confirmation states must be confirmable."""
        for s in ('new_request', 'pending_payment', 'payment_uploaded', 'payment_verified'):
            with self.subTest(status=s):
                self.assertTrue(can_confirm(s),
                                f'{s!r} must be confirmable (new-vocab pre-confirmation state)')

    def test_allows_legacy_pre_confirmation_states(self):
        """Legacy values still in DB rows must remain confirmable."""
        for s in ('unconfirmed', 'pending_verification'):
            with self.subTest(status=s):
                self.assertTrue(can_confirm(s),
                                f'{s!r} must be confirmable for backward compat with existing rows')

    def test_blocks_post_confirmation_state(self):
        """Already-confirmed bookings cannot be re-confirmed."""
        self.assertFalse(can_confirm('confirmed'))

    def test_blocks_in_house_states(self):
        """Once a guest is in-house, the booking cannot transition back."""
        for s in ('checked_in', 'checked_out'):
            with self.subTest(status=s):
                self.assertFalse(can_confirm(s),
                                 f'{s!r} must NOT be confirmable')

    def test_blocks_terminal_states(self):
        """Cancelled/rejected bookings cannot be confirmed."""
        for s in ('cancelled', 'rejected'):
            with self.subTest(status=s):
                self.assertFalse(can_confirm(s),
                                 f'{s!r} must NOT be confirmable (terminal state)')

    def test_blocks_unknown_status(self):
        """Unknown / None / empty must default to refused (safe default)."""
        self.assertFalse(can_confirm('garbage'))
        self.assertFalse(can_confirm(None))
        self.assertFalse(can_confirm(''))

    def test_CONFIRMABLE_FROM_membership_matches_helper(self):
        """can_confirm(s) must return True iff s is in CONFIRMABLE_FROM."""
        all_statuses = list(BOOKING_STATUSES) + ['unconfirmed', 'pending_verification']
        for s in all_statuses:
            self.assertEqual(can_confirm(s), s in CONFIRMABLE_FROM,
                             f'mismatch for {s!r}')

    def test_CONFIRMABLE_FROM_excludes_post_confirmation_states(self):
        """The constant itself must not contain any post-confirmation state."""
        for s in ('confirmed', 'checked_in', 'checked_out', 'cancelled', 'rejected'):
            self.assertNotIn(s, CONFIRMABLE_FROM)

    def test_target_pair_after_confirm_is_canonically_valid(self):
        """The IDEAL post-confirm pair (confirmed, verified) is in VALID_STATUS_PAIRS,
        even if the route currently writes legacy 'paid' for back-compat with
        accounting queries."""
        self.assertTrue(is_valid_status_pair('confirmed', 'verified'))
        # Legacy 'paid' is NOT a canonical value — the route uses it temporarily
        # but is_valid_status_pair correctly refuses to bless it:
        self.assertFalse(is_valid_status_pair('confirmed', 'paid'))


if __name__ == '__main__':
    unittest.main(verbosity=2)
