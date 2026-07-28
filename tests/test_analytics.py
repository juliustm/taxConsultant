# tests/test_analytics.py
"""
The findings that only appear once there are several receipts.

The risk in this module is not a crash - it is a confident percentage drawn through
two data points, presented to somebody who then renegotiates a supply contract on it.
So most of what is asserted here is restraint: that nothing is reported until there is
enough history to mean anything.
"""
from datetime import date, datetime
from decimal import Decimal

import pytest

from models.user import Receipt, ReceiptItem, Submission
from utils import analytics, geo


def receipt(receipt_id, vendor='PLASCO LIMITED', tin='100147181', total=118_00,
            when=None, items=(), category='fuel', location=None, retries=0,
            processed=None, **overrides):
    """A receipt detached from any database, which is all analytics needs."""
    submission = Submission(
        input_type='url', input_data='x', status='completed',
        location=location, retry_count=retries,
        received_at=overrides.pop('received_at', datetime(2026, 5, 1, 8, 0)),
    )
    built = Receipt(
        id=receipt_id, vendor_name=vendor, vendor_tin=tin, vendor_id=overrides.pop('vendor_id', None),
        receipt_date=when or date(2026, 5, 1), total_incl_tax_cents=total,
        category=category, processed_at=processed or datetime(2026, 5, 1, 8, 0),
        is_cancelled=overrides.pop('is_cancelled', False),
        is_test=overrides.pop('is_test', False), **overrides,
    )
    for index, (description, quantity, amount) in enumerate(items, start=1):
        built.items.append(ReceiptItem(
            line_number=index, description=description,
            quantity=Decimal(str(quantity)) if quantity is not None else None,
            amount_cents=amount, tax_code='A',
        ))
    built.submission = submission
    return built


# --- Unit price movements ---------------------------------------------------

def test_a_price_rise_is_reported_with_the_history_behind_it():
    receipts = [
        receipt(1, when=date(2026, 3, 1), items=[('DIESEL AGO', 10, 30_000_00)]),
        receipt(2, when=date(2026, 4, 1), items=[('DIESEL AGO', 10, 30_000_00)]),
        receipt(3, when=date(2026, 5, 1), items=[('DIESEL AGO', 10, 36_000_00)]),
    ]

    found = analytics.unit_price_movements(receipts)

    assert len(found) == 1
    assert found[0]['change_pct'] == 20.0
    assert found[0]['baseline_cents'] == 3_000_00
    assert found[0]['latest_cents'] == 3_600_00
    assert found[0]['observations'] == 2


def test_no_trend_is_drawn_through_a_single_earlier_price():
    """
    Two points are a line, not a trend.

    One prior purchase says nothing about what this supplier normally charges, and a
    percentage printed next to it would be believed anyway.
    """
    receipts = [
        receipt(1, when=date(2026, 4, 1), items=[('DIESEL AGO', 10, 30_000_00)]),
        receipt(2, when=date(2026, 5, 1), items=[('DIESEL AGO', 10, 60_000_00)]),
    ]

    assert analytics.unit_price_movements(receipts) == []


def test_small_movements_are_not_reported():
    """EFD rounding and a shilling here or there are not a price change."""
    receipts = [
        receipt(index, when=date(2026, 3 + index, 1), items=[('DIESEL AGO', 10, 30_000_00)])
        for index in range(1, 3)
    ]
    receipts.append(receipt(9, when=date(2026, 6, 1), items=[('DIESEL AGO', 10, 30_500_00)]))

    assert analytics.unit_price_movements(receipts) == []


def test_lines_without_a_quantity_yield_no_unit_price():
    """
    'SUMMARIZED SALE' with no quantity is most of what Tanzanian EFDs print.

    A unit price cannot be divided out of it, and inventing one by treating the line
    as a single unit would produce a price series that tracks basket size.
    """
    receipts = [
        receipt(index, when=date(2026, index, 1), items=[('SUMMARIZED SALE', None, 30_000_00)])
        for index in range(1, 5)
    ]

    assert analytics.unit_price_movements(receipts) == []


def test_prices_are_tracked_per_supplier_not_across_them():
    """One supplier raising its price is not another supplier's price history."""
    receipts = [
        receipt(1, vendor='A LTD', tin='1', when=date(2026, 3, 1), items=[('DIESEL AGO', 10, 30_000_00)]),
        receipt(2, vendor='A LTD', tin='1', when=date(2026, 4, 1), items=[('DIESEL AGO', 10, 30_000_00)]),
        receipt(3, vendor='B LTD', tin='2', when=date(2026, 5, 1), items=[('DIESEL AGO', 10, 60_000_00)]),
    ]

    assert analytics.unit_price_movements(receipts) == []


def test_item_descriptions_are_matched_past_punctuation_and_case():
    receipts = [
        receipt(1, when=date(2026, 3, 1), items=[('Diesel (AGO)', 10, 30_000_00)]),
        receipt(2, when=date(2026, 4, 1), items=[('DIESEL  AGO', 10, 30_000_00)]),
        receipt(3, when=date(2026, 5, 1), items=[('diesel ago', 10, 45_000_00)]),
    ]

    assert len(analytics.unit_price_movements(receipts)) == 1


# --- Cheaper elsewhere ------------------------------------------------------

def test_the_same_item_cheaper_at_another_supplier_is_reported():
    receipts = [
        receipt(1, vendor='EXPENSIVE LTD', tin='1', when=date(2026, 5, 2),
                items=[('A4 PAPER REAM', 10, 100_000_00)]),
        receipt(2, vendor='CHEAP LTD', tin='2', when=date(2026, 5, 1),
                items=[('A4 PAPER REAM', 10, 80_000_00)]),
    ]

    found = analytics.cheaper_elsewhere(receipts)

    assert len(found) == 1
    assert found[0]['current_vendor'] == 'EXPENSIVE LTD'
    assert found[0]['cheapest_vendor'] == 'CHEAP LTD'
    assert found[0]['saving_pct'] == 20.0


def test_nothing_is_reported_when_the_latest_supplier_is_already_cheapest():
    receipts = [
        receipt(1, vendor='CHEAP LTD', tin='1', when=date(2026, 5, 2),
                items=[('A4 PAPER REAM', 10, 80_000_00)]),
        receipt(2, vendor='EXPENSIVE LTD', tin='2', when=date(2026, 5, 1),
                items=[('A4 PAPER REAM', 10, 100_000_00)]),
    ]

    assert analytics.cheaper_elsewhere(receipts) == []


def test_an_item_bought_from_one_supplier_says_nothing_about_alternatives():
    receipts = [
        receipt(1, when=date(2026, 5, 1), items=[('A4 PAPER REAM', 10, 100_000_00)]),
        receipt(2, when=date(2026, 5, 2), items=[('A4 PAPER REAM', 10, 100_000_00)]),
    ]

    assert analytics.cheaper_elsewhere(receipts) == []


# --- Spend anomalies --------------------------------------------------------

def test_an_unusually_large_receipt_is_flagged_within_its_category():
    receipts = [receipt(index, total=100_00, category='fuel') for index in range(1, 9)]
    receipts.append(receipt(99, total=5_000_00, category='fuel'))

    found = analytics.spend_anomalies(receipts)

    assert [finding['receipt_id'] for finding in found] == [99]
    assert found[0]['compared_with'] == 9


def test_categories_are_measured_separately():
    """
    Rent is not an unusually large airtime purchase.

    Measured across a whole ledger, every rent receipt is an outlier and the finding
    is worthless.
    """
    receipts = [receipt(index, total=100_00, category='telecom') for index in range(1, 9)]
    receipts += [receipt(100 + index, total=5_000_00, category='rent') for index in range(1, 9)]

    assert analytics.spend_anomalies(receipts) == []


def test_no_anomaly_is_called_without_enough_history():
    receipts = [receipt(1, total=100_00), receipt(2, total=100_00), receipt(3, total=9_000_00)]

    assert analytics.spend_anomalies(receipts) == []


def test_cancelled_receipts_are_not_analysed():
    receipts = [receipt(index, total=100_00) for index in range(1, 9)]
    receipts.append(receipt(99, total=5_000_00, is_cancelled=True))

    assert analytics.spend_anomalies(receipts) == []


# --- Vendor upload behaviour ------------------------------------------------

def test_a_supplier_who_uploads_late_is_reported_with_the_wait_observed():
    """
    A retry proves the receipt was not on the portal when first checked.

    The wait is measured from that first attempt to the successful one, which is a
    lower bound on how long the supplier took - the receipt may have appeared at any
    point in between, and the finding is worded that way.
    """
    receipts = [
        receipt(1, retries=0),
        receipt(2, retries=3,
                received_at=datetime(2026, 5, 1, 8, 0), processed=datetime(2026, 5, 1, 14, 0)),
    ]

    found = analytics.vendor_upload_behaviour(receipts)

    assert len(found) == 1
    assert found[0]['receipts'] == 2 and found[0]['late'] == 1
    assert found[0]['late_pct'] == 50.0
    assert found[0]['median_wait_hours'] == 6.0


def test_a_supplier_whose_receipts_are_always_there_is_not_reported():
    assert analytics.vendor_upload_behaviour([receipt(1), receipt(2)]) == []


# --- Location outliers ------------------------------------------------------

DAR = '-6.7924,39.2083'
MWANZA = '-2.5164,32.9175'


def test_a_receipt_collected_far_from_a_suppliers_usual_place_is_flagged():
    receipts = [
        receipt(1, location=DAR), receipt(2, location=DAR), receipt(3, location=DAR),
        receipt(4, location=MWANZA),
    ]

    found = analytics.location_outliers(receipts)

    assert [finding['receipt_id'] for finding in found] == [4]
    assert found[0]['region'] == 'Mwanza'
    assert found[0]['usual_region'] == 'Dar es Salaam'
    assert found[0]['distance_km'] > 500


def test_one_distant_receipt_does_not_make_the_others_look_wrong():
    """
    The odd receipt must not contaminate the baseline the others are judged against.

    Averaging the positions would pull the supplier's "usual place" a third of the way
    to Mwanza, and all four receipts would then be reported as outliers. Taking the
    median leaves it in Dar es Salaam, where the money was actually spent.
    """
    receipts = [receipt(index, location=DAR) for index in range(1, 5)]
    receipts.append(receipt(9, location=MWANZA))

    assert [finding['receipt_id'] for finding in analytics.location_outliers(receipts)] == [9]


def test_a_supplier_with_too_few_located_receipts_has_no_usual_place():
    """
    Three receipts cannot establish where a supplier's receipts normally come from.

    Judging one against the other two compares it to the midpoint of a pair, which a
    single distant receipt moves half way - so with two Dar receipts and one from
    Mwanza, all three would be called outliers. Below four, nothing is said at all.
    """
    receipts = [receipt(1, location=DAR), receipt(2, location=DAR), receipt(3, location=MWANZA)]

    assert analytics.location_outliers(receipts) == []


def test_receipts_without_a_location_are_simply_not_considered():
    receipts = [receipt(1, location=DAR), receipt(2, location=DAR), receipt(3), receipt(4)]

    assert analytics.location_outliers(receipts) == []


# --- Breakdowns -------------------------------------------------------------

def test_category_breakdown_shares_add_up():
    receipts = [
        receipt(1, category='fuel', total=750_00),
        receipt(2, category='telecom', total=250_00),
    ]

    breakdown = analytics.category_breakdown(receipts)

    assert [entry['category'] for entry in breakdown] == ['fuel', 'telecom']
    assert [entry['share_pct'] for entry in breakdown] == [75.0, 25.0]


def test_region_breakdown_keeps_unlocated_receipts_visible_and_last():
    """
    Dropping them would make the shares describe a different, smaller ledger.

    'Unknown' is an absence of data rather than a place, so it sorts last however
    much money is in it.
    """
    receipts = [
        receipt(1, location=DAR, total=100_00),
        receipt(2, total=900_00),
    ]

    breakdown = analytics.region_breakdown(receipts)

    assert [entry['region'] for entry in breakdown] == ['Dar es Salaam', 'Unknown']
    assert sum(entry['cents'] for entry in breakdown) == 1_000_00


def test_monthly_totals_run_oldest_first_and_include_empty_months():
    receipts = [receipt(1, when=date(2026, 5, 10), total=500_00)]

    months = analytics.monthly_totals(receipts, months=3, today=date(2026, 5, 20))

    assert [entry['label'] for entry in months] == ['2026-03', '2026-04', '2026-05']
    assert [entry['cents'] for entry in months] == [0, 0, 500_00]


# --- Geography ---------------------------------------------------------------

@pytest.mark.parametrize('raw, expected', [
    ('-6.7924,39.2083', (-6.7924, 39.2083)),
    ('-6.7924, 39.2083', (-6.7924, 39.2083)),
    ('-6.7924 39.2083', (-6.7924, 39.2083)),
    ('-6.7924,39.2083 - Kariakoo, Dar es Salaam', (-6.7924, 39.2083)),
    ('', None),
    (None, None),
    ('somewhere in town', None),
    ('999,999', None),
])
def test_location_strings_are_parsed_leniently_but_validated(raw, expected):
    assert geo.parse_location(raw) == expected


def test_a_street_name_with_a_number_is_not_read_as_a_coordinate():
    """Only the part before the dash is a coordinate; the rest is a label."""
    assert geo.parse_location('-6.7924,39.2083 - 12 Nyerere Road') == (-6.7924, 39.2083)


@pytest.mark.parametrize('point, region', [
    ((-6.7924, 39.2083), 'Dar es Salaam'),
    ((-3.3869, 36.6830), 'Arusha'),
    ((-2.5164, 32.9175), 'Mwanza'),
    ((-6.1650, 39.2026), 'Mjini Magharibi'),
])
def test_known_places_land_in_the_right_region(point, region):
    assert geo.region_for(point) == region


def test_a_point_far_outside_tanzania_belongs_to_no_region():
    """London must not be filed under Kagera because that is the closest centre."""
    assert geo.region_for((51.5074, -0.1278)) is None


def test_distance_between_two_cities_is_about_right():
    """Dar es Salaam to Arusha is roughly 470 km in a straight line."""
    assert 450 < geo.distance_km((-6.7924, 39.2083), (-3.3869, 36.6830)) < 490


# --- Verification reliability -----------------------------------------------

def submission(submission_id, code=None, status='completed', retries=0, input_type='url',
               reason=None, received=None):
    """A submission detached from any database."""
    built = Submission(
        id=submission_id, input_type=input_type, input_data='x', status=status,
        receipt_code=code, retry_count=retries, failure_reason=reason,
        received_at=received or datetime(2026, 5, 1, 8, 0),
    )
    return built


def coded_receipt(receipt_id, code, vendor='PLASCO LIMITED', tin='100147181'):
    built = receipt(receipt_id, vendor=vendor, tin=tin)
    built.receipt_verification_code = code
    return built


def test_a_failure_is_pinned_on_a_vendor_through_the_verification_code():
    """
    A submission that never verified has no vendor - TRA never told us who issued it.

    The code read off the URL at intake closes that gap: if the same receipt reached
    us any other way, the earlier failure belongs to that receipt's vendor.
    """
    receipts = [coded_receipt(1, 'ABC123')]
    submissions = [
        submission(1, code='ABC123', status='failed', reason='TraThrottled'),
        submission(2, code='ABC123', status='completed'),
    ]

    found = analytics.verification_reliability(receipts, submissions)

    assert len(found['vendors']) == 1
    assert found['vendors'][0]['vendor_name'] == 'PLASCO LIMITED'
    assert found['vendors'][0]['gave_up'] == 1
    assert found['vendors'][0]['clean'] == 1
    assert found['vendors'][0]['trouble_pct'] == 50.0
    assert found['unattributed'] == []


def test_a_failure_whose_receipt_never_arrived_is_not_blamed_on_anyone():
    """
    Nothing identifies the issuer of a receipt we have never seen.

    Attributing it to a vendor would be inventing a fact, so it is reported on its own
    - each one is a receipt somebody still has to chase by hand.
    """
    receipts = [coded_receipt(1, 'ABC123')]
    submissions = [
        submission(1, code='ABC123', status='completed'),
        submission(2, code='NEVERSEEN', status='failed', reason='TraReceiptNotUploaded'),
    ]

    found = analytics.verification_reliability(receipts, submissions)

    assert found['vendors'] == []
    assert found['unattributed_total'] == 1
    assert found['unattributed'][0]['receipt_code'] == 'NEVERSEEN'


def test_a_submission_still_working_through_its_retries_has_not_failed_yet():
    """Queued is not a failure, and counting it as one would inflate every rate."""
    found = analytics.verification_reliability([], [submission(1, code='PENDING', status='queued')])

    assert found['unattributed'] == []


def test_retries_count_against_a_vendor_even_when_they_eventually_worked():
    """Needing three attempts is a supplier problem, not a clean result."""
    receipts = [coded_receipt(1, 'ABC123')]
    submissions = [submission(1, code='ABC123', status='completed', retries=3)]

    found = analytics.verification_reliability(receipts, submissions)

    assert found['vendors'][0]['retried'] == 1
    assert found['vendors'][0]['clean'] == 0
    assert found['vendors'][0]['trouble_pct'] == 100.0


def test_a_supplier_whose_receipts_always_verify_first_time_is_not_listed():
    receipts = [coded_receipt(1, 'ABC123')]
    submissions = [submission(1, code='ABC123', status='completed')]

    assert analytics.verification_reliability(receipts, submissions)['vendors'] == []


def test_photographed_submissions_are_left_out_of_the_verification_rate():
    """They never go to TRA at all, so they cannot say anything about its reliability."""
    found = analytics.verification_reliability(
        [], [submission(1, status='failed', input_type='photo', reason='ValueError')],
    )

    assert found['unattributed'] == [] and found['vendors'] == []
