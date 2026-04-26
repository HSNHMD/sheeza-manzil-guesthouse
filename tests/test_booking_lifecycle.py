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
    REVENUE_PAYMENT_STATUSES,
    OUTSTANDING_PAYMENT_STATUSES,
    can_confirm,
    can_confirm_booking,
    can_verify_payment,
    can_mark_mismatch,
    can_mark_pending_review,
    can_reject_payment,
    can_cancel,
    can_check_in,
    can_check_out,
    is_valid_booking_status,
    is_valid_payment_status,
    is_valid_status_pair,
    normalize_legacy_payment_status,
    get_status_label,
    get_status_badge_class,
)


class _FakeBooking:
    """Minimal Booking-like stub for can_confirm_booking() tests."""

    def __init__(self, status=None, payment_slip_filename=None, invoice=None):
        self.status = status
        self.payment_slip_filename = payment_slip_filename
        self.invoice = invoice
        self.booking_ref = 'BKTEST00'


class _FakeInvoiceFull:
    """Invoice-like stub with payment_status + amount_paid + balance_due."""

    def __init__(self, payment_status='unpaid', amount_paid=0.0, total_amount=600.0):
        self.payment_status = payment_status
        self.amount_paid = amount_paid
        self.total_amount = total_amount

    @property
    def balance_due(self):
        return self.total_amount - self.amount_paid


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


class TestCanConfirmBooking(unittest.TestCase):
    """can_confirm_booking() — full business rule (status + payment evidence).

    Prevents the previously-permitted unsafe transition:
        pending_payment + not_received  →  confirmed
    by requiring payment evidence in addition to a confirmable status.
    """

    # ── Pre-confirmation states with NO payment evidence: must REFUSE ────────
    def test_pending_payment_not_received_no_slip_cannot_confirm(self):
        b = _FakeBooking(
            status='pending_payment',
            payment_slip_filename=None,
            invoice=_FakeInvoiceFull(payment_status='not_received', amount_paid=0),
        )
        self.assertFalse(can_confirm_booking(b))

    def test_new_request_not_received_no_slip_cannot_confirm(self):
        b = _FakeBooking(
            status='new_request',
            payment_slip_filename=None,
            invoice=_FakeInvoiceFull(payment_status='not_received', amount_paid=0),
        )
        self.assertFalse(can_confirm_booking(b))

    def test_pending_payment_with_no_invoice_at_all_cannot_confirm(self):
        b = _FakeBooking(status='pending_payment', payment_slip_filename=None, invoice=None)
        self.assertFalse(can_confirm_booking(b))

    # ── Pre-confirmation states WITH payment evidence: must ALLOW ───────────
    def test_payment_uploaded_with_slip_can_confirm(self):
        b = _FakeBooking(
            status='payment_uploaded',
            payment_slip_filename='slip_test.jpg',
            invoice=_FakeInvoiceFull(payment_status='pending_review', amount_paid=0),
        )
        self.assertTrue(can_confirm_booking(b))

    def test_payment_uploaded_no_slip_cannot_confirm_data_anomaly(self):
        """Defensive: if status says 'payment_uploaded' but no slip is on
        file, treat as data corruption and refuse confirmation."""
        b = _FakeBooking(
            status='payment_uploaded',
            payment_slip_filename=None,
            invoice=_FakeInvoiceFull(payment_status='pending_review', amount_paid=0),
        )
        # pending_review IS evidence (admin will review the supposedly-uploaded
        # slip), so this passes — anomaly is a data-integrity concern, not a
        # confirmation-block concern. Verifying current behavior:
        self.assertTrue(can_confirm_booking(b))

    def test_payment_verified_can_confirm(self):
        """payment_verified is the dedicated evidence-reviewed state — admin
        can always proceed to 'confirmed' from here."""
        b = _FakeBooking(
            status='payment_verified',
            payment_slip_filename='slip_test.jpg',
            invoice=_FakeInvoiceFull(payment_status='verified', amount_paid=600),
        )
        self.assertTrue(can_confirm_booking(b))

    def test_payment_verified_without_slip_still_confirmable(self):
        """payment_verified should NOT require a slip to be present —
        admin verified payment by some other means (cash, bank transfer
        confirmation outside the app)."""
        b = _FakeBooking(
            status='payment_verified',
            payment_slip_filename=None,
            invoice=_FakeInvoiceFull(payment_status='verified', amount_paid=600),
        )
        self.assertTrue(can_confirm_booking(b))

    # ── Bad payment states: must REFUSE regardless of status ────────────────
    def test_payment_uploaded_with_mismatch_cannot_confirm(self):
        b = _FakeBooking(
            status='payment_uploaded',
            payment_slip_filename='slip_test.jpg',  # slip exists but flagged
            invoice=_FakeInvoiceFull(payment_status='mismatch', amount_paid=0),
        )
        self.assertFalse(can_confirm_booking(b))

    def test_any_state_with_rejected_payment_cannot_confirm(self):
        """Even from a normally-confirmable state, a rejected invoice
        blocks confirmation."""
        b = _FakeBooking(
            status='payment_uploaded',
            payment_slip_filename='slip_test.jpg',
            invoice=_FakeInvoiceFull(payment_status='rejected', amount_paid=0),
        )
        self.assertFalse(can_confirm_booking(b))

    # ── Post-confirmation and terminal states: always REFUSE ────────────────
    def test_already_confirmed_cannot_be_re_confirmed(self):
        b = _FakeBooking(
            status='confirmed',
            payment_slip_filename='slip_test.jpg',
            invoice=_FakeInvoiceFull(payment_status='verified', amount_paid=600),
        )
        self.assertFalse(can_confirm_booking(b))

    def test_in_house_states_cannot_confirm(self):
        for s in ('checked_in', 'checked_out'):
            with self.subTest(status=s):
                b = _FakeBooking(
                    status=s,
                    payment_slip_filename='slip_test.jpg',
                    invoice=_FakeInvoiceFull(payment_status='verified', amount_paid=600),
                )
                self.assertFalse(can_confirm_booking(b),
                                 f'{s} must not be confirmable')

    def test_terminal_states_cannot_confirm(self):
        for s in ('cancelled', 'rejected'):
            with self.subTest(status=s):
                b = _FakeBooking(
                    status=s,
                    payment_slip_filename='slip_test.jpg',
                    invoice=_FakeInvoiceFull(payment_status='verified', amount_paid=600),
                )
                self.assertFalse(can_confirm_booking(b))

    # ── Legacy compat with payment evidence: must ALLOW ─────────────────────
    def test_legacy_unconfirmed_with_slip_can_confirm(self):
        b = _FakeBooking(
            status='unconfirmed',
            payment_slip_filename='slip_test.jpg',
            invoice=_FakeInvoiceFull(payment_status='unpaid', amount_paid=0),
        )
        self.assertTrue(can_confirm_booking(b))

    def test_legacy_pending_verification_with_slip_can_confirm(self):
        b = _FakeBooking(
            status='pending_verification',
            payment_slip_filename='slip_test.jpg',
            invoice=_FakeInvoiceFull(payment_status='unpaid', amount_paid=0),
        )
        self.assertTrue(can_confirm_booking(b))

    def test_legacy_unconfirmed_with_partial_payment_can_confirm(self):
        """Legacy 'partial' is treated as evidence — guest paid SOMETHING
        and staff already recorded it."""
        b = _FakeBooking(
            status='unconfirmed',
            payment_slip_filename=None,
            invoice=_FakeInvoiceFull(payment_status='partial', amount_paid=200),
        )
        self.assertTrue(can_confirm_booking(b))

    def test_legacy_unconfirmed_with_paid_payment_can_confirm(self):
        b = _FakeBooking(
            status='unconfirmed',
            payment_slip_filename=None,
            invoice=_FakeInvoiceFull(payment_status='paid', amount_paid=600),
        )
        self.assertTrue(can_confirm_booking(b))

    # ── Legacy compat WITHOUT payment evidence: must REFUSE ─────────────────
    def test_legacy_unconfirmed_without_evidence_cannot_confirm(self):
        b = _FakeBooking(
            status='unconfirmed',
            payment_slip_filename=None,
            invoice=_FakeInvoiceFull(payment_status='unpaid', amount_paid=0),
        )
        self.assertFalse(can_confirm_booking(b))

    def test_legacy_pending_verification_without_evidence_cannot_confirm(self):
        """Defensive: pending_verification SHOULD imply slip exists (it's
        the legacy equivalent of 'payment_uploaded'). If slip column is
        empty AND no other evidence, treat the row as data-anomalous and
        refuse confirmation rather than silently allow."""
        b = _FakeBooking(
            status='pending_verification',
            payment_slip_filename=None,
            invoice=_FakeInvoiceFull(payment_status='unpaid', amount_paid=0),
        )
        self.assertFalse(can_confirm_booking(b))

    # ── Recorded amount_paid > 0 alone is sufficient evidence ───────────────
    def test_amount_paid_alone_is_evidence_even_with_unrecognized_payment_status(self):
        """If staff recorded a cash payment, that's evidence regardless of
        what payment_status string is on file."""
        b = _FakeBooking(
            status='unconfirmed',
            payment_slip_filename=None,
            invoice=_FakeInvoiceFull(payment_status='unpaid', amount_paid=100),
        )
        self.assertTrue(can_confirm_booking(b))

    # ── Defensive None / bad-data handling ──────────────────────────────────
    def test_None_booking_is_not_confirmable(self):
        self.assertFalse(can_confirm_booking(None))

    def test_invoice_arg_overrides_booking_invoice(self):
        """If caller passes an explicit invoice arg, that's used in lieu of
        booking.invoice — useful for tests and for routes that want to
        validate against a hypothetical post-update invoice state."""
        b = _FakeBooking(
            status='pending_payment',
            payment_slip_filename=None,
            invoice=_FakeInvoiceFull(payment_status='not_received', amount_paid=0),  # would refuse
        )
        # Override with a verified invoice → allows
        override = _FakeInvoiceFull(payment_status='verified', amount_paid=600)
        self.assertTrue(can_confirm_booking(b, invoice=override))

    def test_booking_with_non_numeric_amount_paid_does_not_crash(self):
        """If amount_paid is somehow a non-numeric value, fall back to 0."""
        class WeirdInvoice:
            payment_status = 'not_received'
            amount_paid = 'not a number'
        b = _FakeBooking(status='pending_payment', payment_slip_filename=None, invoice=WeirdInvoice())
        # Should not raise, should return False (no evidence)
        self.assertFalse(can_confirm_booking(b))


class TestCanVerifyPayment(unittest.TestCase):
    """can_verify_payment() — admin "Verify Payment" precondition."""

    def test_payment_uploaded_pending_review_with_slip_can_verify(self):
        b = _FakeBooking(status='payment_uploaded', payment_slip_filename='slip.jpg',
                         invoice=_FakeInvoiceFull(payment_status='pending_review'))
        self.assertTrue(can_verify_payment(b))

    def test_payment_uploaded_mismatch_with_slip_can_verify(self):
        """Admin can verify after fixing a previously-flagged mismatch."""
        b = _FakeBooking(status='payment_uploaded', payment_slip_filename='slip.jpg',
                         invoice=_FakeInvoiceFull(payment_status='mismatch', amount_paid=600))
        self.assertTrue(can_verify_payment(b))

    def test_legacy_pending_verification_with_slip_can_verify(self):
        b = _FakeBooking(status='pending_verification', payment_slip_filename='slip.jpg',
                         invoice=_FakeInvoiceFull(payment_status='unpaid'))
        self.assertTrue(can_verify_payment(b))

    def test_payment_uploaded_no_slip_no_amount_cannot_verify(self):
        """No evidence at all → refuse."""
        b = _FakeBooking(status='payment_uploaded', payment_slip_filename=None,
                         invoice=_FakeInvoiceFull(payment_status='pending_review', amount_paid=0))
        self.assertFalse(can_verify_payment(b))

    def test_already_verified_cannot_be_re_verified(self):
        b = _FakeBooking(status='payment_verified', payment_slip_filename='slip.jpg',
                         invoice=_FakeInvoiceFull(payment_status='verified', amount_paid=600))
        self.assertFalse(can_verify_payment(b))

    def test_confirmed_booking_cannot_re_run_payment_verify(self):
        b = _FakeBooking(status='confirmed', payment_slip_filename='slip.jpg',
                         invoice=_FakeInvoiceFull(payment_status='verified', amount_paid=600))
        self.assertFalse(can_verify_payment(b))

    def test_rejected_payment_cannot_be_verified(self):
        """If payment has been rejected, must reset to pending_review first."""
        b = _FakeBooking(status='payment_uploaded', payment_slip_filename='slip.jpg',
                         invoice=_FakeInvoiceFull(payment_status='rejected'))
        self.assertFalse(can_verify_payment(b))


class TestCanMarkMismatch(unittest.TestCase):
    """can_mark_mismatch() — flag amount mismatch on a slip in review."""

    def test_payment_uploaded_pending_review_with_slip_can_mark_mismatch(self):
        b = _FakeBooking(status='payment_uploaded', payment_slip_filename='slip.jpg',
                         invoice=_FakeInvoiceFull(payment_status='pending_review'))
        self.assertTrue(can_mark_mismatch(b))

    def test_already_marked_mismatch_cannot_re_mark(self):
        b = _FakeBooking(status='payment_uploaded', payment_slip_filename='slip.jpg',
                         invoice=_FakeInvoiceFull(payment_status='mismatch'))
        self.assertFalse(can_mark_mismatch(b))

    def test_no_slip_cannot_mark_mismatch(self):
        b = _FakeBooking(status='payment_uploaded', payment_slip_filename=None,
                         invoice=_FakeInvoiceFull(payment_status='pending_review'))
        self.assertFalse(can_mark_mismatch(b))

    def test_pending_payment_cannot_mark_mismatch(self):
        """No slip uploaded yet → mismatch action makes no sense."""
        b = _FakeBooking(status='pending_payment', payment_slip_filename=None,
                         invoice=_FakeInvoiceFull(payment_status='not_received'))
        self.assertFalse(can_mark_mismatch(b))


class TestCanMarkPendingReview(unittest.TestCase):
    """can_mark_pending_review() — undo a mismatch back to review queue."""

    def test_mismatch_can_be_re_queued(self):
        b = _FakeBooking(status='payment_uploaded', payment_slip_filename='slip.jpg',
                         invoice=_FakeInvoiceFull(payment_status='mismatch'))
        self.assertTrue(can_mark_pending_review(b))

    def test_already_pending_cannot_re_queue(self):
        b = _FakeBooking(status='payment_uploaded', payment_slip_filename='slip.jpg',
                         invoice=_FakeInvoiceFull(payment_status='pending_review'))
        self.assertFalse(can_mark_pending_review(b))

    def test_verified_cannot_be_re_queued(self):
        b = _FakeBooking(status='payment_verified', payment_slip_filename='slip.jpg',
                         invoice=_FakeInvoiceFull(payment_status='verified'))
        self.assertFalse(can_mark_pending_review(b))


class TestCanRejectPayment(unittest.TestCase):
    """can_reject_payment() — admin rejects payment outright."""

    def test_pending_review_can_be_rejected(self):
        b = _FakeBooking(status='payment_uploaded', payment_slip_filename='slip.jpg',
                         invoice=_FakeInvoiceFull(payment_status='pending_review'))
        self.assertTrue(can_reject_payment(b))

    def test_mismatch_can_be_rejected(self):
        b = _FakeBooking(status='payment_uploaded', payment_slip_filename='slip.jpg',
                         invoice=_FakeInvoiceFull(payment_status='mismatch'))
        self.assertTrue(can_reject_payment(b))

    def test_already_verified_cannot_be_rejected_via_this_route(self):
        """Rejecting an already-verified payment is a different scenario
        (e.g. chargeback) — not handled by this admin action."""
        b = _FakeBooking(status='payment_verified', payment_slip_filename='slip.jpg',
                         invoice=_FakeInvoiceFull(payment_status='verified'))
        self.assertFalse(can_reject_payment(b))

    def test_pending_payment_cannot_be_rejected(self):
        """No payment to reject yet."""
        b = _FakeBooking(status='pending_payment', payment_slip_filename=None,
                         invoice=_FakeInvoiceFull(payment_status='not_received'))
        self.assertFalse(can_reject_payment(b))


class TestCanCancel(unittest.TestCase):
    """can_cancel() — cancellation allowed unless terminal."""

    def test_pre_confirmation_states_can_cancel(self):
        for s in ('new_request', 'pending_payment', 'payment_uploaded',
                  'payment_verified', 'unconfirmed', 'pending_verification'):
            with self.subTest(status=s):
                self.assertTrue(can_cancel(s))

    def test_confirmed_can_cancel(self):
        self.assertTrue(can_cancel('confirmed'))

    def test_checked_in_can_cancel(self):
        """Edge case: guest in-house but business decision to cancel.
        Allow it; the room-status reset handles the side-effect."""
        self.assertTrue(can_cancel('checked_in'))

    def test_terminal_states_cannot_cancel(self):
        for s in ('cancelled', 'rejected', 'checked_out'):
            with self.subTest(status=s):
                self.assertFalse(can_cancel(s))

    def test_unknown_cannot_cancel(self):
        self.assertFalse(can_cancel('garbage'))
        self.assertFalse(can_cancel(None))


class TestCanCheckIn(unittest.TestCase):
    """can_check_in() — gate for /bookings/<id>/checkin."""

    def test_confirmed_with_verified_payment_can_check_in(self):
        b = _FakeBooking(status='confirmed', payment_slip_filename='slip.jpg',
                         invoice=_FakeInvoiceFull(payment_status='verified', amount_paid=600))
        self.assertTrue(can_check_in(b))

    def test_confirmed_with_legacy_paid_can_check_in(self):
        b = _FakeBooking(status='confirmed', payment_slip_filename='slip.jpg',
                         invoice=_FakeInvoiceFull(payment_status='paid', amount_paid=600))
        self.assertTrue(can_check_in(b))

    def test_confirmed_with_legacy_partial_can_check_in(self):
        """Partial-payment guests can check in; balance can be settled at desk."""
        b = _FakeBooking(status='confirmed', payment_slip_filename='slip.jpg',
                         invoice=_FakeInvoiceFull(payment_status='partial', amount_paid=300))
        self.assertTrue(can_check_in(b))

    def test_confirmed_with_pending_review_cannot_check_in(self):
        """Slip uploaded but not yet verified — must not let guest in."""
        b = _FakeBooking(status='confirmed', payment_slip_filename='slip.jpg',
                         invoice=_FakeInvoiceFull(payment_status='pending_review'))
        self.assertFalse(can_check_in(b))

    def test_confirmed_with_no_payment_evidence_cannot_check_in(self):
        b = _FakeBooking(status='confirmed', payment_slip_filename=None,
                         invoice=_FakeInvoiceFull(payment_status='not_received', amount_paid=0))
        self.assertFalse(can_check_in(b))

    def test_payment_verified_cannot_check_in_yet(self):
        """payment_verified is the verify state; admin must Confirm first
        before check-in."""
        b = _FakeBooking(status='payment_verified', payment_slip_filename='slip.jpg',
                         invoice=_FakeInvoiceFull(payment_status='verified', amount_paid=600))
        self.assertFalse(can_check_in(b))

    def test_already_checked_in_cannot_re_check_in(self):
        b = _FakeBooking(status='checked_in', payment_slip_filename='slip.jpg',
                         invoice=_FakeInvoiceFull(payment_status='verified', amount_paid=600))
        self.assertFalse(can_check_in(b))

    def test_cancelled_cannot_check_in(self):
        b = _FakeBooking(status='cancelled', payment_slip_filename='slip.jpg',
                         invoice=_FakeInvoiceFull(payment_status='verified', amount_paid=600))
        self.assertFalse(can_check_in(b))

    def test_no_invoice_cannot_check_in(self):
        b = _FakeBooking(status='confirmed', payment_slip_filename='slip.jpg', invoice=None)
        self.assertFalse(can_check_in(b))


class TestCanCheckOut(unittest.TestCase):
    """can_check_out() — only from checked_in."""

    def test_checked_in_can_check_out(self):
        self.assertTrue(can_check_out('checked_in'))

    def test_other_states_cannot_check_out(self):
        for s in ('new_request', 'pending_payment', 'payment_uploaded',
                  'payment_verified', 'confirmed', 'checked_out',
                  'cancelled', 'rejected',
                  'unconfirmed', 'pending_verification'):
            with self.subTest(status=s):
                self.assertFalse(can_check_out(s))

    def test_unknown_cannot_check_out(self):
        self.assertFalse(can_check_out('garbage'))
        self.assertFalse(can_check_out(None))


class TestUserSpecifiedTransitionRequirements(unittest.TestCase):
    """Direct verification of the user's explicit safety rules."""

    def test_confirmed_plus_not_received_is_invalid_pair(self):
        """Per user: 'Do not allow confirmed + not_received'."""
        self.assertFalse(is_valid_status_pair('confirmed', 'not_received'))

    def test_check_in_requires_confirmed(self):
        """Per user: 'Do not allow check-in unless booking is confirmed.'"""
        for s in BOOKING_STATUSES:
            if s == 'confirmed':
                continue
            b = _FakeBooking(status=s, payment_slip_filename='slip.jpg',
                             invoice=_FakeInvoiceFull(payment_status='verified', amount_paid=600))
            with self.subTest(status=s):
                self.assertFalse(can_check_in(b),
                                 f'{s!r} must NOT allow check-in (not confirmed)')

    def test_check_out_requires_checked_in(self):
        """Per user: 'Do not allow check-out unless booking is checked_in.'"""
        for s in BOOKING_STATUSES:
            if s == 'checked_in':
                continue
            with self.subTest(status=s):
                self.assertFalse(can_check_out(s),
                                 f'{s!r} must NOT allow check-out (not checked_in)')

    def test_cancelled_terminal(self):
        """Per user: 'Cancelled/rejected bookings should not be check-in/check-out/confirmed.'"""
        b = _FakeBooking(status='cancelled', payment_slip_filename='slip.jpg',
                         invoice=_FakeInvoiceFull(payment_status='verified', amount_paid=600))
        self.assertFalse(can_confirm_booking(b))
        self.assertFalse(can_check_in(b))
        self.assertFalse(can_check_out('cancelled'))
        self.assertFalse(can_cancel('cancelled'))  # already cancelled

    def test_rejected_terminal(self):
        b = _FakeBooking(status='rejected', payment_slip_filename='slip.jpg',
                         invoice=_FakeInvoiceFull(payment_status='rejected'))
        self.assertFalse(can_confirm_booking(b))
        self.assertFalse(can_check_in(b))
        self.assertFalse(can_check_out('rejected'))
        self.assertFalse(can_cancel('rejected'))

    def test_payment_verification_requires_evidence(self):
        """Per user: 'Do not allow payment verification if there is no payment evidence.'"""
        b = _FakeBooking(status='payment_uploaded', payment_slip_filename=None,
                         invoice=_FakeInvoiceFull(payment_status='pending_review', amount_paid=0))
        self.assertFalse(can_verify_payment(b))


class TestRevenueAndOutstandingFilterMembership(unittest.TestCase):
    """REVENUE_PAYMENT_STATUSES and OUTSTANDING_PAYMENT_STATUSES are the
    single source of truth for the accounting/invoice query filters.
    These tests guard against regressions where a future code change drops
    one of the values from the lists, silently breaking revenue reports."""

    # ── Revenue filter membership ───────────────────────────────────────────
    def test_legacy_paid_in_revenue(self):
        """Old DB rows with payment_status='paid' must still count as revenue."""
        self.assertIn('paid', REVENUE_PAYMENT_STATUSES)

    def test_legacy_partial_in_revenue(self):
        """Partial payments contribute to revenue (the part received)."""
        self.assertIn('partial', REVENUE_PAYMENT_STATUSES)

    def test_new_verified_in_revenue(self):
        """The post-fix headline check: invoices marked 'verified' by the
        new admin Verify Payment button MUST appear in revenue reports."""
        self.assertIn('verified', REVENUE_PAYMENT_STATUSES)

    def test_revenue_excludes_unpaid_and_not_received(self):
        """No money received yet → not revenue."""
        self.assertNotIn('unpaid',       REVENUE_PAYMENT_STATUSES)
        self.assertNotIn('not_received', REVENUE_PAYMENT_STATUSES)

    def test_revenue_excludes_pending_review(self):
        """Slip uploaded but not yet verified → not revenue (only trusted
        payments count)."""
        self.assertNotIn('pending_review', REVENUE_PAYMENT_STATUSES)

    def test_revenue_excludes_rejected_and_mismatch(self):
        """Bad payments → not revenue."""
        self.assertNotIn('rejected', REVENUE_PAYMENT_STATUSES)
        self.assertNotIn('mismatch', REVENUE_PAYMENT_STATUSES)

    # ── Outstanding filter membership ───────────────────────────────────────
    def test_legacy_unpaid_in_outstanding(self):
        """Old DB rows with payment_status='unpaid' must still appear in
        outstanding/receivables reports."""
        self.assertIn('unpaid', OUTSTANDING_PAYMENT_STATUSES)

    def test_legacy_partial_in_outstanding(self):
        """Partial payments still have a balance owing."""
        self.assertIn('partial', OUTSTANDING_PAYMENT_STATUSES)

    def test_new_not_received_in_outstanding(self):
        """New-vocab equivalent of 'unpaid' must be visible to admins."""
        self.assertIn('not_received', OUTSTANDING_PAYMENT_STATUSES)

    def test_new_pending_review_in_outstanding(self):
        """Slip-uploaded-but-not-yet-verified counts as outstanding because
        the money has not been booked as received."""
        self.assertIn('pending_review', OUTSTANDING_PAYMENT_STATUSES)

    def test_outstanding_excludes_paid_and_verified(self):
        """Fully paid → not outstanding."""
        self.assertNotIn('paid',     OUTSTANDING_PAYMENT_STATUSES)
        self.assertNotIn('verified', OUTSTANDING_PAYMENT_STATUSES)

    def test_outstanding_excludes_rejected_and_mismatch(self):
        """Bad payments are not 'owed' — they're problematic. Booking is
        presumed cancelled or in re-handling, not on the outstanding list."""
        self.assertNotIn('rejected', OUTSTANDING_PAYMENT_STATUSES)
        self.assertNotIn('mismatch', OUTSTANDING_PAYMENT_STATUSES)

    # ── Cross-list invariants ──────────────────────────────────────────────
    def test_partial_appears_in_both_lists(self):
        """Partial is the only value that's BOTH revenue (some money received)
        AND outstanding (balance still due) — this is a deliberate overlap."""
        self.assertIn('partial', REVENUE_PAYMENT_STATUSES)
        self.assertIn('partial', OUTSTANDING_PAYMENT_STATUSES)

    def test_paid_and_verified_never_outstanding(self):
        """A fully-paid (paid/verified) invoice can never be both fully paid
        AND outstanding — those values must NOT be in the outstanding list."""
        self.assertNotIn('paid',     OUTSTANDING_PAYMENT_STATUSES)
        self.assertNotIn('verified', OUTSTANDING_PAYMENT_STATUSES)

    def test_unpaid_and_not_received_never_revenue(self):
        """No-money-received states can never be revenue."""
        self.assertNotIn('unpaid',       REVENUE_PAYMENT_STATUSES)
        self.assertNotIn('not_received', REVENUE_PAYMENT_STATUSES)


class TestQuerySitesUseConstants(unittest.TestCase):
    """Defensive: the source files that hold accounting/invoice queries
    must import and reference the canonical constants — not hardcode the
    legacy literal lists. Catches regressions where a developer reverts
    or duplicates a filter literal somewhere."""

    def _read(self, relpath):
        with open(os.path.join(_PROJECT_ROOT, relpath)) as f:
            return f.read()

    def test_accounting_imports_constants(self):
        src = self._read('app/routes/accounting.py')
        self.assertIn('REVENUE_PAYMENT_STATUSES', src,
                      'accounting.py must import REVENUE_PAYMENT_STATUSES')
        self.assertIn('OUTSTANDING_PAYMENT_STATUSES', src,
                      'accounting.py must import OUTSTANDING_PAYMENT_STATUSES')

    def test_invoices_imports_outstanding_constant(self):
        src = self._read('app/routes/invoices.py')
        self.assertIn('OUTSTANDING_PAYMENT_STATUSES', src,
                      'invoices.py must import OUTSTANDING_PAYMENT_STATUSES')

    def test_no_hardcoded_paid_partial_filter_in_accounting(self):
        """Defensive: no remaining literal `['paid', 'partial']` filter."""
        src = self._read('app/routes/accounting.py')
        self.assertNotIn("['paid', 'partial']", src,
                         'accounting.py should use REVENUE_PAYMENT_STATUSES, not literal list')

    def test_no_hardcoded_unpaid_partial_filter_in_accounting(self):
        src = self._read('app/routes/accounting.py')
        self.assertNotIn("['unpaid', 'partial']", src,
                         'accounting.py should use OUTSTANDING_PAYMENT_STATUSES, not literal list')

    def test_no_hardcoded_unpaid_partial_filter_in_invoices(self):
        src = self._read('app/routes/invoices.py')
        self.assertNotIn("['unpaid', 'partial']", src,
                         'invoices.py should use OUTSTANDING_PAYMENT_STATUSES, not literal list')


if __name__ == '__main__':
    unittest.main(verbosity=2)
