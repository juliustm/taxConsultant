# tests/test_tra_parser.py
"""
The parser's contract, pinned against a page saved from the live portal.

Every expected value here was read off that page by hand. If TRA changes the markup
these fail, which is the point: the alternative to a failing test is a receipt whose
numbers quietly stop matching the receipt.
"""
from datetime import date, time
from decimal import Decimal

import pytest

from utils.tra_parser import parse_receipt_html, TraParseError


def test_parses_vendor_block(receipt_html):
    receipt = parse_receipt_html(receipt_html)

    assert receipt.vendor_name == 'PLASCO LIMITED'
    assert receipt.vendor_tin == '100147181'
    assert receipt.vrn == '10007206H'
    assert receipt.vendor_phone == '+255 761 850 579'
    assert receipt.vendor_po_box == '19956'
    assert receipt.efd_serial == '03TZ343001520'
    assert receipt.uin == '02ESDINCOTEX118M-11078151210014718103TZ343001520'
    assert receipt.tax_office == 'LARGE TAXPAYERS DEPARTMENT'


def test_parses_customer_block(receipt_html):
    receipt = parse_receipt_html(receipt_html)

    assert receipt.customer_name == 'NZEGA URBAN WATER'
    assert receipt.customer_id_type == 'TIN'
    assert receipt.customer_id == '108537108'
    # Printed as 'n/a' on this receipt, which is an absent value, not the text 'n/a'.
    assert receipt.customer_mobile is None


def test_parses_receipt_identity(receipt_html):
    receipt = parse_receipt_html(receipt_html)

    assert receipt.receipt_number == '514'
    assert receipt.z_number == '169'
    assert receipt.verification_code == '58E41A514'
    # Printed DD/MM/YYYY: 08/03/2022 is 8 March, not 3 August.
    assert receipt.receipt_date == date(2022, 3, 8)
    assert receipt.receipt_time == time(9, 20, 22)
    assert receipt.receipt_datetime.isoformat(sep=' ') == '2022-03-08 09:20:22'


def test_parses_line_items(receipt_html):
    receipt = parse_receipt_html(receipt_html)

    assert len(receipt.items) == 1
    item = receipt.items[0]
    assert item.line_number == 1
    assert item.description == 'SUMMARIZED SALE - E'
    assert item.quantity == Decimal('1')
    assert item.amount == Decimal('24273000.00')
    assert item.tax_code == 'EX'


def test_parses_totals_exactly(receipt_html):
    receipt = parse_receipt_html(receipt_html)

    # Decimal, not float: 24273000.00 must survive being added to other receipts.
    assert receipt.total_excl_tax == Decimal('24273000.00')
    assert receipt.total_tax == Decimal('0.00')
    assert receipt.total_incl_tax == Decimal('24273000.00')
    assert receipt.discount is None
    assert receipt.total_excl_tax + receipt.total_tax == receipt.total_incl_tax


def test_parses_tax_rate_lines(receipt_html):
    receipt = parse_receipt_html(receipt_html)

    assert len(receipt.tax_lines) == 1
    line = receipt.tax_lines[0]
    assert (line.code, line.rate, line.amount) == ('EX', Decimal('0'), Decimal('0.00'))


def test_live_receipt_is_neither_cancelled_nor_test(receipt_html):
    receipt = parse_receipt_html(receipt_html)

    assert receipt.is_cancelled is False
    assert receipt.is_test is False
    assert receipt.is_expense is True


def test_detects_cancelled_watermark(receipt_html):
    """A cancelled receipt is voided money and must not be counted as an expense."""
    cancelled = receipt_html.replace(
        '<!-- Cancelled Watermark -->',
        '<!-- Cancelled Watermark --><div class="cancelled-watermark"></div>',
    )

    receipt = parse_receipt_html(cancelled)

    assert receipt.is_cancelled is True
    assert receipt.is_expense is False


def test_detects_test_watermark(receipt_html):
    tested = receipt_html.replace(
        '<!-- TEST Watermark -->',
        '<!-- TEST Watermark --><div class="test-watermark"></div>',
    )

    receipt = parse_receipt_html(tested)

    assert receipt.is_test is True
    assert receipt.is_expense is False


def test_watermark_css_alone_is_not_a_cancelled_receipt(receipt_html):
    """
    The stylesheet defining .cancelled-watermark is on every page the portal serves.
    Only an element carrying the class means the receipt was cancelled.
    """
    assert '.cancelled-watermark' in receipt_html
    assert parse_receipt_html(receipt_html).is_cancelled is False


def test_detects_watermark_drawn_without_the_class(receipt_html):
    """Backstop: content after the watermark comment counts even if the class changes."""
    cancelled = receipt_html.replace(
        '<!-- Cancelled Watermark -->',
        '<!-- Cancelled Watermark --><div class="voided-stamp">CANCELLED</div>',
    )

    assert parse_receipt_html(cancelled).is_cancelled is True


def test_parses_multiple_items_rates_and_discount(receipt_html):
    """
    A rated receipt: two lines, standard-rated tax alongside the exempt line, and a
    discount row. All three rows only appear on some receipts, so they are exercised
    by rewriting the saved page rather than by fetching a second one.
    """
    # The first </tbody> on the page closes the purchased-items table.
    html = receipt_html.replace(
        '</tbody>',
        '<tr><td>DIESEL</td><td>12.5</td><td align="right">1,200.50</td>'
        '<td><span>A</span></td></tr></tbody>',
        1,
    ).replace(
        '<!-- DISCOUNT -->',
        '<!-- DISCOUNT --><tr><th>DISCOUNT:</th><td align="right">500.00</td></tr>',
    ).replace(
        '<!-- TAX RATE A -->',
        '<!-- TAX RATE A --><tr><th><small>TAX RATE A (18%)</small></th>'
        '<td align="right"><small>183.13</small></td></tr>',
    )

    receipt = parse_receipt_html(html)

    assert [item.description for item in receipt.items] == ['SUMMARIZED SALE - E', 'DIESEL']
    assert receipt.items[1].quantity == Decimal('12.5')
    assert receipt.items[1].amount == Decimal('1200.50')
    assert receipt.items[1].tax_code == 'A'
    assert receipt.items[1].line_number == 2

    assert [(line.code, line.rate, line.amount) for line in receipt.tax_lines] == [
        ('A', Decimal('18'), Decimal('183.13')),
        ('EX', Decimal('0'), Decimal('0.00')),
    ]
    assert receipt.discount == Decimal('500.00')


def test_rejects_a_page_that_is_not_a_receipt():
    with pytest.raises(TraParseError):
        parse_receipt_html('<html><body>Please provide your receipt time</body></html>')


def test_rejects_a_receipt_without_a_verification_code(receipt_html):
    """
    The code is the ledger's unique key and its duplicate check. A page that has lost
    it is a parser bug to fix, not a receipt to store with a blank key.
    """
    broken = receipt_html.replace('58E41A514', 'X', 1)  # heading
    broken = broken.replace('<h4>X</h4>', '<h4></h4>').replace('id="barcode"', 'id="gone"')

    with pytest.raises(TraParseError, match='verification code'):
        parse_receipt_html(broken)


def test_verification_code_falls_back_to_the_qr_image(receipt_html):
    """The QR image carries the same code, so one broken heading is survivable."""
    without_heading = receipt_html.replace(
        '<h4>58E41A514</h4>', '<h4></h4>', 1,
    )

    assert parse_receipt_html(without_heading).verification_code == '58E41A514'


def test_llm_facts_are_a_fraction_of_the_page(receipt_html):
    """
    The judgment layer is fed structured facts, not the scraped page. This pins the
    saving that motivated the change and, more importantly, that no identifier the
    model has no use for is shipped to it.
    """
    import json

    facts = parse_receipt_html(receipt_html).as_llm_facts()

    assert len(json.dumps(facts)) < len(receipt_html) / 10
    assert facts['vendor_tin'] == '100147181'
    assert facts['vendor_is_vat_registered'] is True
    assert facts['total_incl_tax'] == '24273000.00'
    assert 'uin' not in facts
    assert 'customer_id' not in facts


def test_not_registered_in_the_vrn_field_is_an_absent_vrn(receipt_html):
    """
    TRA prints 'VRN: NOT REGISTERED' for a supplier below the registration threshold.

    Kept as text it is a truthy VRN, which is how unregistered suppliers came to be
    labelled 'VAT registered' in the vendor list and scored as though the tax on
    their receipts were recoverable input VAT.
    """
    unregistered = receipt_html.replace('10007206H', 'NOT REGISTERED', 1)
    receipt = parse_receipt_html(unregistered)

    assert receipt.vrn is None
    assert receipt.as_llm_facts()['vendor_is_vat_registered'] is False
