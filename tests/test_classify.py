# tests/test_classify.py
"""
The deterministic reading of a line item.

These are the cases that decide real money - a laptop booked as an expense instead of
an asset, a rent invoice paid without withholding - plus the near-misses that a naive
substring match gets wrong.
"""
import pytest

from utils.classify import (
    CAPITAL_THRESHOLD_CENTS, EXPENSE_CATEGORIES,
    categorise_item, categorise_receipt, deductibility_flags, is_capital_item, wht_class,
)


# --- Categories -------------------------------------------------------------

@pytest.mark.parametrize('description, expected', [
    ('DIESEL AGO', 'fuel'),
    ('Petrol Unleaded 10L', 'fuel'),
    ('MAFUTA YA TAA', 'fuel'),
    ('Tyre 195/65 R15', 'vehicle_running'),
    ('AIRTIME VODACOM', 'telecom'),
    ('Data bundle 10GB', 'telecom'),
    ('LUKU UNITS', 'utilities'),
    ('OFFICE RENT - MARCH', 'rent'),
    ('Audit fees Q1', 'professional_services'),
    ('HP LAPTOP PROBOOK 450', 'capital_asset'),
    ('A4 PAPER REAM', 'office_supplies'),
    ('Hotel accommodation 2 nights', 'accommodation'),
    ('Bank charge - ledger fee', 'bank_charges'),
    ('Motor vehicle insurance premium', 'insurance'),
    ('Billboard advertising', 'marketing'),
])
def test_categorises_common_line_items(description, expected):
    assert categorise_item(description) == expected


@pytest.mark.parametrize('description', ['SUMMARIZED SALE - E', 'ITEM 1', '', None, '   ', '12345'])
def test_uncategorisable_text_returns_none_rather_than_other(description):
    """
    'other' would be a claim to have understood the line. None says we could not.

    Most Tanzanian EFDs print 'SUMMARIZED SALE' instead of the goods, and the
    difference between "we looked and could not tell" and "we never looked" is what
    decides whether it is worth asking a human.
    """
    assert categorise_item(description) is None


def test_matching_is_on_whole_words():
    """Substring matching would make 'ago' (gas oil) match 'Chicago'."""
    assert categorise_item('CHICAGO PIZZA') != 'fuel'
    assert categorise_item('AGO 20 LITRES') == 'fuel'
    assert categorise_item('PAPER CUPS') == 'office_supplies'


def test_specific_rules_beat_general_ones():
    """Diesel for a generator is fuel, not the purchase of a generator."""
    assert categorise_item('GENERATOR DIESEL 200L') == 'fuel'


def test_receipt_category_is_the_most_common_line():
    descriptions = ['DIESEL AGO', 'PETROL', 'A4 PAPER REAM']
    assert categorise_receipt(descriptions) == 'fuel'


def test_receipt_category_ignores_unreadable_lines():
    assert categorise_receipt(['SUMMARIZED SALE', 'AIRTIME VODACOM']) == 'telecom'
    assert categorise_receipt(['SUMMARIZED SALE', 'ITEM']) is None
    assert categorise_receipt([]) is None


def test_every_rule_maps_to_a_known_category():
    """A category invented here would be written to the ledger and match nothing."""
    for description in ('DIESEL', 'LAPTOP', 'OFFICE RENT'):
        assert categorise_item(description) in EXPENSE_CATEGORIES


# --- Capital vs revenue -----------------------------------------------------

def test_expensive_asset_is_capital():
    capital, keyword = is_capital_item('HP LAPTOP PROBOOK', 3_500_000_00)
    assert capital and keyword == 'laptop'


def test_cheap_asset_is_not_worth_capitalising():
    """A 15,000 TZS 'desk' is an office cost whatever the keyword list says."""
    capital, _ = is_capital_item('DESK ORGANISER', 15_000_00)
    assert not capital


def test_consumables_are_not_capitalised_on_a_capital_sounding_word():
    """A printer cartridge is not a printer, however much of them you buy."""
    capital, _ = is_capital_item('PRINTER TONER CARTRIDGE', 2_000_000_00)
    assert not capital


def test_capital_threshold_boundary():
    assert is_capital_item('OFFICE FURNITURE', CAPITAL_THRESHOLD_CENTS)[0]
    assert not is_capital_item('OFFICE FURNITURE', CAPITAL_THRESHOLD_CENTS - 1)[0]


def test_missing_amount_cannot_be_capital():
    assert not is_capital_item('LAPTOP', None)[0]


# --- Withholding tax --------------------------------------------------------

@pytest.mark.parametrize('description, expected_class, expected_rate', [
    ('OFFICE RENT MARCH 2026', 'rent', 10),
    ('CONSULTANCY SERVICES', 'professional_fees', 5),
    ('AUDIT FEE', 'professional_fees', 5),
    ('TRANSPORT AND HAULAGE', 'transport', 5),
    ('SECURITY SERVICES - JAN', 'security', 5),
    ('CLEANING SERVICES', 'cleaning', 5),
])
def test_service_lines_are_flagged_for_withholding(description, expected_class, expected_rate):
    name, rate, _ = wht_class(description)
    assert (name, rate) == (expected_class, expected_rate)


@pytest.mark.parametrize('description', ['DIESEL AGO', 'A4 PAPER REAM', 'BOTTLED WATER', ''])
def test_goods_do_not_attract_withholding(description):
    assert wht_class(description) is None


# --- Restricted expenditure -------------------------------------------------

def test_entertainment_is_flagged():
    flags = dict(deductibility_flags('SERENGETI BEER 500ML'))
    assert 'entertainment' in flags


def test_gifts_and_fines_are_flagged():
    assert 'gift' in dict(deductibility_flags('CHRISTMAS GIFT HAMPER'))
    assert 'fine_penalty' in dict(deductibility_flags('LATE FILING PENALTY'))


def test_ordinary_purchases_raise_no_flags():
    assert deductibility_flags('A4 PAPER REAM') == []
