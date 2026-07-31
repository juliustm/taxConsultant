# utils/tra_parser.py
"""
Deterministic parser for the TRA verified-receipt page.

The page is fully structured HTML, so the facts on it are read, not guessed. Every
field the portal prints is a `<b>LABEL:</b><span>value</span>` pair, a row in the
purchased-items table, or a row in the totals table; all three are extracted here.

Why this exists: the receipt's facts used to be recovered by flattening the page to
text and asking an LLM to transcribe it. That put a probabilistic step between an
exact source and the ledger - dates, TINs and amounts were free to drift, and the
item lines never came through at all. The LLM is now only asked for judgment
(category, deductibility), and is fed the output of this module.

Two markers on the page decide whether a receipt is money at all:

  * `.cancelled-watermark` - the receipt was cancelled. It is not an expense.
  * `.test-watermark`      - printed by an EFD in test mode. Never an expense.

Both are rendered as an element whose text comes from a CSS ::before rule, so the
class is the signal; the raw markup after the matching HTML comment is checked as
well in case the portal changes how it draws them.
"""
import re
from dataclasses import dataclass, field
from datetime import date, datetime, time
from decimal import Decimal, InvalidOperation

from bs4 import BeautifulSoup

# Present on a genuine receipt page and on nothing else the portal serves.
RECEIPT_MARKER = 'RECEIPT VERIFICATION CODE'

# Tax codes TRA prints, in the order they appear in the totals table. A/B/C are the
# rated classes, SR is special rate, EX is exempt.
TAX_CODES = ('A', 'B', 'C', 'SR', 'EX')

_TAX_RATE_RE = re.compile(r'TAX RATE\s+([A-Z]{1,2})\s*\(\s*([\d.]+)\s*%\s*\)')
_MONEY_RE = re.compile(r'^-?[\d,]*\.?\d+$')
_NULL_VALUES = {'', 'n/a', 'na', '-', 'null', 'none'}

# What TRA prints in the VRN field for a supplier that is not registered for VAT. It
# is a sentence, not a number, so it has to be recognised as an absent VRN - stored
# as-is it is a truthy string and every "is this supplier VAT registered?" check in
# the codebase (badges, input-VAT recoverability, compliance scoring) answers yes.
_VRN_NULL_VALUES = _NULL_VALUES | {
    'not registered', 'notregistered', 'not-registered',
    'not registered for vat', 'unregistered', 'not applicable', 'nil',
}


class TraParseError(Exception):
    """
    The page did not contain the fields a receipt must have.

    This means TRA changed the markup (or we were handed something that is not a
    receipt). It is not retryable: the same HTML parses the same way every time, so
    the caller should fail the submission and keep the HTML for inspection rather
    than fall back to guessing the numbers.
    """


@dataclass(frozen=True)
class ParsedItem:
    line_number: int
    description: str
    quantity: Decimal = None
    amount: Decimal = None
    tax_code: str = None

    def as_dict(self):
        return {
            'line_number': self.line_number,
            'description': self.description,
            'quantity': _number_str(self.quantity),
            'amount': _money_str(self.amount),
            'tax_code': self.tax_code,
        }


@dataclass(frozen=True)
class ParsedTaxLine:
    code: str
    rate: Decimal = None
    amount: Decimal = None

    def as_dict(self):
        return {
            'code': self.code,
            'rate': _number_str(self.rate),
            'amount': _money_str(self.amount),
        }


@dataclass
class ParsedReceipt:
    """Everything the verified page states, with nothing inferred."""
    # --- Vendor ---
    vendor_name: str = None
    vendor_tin: str = None
    vrn: str = None
    vendor_phone: str = None
    vendor_po_box: str = None
    efd_serial: str = None
    uin: str = None
    tax_office: str = None
    # --- Customer ---
    customer_name: str = None
    customer_id_type: str = None
    customer_id: str = None
    customer_mobile: str = None
    # --- Receipt identity ---
    receipt_number: str = None
    z_number: str = None
    receipt_date: date = None
    receipt_time: time = None
    verification_code: str = None
    # --- Money ---
    items: list = field(default_factory=list)
    tax_lines: list = field(default_factory=list)
    discount: Decimal = None
    total_excl_tax: Decimal = None
    total_tax: Decimal = None
    total_incl_tax: Decimal = None
    # --- Validity ---
    is_cancelled: bool = False
    is_test: bool = False

    @property
    def receipt_datetime(self):
        if self.receipt_date is None:
            return None
        return datetime.combine(self.receipt_date, self.receipt_time or time.min)

    @property
    def is_expense(self):
        """A cancelled or test receipt records no spending."""
        return not (self.is_cancelled or self.is_test)

    def as_dict(self):
        """JSON-safe view of the whole receipt. Money is a string, never a float."""
        stamp = self.receipt_datetime
        return {
            'vendor_name': self.vendor_name,
            'vendor_tin': self.vendor_tin,
            'vrn': self.vrn,
            'vendor_phone': self.vendor_phone,
            'vendor_po_box': self.vendor_po_box,
            'efd_serial': self.efd_serial,
            'uin': self.uin,
            'tax_office': self.tax_office,
            'customer_name': self.customer_name,
            'customer_id_type': self.customer_id_type,
            'customer_id': self.customer_id,
            'customer_mobile': self.customer_mobile,
            'receipt_number': self.receipt_number,
            'z_number': self.z_number,
            'receipt_date': self.receipt_date.isoformat() if self.receipt_date else None,
            'receipt_time': self.receipt_time.isoformat() if self.receipt_time else None,
            'receipt_datetime': stamp.strftime('%Y-%m-%d %H:%M:%S') if stamp else None,
            'items': [item.as_dict() for item in self.items],
            'tax_lines': [line.as_dict() for line in self.tax_lines],
            'discount': _money_str(self.discount),
            'total_excl_tax': _money_str(self.total_excl_tax),
            'total_tax': _money_str(self.total_tax),
            'total_incl_tax': _money_str(self.total_incl_tax),
            'verification_code': self.verification_code,
            'is_cancelled': self.is_cancelled,
            'is_test': self.is_test,
        }

    def as_llm_facts(self):
        """
        The subset an LLM needs to categorise the purchase and comment on it.

        Deliberately narrow: identifiers it cannot reason about (UIN, EFD serial, Z
        number, customer details) are left out, because every token of those is a
        token spent on something the model must not restate as fact anyway.
        """
        return {
            'vendor_name': self.vendor_name,
            'vendor_tin': self.vendor_tin,
            'vrn': self.vrn,
            'vendor_is_vat_registered': bool(self.vrn),
            'tax_office': self.tax_office,
            'receipt_date': self.receipt_date.isoformat() if self.receipt_date else None,
            'items': [
                {
                    'description': item.description,
                    'quantity': _number_str(item.quantity),
                    'amount': _money_str(item.amount),
                    'tax_code': item.tax_code,
                }
                for item in self.items
            ],
            'tax_lines': [line.as_dict() for line in self.tax_lines],
            'total_excl_tax': _money_str(self.total_excl_tax),
            'total_tax': _money_str(self.total_tax),
            'total_incl_tax': _money_str(self.total_incl_tax),
            'is_cancelled': self.is_cancelled,
            'is_test': self.is_test,
        }


def parse_receipt_html(html: str) -> ParsedReceipt:
    """
    Turns a verified-receipt page into a ParsedReceipt.

    Raises TraParseError if the page is not a receipt, or is missing the fields that
    make one usable (verification code and total).
    """
    if not html or RECEIPT_MARKER not in html.upper():
        raise TraParseError('Page does not contain a verified receipt.')

    soup = BeautifulSoup(html, 'lxml')
    section = soup.select_one('section.invoice') or soup

    labels = _label_values(section)
    items = _parse_items(section)
    totals, tax_lines = _parse_totals(section)

    receipt_date = _parse_date(labels.get('RECEIPT DATE'))
    receipt_time = _parse_time(labels.get('RECEIPT TIME'))

    parsed = ParsedReceipt(
        vendor_name=_vendor_name(section),
        vendor_tin=labels.get('TIN'),
        vrn=normalise_vrn(labels.get('VRN')),
        vendor_phone=labels.get('MOBILE'),
        vendor_po_box=labels.get('P.O BOX') or labels.get('P.O. BOX'),
        efd_serial=labels.get('SERIAL NO'),
        uin=labels.get('UIN'),
        tax_office=labels.get('TAX OFFICE'),
        customer_name=labels.get('CUSTOMER NAME'),
        customer_id_type=labels.get('CUSTOMER ID TYPE'),
        customer_id=labels.get('CUSTOMER ID'),
        customer_mobile=labels.get('CUSTOMER MOBILE'),
        receipt_number=labels.get('RECEIPT NO'),
        z_number=labels.get('Z NUMBER'),
        receipt_date=receipt_date,
        receipt_time=receipt_time,
        verification_code=_verification_code(section),
        items=items,
        tax_lines=tax_lines,
        discount=totals.get('discount'),
        total_excl_tax=totals.get('total_excl_tax'),
        total_tax=totals.get('total_tax'),
        total_incl_tax=totals.get('total_incl_tax'),
        is_cancelled=_has_watermark(soup, html, 'cancelled-watermark', 'Cancelled Watermark'),
        is_test=_has_watermark(soup, html, 'test-watermark', 'TEST Watermark'),
    )

    missing = [
        name for name, value in (
            ('verification code', parsed.verification_code),
            ('total including tax', parsed.total_incl_tax),
        ) if value is None
    ]
    if missing:
        raise TraParseError(f"Receipt page is missing: {', '.join(missing)}.")

    return parsed


# --- Field extraction -------------------------------------------------------

def _label_values(section) -> dict:
    """
    Collects every `<b>LABEL:</b><span>value</span>` pair on the page.

    Two shapes occur. Usually the value is the `<span>` that follows the `<b>`; in
    the P.O. Box line the span is nested inside the `<b>` instead. Both are handled,
    and the first occurrence of a label wins, so a repeated label lower down the page
    cannot overwrite the vendor block.
    """
    values = {}
    for bold in section.find_all('b'):
        nested = bold.find('span')
        if nested is not None:
            label = bold.get_text().replace(nested.get_text(), '', 1)
            raw_value = nested.get_text()
        else:
            sibling = bold.find_next_sibling()
            if sibling is None or sibling.name != 'span':
                continue
            label = bold.get_text()
            raw_value = sibling.get_text()

        label = _squash(label).rstrip(':').strip().upper()
        if label and label not in values:
            values[label] = _clean(raw_value)
    return values


def _vendor_name(section):
    """The trading name, printed as the first `<h4><b>` of the invoice header."""
    header = section.select_one('.invoice-header')
    if header is None:
        return None
    bold = header.select_one('h4 b')
    return _clean(bold.get_text()) if bold else None


def _verification_code(section):
    """
    The code under the 'RECEIPT VERIFICATION CODE' heading.

    Falls back to the QR image's title attribute, which carries the same code, so a
    change to the heading block alone cannot cost us the receipt's primary key.
    """
    for bold in section.find_all('b'):
        if _squash(bold.get_text()).upper() != RECEIPT_MARKER:
            continue
        block = bold.find_parent('div') or section
        for heading in block.find_all('h4'):
            text = _squash(heading.get_text())
            if text and text.upper() != RECEIPT_MARKER:
                return text

    barcode = section.select_one('img#barcode[title]')
    return _clean(barcode['title']) if barcode else None


def _parse_items(section):
    """
    Reads the purchased-items table.

    Columns are located by their header text rather than by position, so TRA adding
    or reordering a column does not silently shift amounts into the wrong field.
    """
    table = section.select_one('table.table-striped')
    if table is None:
        return []

    columns = {}
    header_row = table.select_one('thead tr')
    if header_row:
        for index, cell in enumerate(header_row.find_all(['th', 'td'])):
            columns[_squash(cell.get_text()).upper()] = index

    body = table.find('tbody') or table
    items = []
    for row in body.find_all('tr'):
        cells = row.find_all('td')
        if not cells:
            continue

        def cell_text(name, fallback_index):
            index = columns.get(name, fallback_index)
            return _clean(cells[index].get_text()) if index < len(cells) else None

        description = cell_text('DESCRIPTION', 0)
        if description is None:
            continue

        items.append(ParsedItem(
            line_number=len(items) + 1,
            description=description,
            quantity=_parse_money(cell_text('QTY', 1)),
            amount=_parse_money(cell_text('AMOUNT', 2)),
            tax_code=cell_text('TYPE', 3),
        ))
    return items


def _parse_totals(section):
    """
    Reads the totals table into named totals plus one entry per printed tax rate.

    Rows are matched on their label, so the rows TRA only sometimes prints (discount,
    each of TAX RATE A/B/C/SR/EX, the TANESCO block) are picked up when present and
    cost nothing when absent. Unrecognised rows are ignored rather than guessed at.
    """
    totals = {}
    tax_lines = []

    for table in section.select('div.table table, table'):
        if 'table-striped' in (table.get('class') or []):
            continue  # the items table, already handled

        for row in table.find_all('tr'):
            label_cell = row.find('th')
            value_cell = row.find('td')
            if label_cell is None or value_cell is None:
                continue

            label = _squash(label_cell.get_text()).upper()
            amount = _parse_money(_clean(value_cell.get_text()))

            rate_match = _TAX_RATE_RE.search(label)
            if rate_match:
                code, rate = rate_match.groups()
                tax_lines.append(ParsedTaxLine(code=code, rate=_parse_money(rate), amount=amount))
                continue

            if label.startswith('TOTAL EXCL'):
                totals.setdefault('total_excl_tax', amount)
            elif label.startswith('TOTAL TAX'):
                totals.setdefault('total_tax', amount)
            elif label.startswith('TOTAL INCL'):
                totals.setdefault('total_incl_tax', amount)
            elif label.startswith('DISCOUNT'):
                totals.setdefault('discount', amount)

    return totals, tax_lines


def _has_watermark(soup, html: str, css_class: str, comment: str) -> bool:
    """
    True when the page carries the named watermark.

    Primary signal is the element itself; the class only ever appears as an element
    when the watermark applies (the stylesheet that defines it lives in a <style>
    block, which holds no elements). As a backstop, the markup between the watermark's
    HTML comment and the next comment is checked for content, which catches the
    portal switching to a differently-classed element.
    """
    if soup.select_one(f'.{css_class}') is not None:
        return True

    marker = f'<!-- {comment} -->'
    start = html.find(marker)
    if start == -1:
        return False

    start += len(marker)
    end = html.find('<!--', start)
    region = html[start:end if end != -1 else len(html)]
    return bool(_squash(re.sub(r'<[^>]+>', ' ', region)))


# --- Value helpers ----------------------------------------------------------

def _squash(text) -> str:
    """Collapses all whitespace (including the page's tabs and newlines) to spaces."""
    return ' '.join((text or '').split())


def _clean(text):
    """Squashes whitespace and maps the portal's placeholders ('n/a') to None."""
    value = _squash(text)
    return None if value.lower() in _NULL_VALUES else value


def normalise_vrn(text):
    """
    A VRN, or None when the value stands for 'this supplier has no VRN'.

    Public because the VRN does not only arrive through this parser - the photo path
    gets it from the LLM, which transcribes the same placeholder off the paper - and
    both routes have to agree on what counts as registered.
    """
    value = _squash(text)
    return None if value.lower().strip('.') in _VRN_NULL_VALUES else value


def _parse_money(text):
    """'24,273,000.00' -> Decimal('24273000.00'). Returns None for anything else."""
    value = _squash(text)
    if not value:
        return None

    negative = value.startswith('(') and value.endswith(')')
    value = value.strip('()').replace(' ', '')
    if not _MONEY_RE.match(value):
        return None

    try:
        amount = Decimal(value.replace(',', ''))
    except InvalidOperation:
        return None
    return -amount if negative else amount


def _parse_date(text):
    """Receipt dates are printed DD/MM/YYYY."""
    value = _squash(text)
    for fmt in ('%d/%m/%Y', '%d-%m-%Y', '%Y-%m-%d'):
        try:
            return datetime.strptime(value, fmt).date()
        except ValueError:
            continue
    return None


def _parse_time(text):
    value = _squash(text)
    for fmt in ('%H:%M:%S', '%H:%M'):
        try:
            return datetime.strptime(value, fmt).time()
        except ValueError:
            continue
    return None


def _money_str(amount):
    return None if amount is None else f'{amount:.2f}'


def _number_str(amount):
    """Renders a quantity or rate without trailing zeros: 1, 2.5, 18."""
    if amount is None:
        return None
    normalised = amount.normalize()
    if normalised == normalised.to_integral_value():
        normalised = normalised.to_integral_value()
    return format(normalised, 'f')
