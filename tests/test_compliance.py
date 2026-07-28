# tests/test_compliance.py
"""
The verdict on a single receipt.

Built on the real Receipt model rather than a stand-in, so a renamed column breaks a
test here instead of quietly turning every check into a pass.

The recurring shape is a 118.00 receipt: 100.00 net, 18.00 VAT at the standard rate,
which is the arithmetic every Tanzanian tax invoice does.
"""
from datetime import date, time
from decimal import Decimal

import pytest

from models.user import Receipt, ReceiptItem, ReceiptTaxLine
from utils import compliance

OUR_TIN = '108537108'
TODAY = date(2026, 7, 27)


def build_receipt(**overrides):
    """A clean, claimable, standard-rated receipt, unless told otherwise."""
    items = overrides.pop('items', [('DIESEL AGO', 118_00, 'A')])
    tax_lines = overrides.pop('tax_lines', [('A', 18, 18_00)])

    fields = dict(
        vendor_name='PLASCO LIMITED', vendor_tin='100147181', vrn='10007206H',
        receipt_verification_code='58E41A514', extraction_source='tra_html',
        customer_name='NZEGA URBAN WATER', customer_id_type='TIN', customer_id=OUR_TIN,
        receipt_date=date(2026, 7, 1), receipt_time=time(10, 30),
        is_cancelled=False, is_test=False,
        total_incl_tax_cents=118_00, total_excl_tax_cents=100_00, total_tax_cents=18_00,
    )
    fields.update(overrides)

    receipt = Receipt(**fields)
    for index, (description, amount, code) in enumerate(items, start=1):
        receipt.items.append(ReceiptItem(
            line_number=index, description=description, amount_cents=amount, tax_code=code,
        ))
    for code, rate, amount in tax_lines:
        receipt.tax_lines.append(ReceiptTaxLine(code=code, rate=Decimal(rate), amount_cents=amount))
    return receipt


def assess(receipt, business_tin=OUR_TIN, **kwargs):
    return compliance.evaluate(receipt, business_tin=business_tin, today=TODAY, **kwargs)


def status_of(assessment, check_id):
    check = assessment.check(check_id)
    return check.status if check else None


# --- The check that decides most claims: is it made out to us? --------------

def test_receipt_issued_to_our_tin_is_claimable():
    assessment = assess(build_receipt())
    assert status_of(assessment, 'buyer_tin') == compliance.PASS
    assert assessment.recoverable_vat_cents == 18_00
    assert assessment.recovery_blockers == []


def test_receipt_issued_to_another_tin_is_not_claimable():
    assessment = assess(build_receipt(customer_id='100999888', customer_name='SOMEONE ELSE LTD'))
    assert status_of(assessment, 'buyer_tin') == compliance.FAIL
    assert assessment.recoverable_vat_cents == 0
    assert 'it is not issued to your TIN' in assessment.recovery_blockers
    assert '100999888' in assessment.check('buyer_tin').detail


def test_walk_in_receipt_with_no_buyer_is_not_claimable():
    assessment = assess(build_receipt(customer_id=None, customer_id_type=None, customer_name=None))
    assert status_of(assessment, 'buyer_tin') == compliance.FAIL
    assert assessment.recoverable_vat_cents == 0


def test_tin_comparison_ignores_punctuation():
    """'108-537-108' and '108537108' are the same taxpayer."""
    assessment = assess(build_receipt(customer_id='108-537-108'))
    assert status_of(assessment, 'buyer_tin') == compliance.PASS


def test_buyer_identified_by_something_other_than_a_tin_is_a_warning():
    assessment = assess(build_receipt(customer_id_type='DRIVING LICENCE', customer_id='4001234567'))
    assert status_of(assessment, 'buyer_tin') == compliance.WARN
    assert assessment.recoverable_vat_cents == 0


def test_unset_business_tin_reports_rather_than_judges():
    """
    An unconfigured instance must not report every receipt as unclaimable.

    Not knowing our own TIN is a missing setting, not a finding about the receipt, so
    the check is informational and the recoverable figure is left alone.
    """
    assessment = assess(build_receipt(), business_tin=None)
    assert status_of(assessment, 'buyer_tin') == compliance.INFO
    assert assessment.recoverable_vat_cents == 18_00
    assert assessment.check('buyer_tin').weight == 0


# --- Arithmetic -------------------------------------------------------------

def test_tax_is_recomputed_from_the_items():
    """Item amounts are tax-inclusive, so 118.00 at 18% carries 18.00 of tax."""
    assessment = assess(build_receipt())
    assert status_of(assessment, 'tax_arithmetic') == compliance.PASS


def test_understated_tax_is_caught_and_blocks_the_claim():
    assessment = assess(build_receipt(
        tax_lines=[('A', 18, 15_00)], total_tax_cents=15_00, total_excl_tax_cents=103_00,
    ))
    assert status_of(assessment, 'tax_arithmetic') == compliance.FAIL
    assert assessment.recoverable_vat_cents == 0
    assert 'the tax on it does not add up' in assessment.recovery_blockers


def test_totals_that_do_not_reconcile_are_caught():
    assessment = assess(build_receipt(total_excl_tax_cents=90_00))
    assert status_of(assessment, 'totals') == compliance.FAIL
    assert assessment.recoverable_vat_cents == 0


def test_a_cent_of_rounding_is_tolerated():
    """EFDs round each line, so the totals may legitimately be a cent out."""
    assessment = assess(build_receipt(total_excl_tax_cents=100_01))
    assert status_of(assessment, 'totals') == compliance.PASS


def test_tax_arithmetic_is_skipped_when_lines_carry_no_codes():
    """A photographed receipt has no per-line tax codes to recompute from."""
    assessment = assess(build_receipt(items=[('SUMMARIZED SALE', 118_00, None)]))
    assert status_of(assessment, 'tax_arithmetic') == compliance.NA
    assert assessment.recoverable_vat_cents == 18_00


# --- The supplier -----------------------------------------------------------

def test_vat_charged_without_a_vrn_is_a_rejected_claim():
    assessment = assess(build_receipt(vrn=None))
    assert status_of(assessment, 'vendor_vrn') == compliance.FAIL
    assert assessment.recoverable_vat_cents == 0
    assert 'the supplier is not VAT-registered' in assessment.recovery_blockers


def test_no_vrn_and_no_tax_is_not_held_against_the_supplier():
    """A supplier below the registration threshold charging no VAT is compliant."""
    assessment = assess(build_receipt(
        items=[('BREAD', 100_00, 'EX')], tax_lines=[('EX', 0, 0)], vrn=None,
        total_incl_tax_cents=100_00, total_excl_tax_cents=100_00, total_tax_cents=0,
    ))
    assert status_of(assessment, 'vendor_vrn') == compliance.NA
    assert assessment.recovery_blockers == []


def test_missing_supplier_tin_is_a_failure():
    assert status_of(assess(build_receipt(vendor_tin=None)), 'vendor_tin') == compliance.FAIL


# --- What is recoverable ----------------------------------------------------

def test_mixed_receipt_splits_standard_rated_from_exempt():
    assessment = assess(build_receipt(
        items=[('DIESEL', 118_00, 'A'), ('BREAD', 100_00, 'EX')],
        tax_lines=[('A', 18, 18_00), ('EX', 0, 0)],
        total_incl_tax_cents=218_00, total_excl_tax_cents=200_00, total_tax_cents=18_00,
    ))
    assert assessment.standard_rated_cents == 118_00
    assert assessment.standard_rated_excl_cents == 100_00
    assert assessment.zero_or_exempt_cents == 100_00
    assert assessment.recoverable_vat_cents == 18_00


def test_a_receipt_with_no_vat_has_nothing_to_recover():
    assessment = assess(build_receipt(
        items=[('BREAD', 100_00, 'EX')], tax_lines=[('EX', 0, 0)],
        total_incl_tax_cents=100_00, total_excl_tax_cents=100_00, total_tax_cents=0,
    ))
    assert assessment.input_vat_cents == 0
    assert status_of(assessment, 'input_vat') == compliance.NA
    assert assessment.recovery_blockers == []


# --- The six-month window ---------------------------------------------------

def test_claim_window_counts_calendar_months_from_the_receipt_date():
    assessment = assess(build_receipt(receipt_date=date(2026, 7, 1)))
    assert assessment.claim_deadline == date(2027, 1, 1)
    assert assessment.claim_days_left == (date(2027, 1, 1) - TODAY).days
    assert status_of(assessment, 'claim_window') == compliance.PASS


def test_claim_window_clamps_to_the_end_of_a_shorter_month():
    """31 August plus six months is 28 February, not 31 February."""
    assessment = assess(build_receipt(receipt_date=date(2026, 8, 31)))
    assert assessment.claim_deadline == date(2027, 2, 28)


def test_a_closing_window_is_a_warning():
    assessment = assess(build_receipt(receipt_date=date(2026, 2, 1)))
    assert status_of(assessment, 'claim_window') == compliance.WARN
    assert 0 <= assessment.claim_days_left <= compliance.CLAIM_WINDOW_WARNING_DAYS


def test_an_expired_window_loses_the_input_vat():
    assessment = assess(build_receipt(receipt_date=date(2025, 1, 5)))
    assert status_of(assessment, 'claim_window') == compliance.FAIL
    assert assessment.claim_days_left < 0
    assert assessment.recoverable_vat_cents == 0
    assert 'the six-month claim window has closed' in assessment.recovery_blockers


# --- Receipts that are not money -------------------------------------------

@pytest.mark.parametrize('flag', ['is_cancelled', 'is_test'])
def test_cancelled_and_test_receipts_score_zero_and_claim_nothing(flag):
    assessment = assess(build_receipt(**{flag: True}))
    assert status_of(assessment, 'validity') == compliance.FAIL
    assert assessment.score == 0
    assert assessment.recoverable_vat_cents == 0


# --- The score --------------------------------------------------------------

def test_a_perfect_receipt_scores_100():
    assert assess(build_receipt()).score == 100


def test_the_score_falls_with_each_defect():
    clean = assess(build_receipt()).score
    no_buyer = assess(build_receipt(customer_id=None, customer_id_type=None)).score
    also_no_tin = assess(build_receipt(customer_id=None, customer_id_type=None, vendor_tin=None)).score
    assert clean > no_buyer > also_no_tin


def test_checks_that_do_not_apply_are_left_out_of_the_score():
    """
    A cash receipt with no VAT on it is not a worse receipt for having no VRN.

    If inapplicable checks scored zero instead of being dropped, every exempt receipt
    would be marked down for a rule that does not apply to it.
    """
    exempt = assess(build_receipt(
        items=[('BREAD', 100_00, 'EX')], tax_lines=[('EX', 0, 0)], vrn=None,
        total_incl_tax_cents=100_00, total_excl_tax_cents=100_00, total_tax_cents=0,
    ))
    assert exempt.score == 100


def test_a_photographed_receipt_scores_below_a_verified_one():
    """It has not been confirmed against the portal, and says so."""
    photo = assess(build_receipt(extraction_source='llm_vision'))
    assert status_of(photo, 'verification') == compliance.WARN
    assert photo.score < 100


def test_a_receipt_with_no_verification_code_fails_verification():
    assessment = assess(build_receipt(receipt_verification_code=None))
    assert status_of(assessment, 'verification') == compliance.FAIL


# --- Line-item judgments ----------------------------------------------------

def test_withholding_is_estimated_on_the_amount_before_vat():
    """
    Rent of 3,540,000 inclusive is 3,000,000 of fee, and 10% of that is 300,000.

    Withholding is computed on the fee, not on the VAT-inclusive total, which is the
    mistake that over-withholds by the VAT rate.
    """
    assessment = assess(build_receipt(
        items=[('OFFICE RENT MARCH', 3_540_000_00, 'A')],
        tax_lines=[('A', 18, 540_000_00)],
        total_incl_tax_cents=3_540_000_00, total_excl_tax_cents=3_000_000_00,
        total_tax_cents=540_000_00,
    ))
    assert assessment.wht_total_cents == 300_000_00
    assert assessment.wht_lines[0]['wht_class'] == 'rent'
    assert status_of(assessment, 'wht') == compliance.WARN


def test_capital_items_are_flagged_against_the_line_not_the_receipt():
    assessment = assess(build_receipt(
        items=[('HP LAPTOP PROBOOK', 3_500_000_00, 'A'), ('A4 PAPER REAM', 40_000_00, 'A')],
        tax_lines=[('A', 18, 540_000_00)],
        total_incl_tax_cents=3_540_000_00, total_excl_tax_cents=3_000_000_00,
        total_tax_cents=540_000_00,
    ))
    assert [item['line_number'] for item in assessment.capital_items] == [1]
    assert status_of(assessment, 'capital') == compliance.WARN


def test_restricted_expenditure_is_flagged():
    assessment = assess(build_receipt(items=[('SERENGETI BEER 500ML', 118_00, 'A')]))
    assert [entry['flag'] for entry in assessment.restrictions] == ['entertainment']


def test_weekend_purchases_are_noted_without_being_penalised():
    assessment = assess(build_receipt(receipt_date=date(2026, 7, 5)))  # a Sunday
    timing = assessment.check('timing')
    assert timing.status == compliance.INFO and timing.weight == 0
    assert assessment.score == 100


def test_out_of_hours_purchases_are_noted():
    assessment = assess(build_receipt(receipt_time=time(23, 40)))
    assert assessment.check('timing').status == compliance.INFO


# --- Shape ------------------------------------------------------------------

def test_assessment_serialises_to_json_safe_values():
    import json

    payload = assess(build_receipt()).as_dict()
    json.dumps(payload)  # raises if a Decimal or date slipped through
    assert payload['score'] == 100
    assert payload['claim_deadline'] == '2027-01-01'
    assert {'id', 'label', 'status', 'detail'} == set(payload['checks'][0])
