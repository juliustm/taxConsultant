# utils/compliance.py
"""
What a receipt is worth, and whether it will survive an audit.

Everything here is computed from the receipt itself - the fields TRA printed and the
line items it carries - with no model involved and no other receipt needed. The
questions are the ones a tax adviser asks first and the dashboard never asked at all:

  * Is this invoice made out to us? Input VAT is only claimable when it is.
  * Does the tax on it add up? An EFD that prints 18% and charges 15% is a rejected
    claim at best, and evidence of tampering at worst.
  * Is the supplier VAT-registered? Tax charged without a VRN is not input tax.
  * How much of the receipt is actually recoverable, as opposed to exempt?
  * How long is left to claim it?

Each answer is a `Check` with a status, a sentence a human can act on, and a weight.
The compliance score is derived from the checks rather than computed separately, so
the badge and the list can never disagree.

Nothing here decides anything on the taxpayer's behalf. A failed check is a prompt
to look, and the wording says which rule prompted it.
"""
from dataclasses import dataclass, field
from datetime import date, time
from decimal import Decimal, ROUND_HALF_UP

from utils.classify import (
    CAPITAL_THRESHOLD_CENTS, WHT_MONTHLY_THRESHOLD_CENTS,
    categorise_receipt, deductibility_flags, is_capital_item, wht_class,
)

# Check outcomes. 'na' means the check does not apply to this receipt and is left out
# of the score entirely; 'info' is something worth showing that is not a judgment.
PASS = 'pass'
WARN = 'warn'
FAIL = 'fail'
INFO = 'info'
NA = 'na'

# A warning is a half-answered question, and scores accordingly.
_STATUS_CREDIT = {PASS: 1.0, WARN: 0.5, FAIL: 0.0}

# Input tax must be claimed within six months of the date on the tax invoice. Counted
# in calendar months from receipt_date, not in days, because that is how the deadline
# is written.
CLAIM_WINDOW_MONTHS = 6

# Below this many days left, the claim window is reported as urgent rather than open.
CLAIM_WINDOW_WARNING_DAYS = 30

# EFDs round each line to the cent, so a computed total may legitimately differ from
# the printed one by a cent per line. Anything past that is an arithmetic error.
BASE_TOLERANCE_CENTS = 1

# Outside these hours a purchase is worth a second look on an expense claim. Not a
# finding in itself - shops are open late - but it is the pattern personal spending
# on a company card makes.
BUSINESS_HOURS = (time(6, 0), time(21, 0))


@dataclass(frozen=True)
class Check:
    """One question asked of a receipt, and the answer."""
    id: str
    label: str
    status: str
    detail: str
    # Contribution to the compliance score. 0 means the check is reported but not
    # scored - after-hours, for instance, is a flag and not a defect.
    weight: int = 0

    @property
    def scored(self):
        return self.weight > 0 and self.status in _STATUS_CREDIT

    def as_dict(self):
        return {'id': self.id, 'label': self.label, 'status': self.status, 'detail': self.detail}


@dataclass
class Assessment:
    """The full verdict on one receipt."""
    checks: list = field(default_factory=list)
    score: int = None
    # Money, all in cents.
    standard_rated_cents: int = 0
    zero_or_exempt_cents: int = 0
    input_vat_cents: int = 0
    recoverable_vat_cents: int = 0
    # Why the recoverable figure is lower than the input VAT charged, if it is.
    recovery_blockers: list = field(default_factory=list)
    claim_deadline: date = None
    claim_days_left: int = None
    # Deterministic reading of the line items; compare against receipt.category, which
    # is the model's.
    computed_category: str = None
    wht_lines: list = field(default_factory=list)
    wht_total_cents: int = 0
    capital_items: list = field(default_factory=list)
    restrictions: list = field(default_factory=list)

    @property
    def standard_rated_excl_cents(self):
        """
        Standard-rated purchases net of the tax on them.

        A VAT return asks for the value of purchases excluding VAT, while an EFD prints
        the tax-inclusive price against each line, so both figures are carried.
        """
        return self.standard_rated_cents - self.input_vat_cents

    @property
    def is_claimable(self):
        return self.recoverable_vat_cents > 0

    @property
    def failed_checks(self):
        return [check for check in self.checks if check.status == FAIL]

    def check(self, check_id):
        return next((check for check in self.checks if check.id == check_id), None)

    def as_dict(self, detailed=True):
        """
        The JSON view.

        `detailed` carries the full wording of every check, which is what a webhook or
        a single receipt wants. A table of fifty rows does not: it renders a score and
        a handful of failed check ids, and shipping fifty copies of the prose behind
        them is most of the page weight for none of the page.
        """
        payload = {
            'score': self.score,
            'failed': [check.id for check in self.failed_checks],
            'standard_rated_cents': self.standard_rated_cents,
            'standard_rated_excl_cents': self.standard_rated_excl_cents,
            'zero_or_exempt_cents': self.zero_or_exempt_cents,
            'input_vat_cents': self.input_vat_cents,
            'recoverable_vat_cents': self.recoverable_vat_cents,
            'recovery_blockers': list(self.recovery_blockers),
            'claim_deadline': self.claim_deadline.isoformat() if self.claim_deadline else None,
            'claim_days_left': self.claim_days_left,
            'computed_category': self.computed_category,
            'wht_lines': list(self.wht_lines),
            'wht_total_cents': self.wht_total_cents,
            'capital_items': list(self.capital_items),
            'restrictions': list(self.restrictions),
        }
        if detailed:
            payload['checks'] = [check.as_dict() for check in self.checks]
        return payload


def evaluate(receipt, business_tin=None, business_vrn=None, today=None,
             capital_threshold_cents=CAPITAL_THRESHOLD_CENTS):
    """
    Assesses one stored Receipt.

    `business_tin` is the TIN of the instance's own business, without which the single
    most valuable check - is this invoice even addressed to us - cannot be made. It is
    passed in rather than read from the config so this module stays a pure function of
    its arguments and can be tested without a database.
    """
    today = today or date.today()
    assessment = Assessment()

    # A cancelled or test receipt is not money and not a claim. Everything downstream
    # is reported for completeness but nothing on it is recoverable.
    voided = _check_validity(receipt, assessment)

    _check_verification(receipt, assessment)
    _check_receipt_date(receipt, assessment, today)
    _check_vendor_identity(receipt, assessment)
    buyer_ok = _check_buyer_tin(receipt, assessment, business_tin)
    _split_by_rate(receipt, assessment)
    vendor_ok = _check_vendor_vrn(receipt, assessment)
    # Both are arithmetic on the same numbers, and either one failing means the figure
    # a claim would be filed on is not the figure on the receipt.
    sums_ok = _check_totals(receipt, assessment) and _check_tax_arithmetic(receipt, assessment)
    window_ok = _check_claim_window(receipt, assessment, today)
    _check_timing(receipt, assessment)

    _resolve_recovery(assessment, voided=voided, buyer_ok=buyer_ok, vendor_ok=vendor_ok,
                      window_ok=window_ok, sums_ok=sums_ok, business_vrn=business_vrn)

    _read_line_items(receipt, assessment, capital_threshold_cents)
    assessment.score = _score(assessment.checks, voided=voided)
    return assessment


# --- Individual checks ------------------------------------------------------

def _check_validity(receipt, assessment):
    """Cancelled and test receipts, which are not expenses at all. Returns True if so."""
    if getattr(receipt, 'is_cancelled', False):
        assessment.checks.append(Check(
            'validity', 'Receipt status', FAIL,
            'Cancelled by the vendor. It is not an expense, nothing on it is deductible '
            'and no input VAT may be claimed.',
        ))
        return True

    if getattr(receipt, 'is_test', False):
        assessment.checks.append(Check(
            'validity', 'Receipt status', FAIL,
            'Printed by an EFD in test mode. It records no real sale and must stay out '
            'of the accounts.',
        ))
        return True

    assessment.checks.append(Check('validity', 'Receipt status', PASS, 'A live receipt for a real sale.'))
    return False


def _check_verification(receipt, assessment):
    """The verification code is what makes the receipt checkable against TRA at all."""
    # A document that was never an EFD receipt is not a receipt that failed
    # verification, and reporting it as one sends the reader looking for a code that
    # was never printed. It is still a real expense - a parking stub is a cost of doing
    # business - it just carries no recoverable input VAT and no TRA record behind it.
    document_type = getattr(receipt, 'document_type', None)
    if document_type in ('other_receipt', 'not_a_receipt'):
        detail = (
            'This is not a Tanzanian EFD receipt, so there is nothing to verify against '
            'TRA. It can support a deduction if the business purpose is clear, but no '
            'input VAT may be claimed on it.' if document_type == 'other_receipt'
            else 'The photograph does not appear to be a purchase document at all. '
                 'Check it before claiming anything on it.'
        )
        assessment.checks.append(Check(
            'verification', 'TRA verification',
            WARN if document_type == 'other_receipt' else FAIL, detail, weight=20,
        ))
        return

    if receipt.receipt_verification_code:
        source = getattr(receipt, 'extraction_source', None)
        if source == 'tra_html':
            detail = (f'Verification code {receipt.receipt_verification_code}, read from '
                      'the TRA verified page.')
        else:
            # Which of the two unverified readings this is matters to whoever has to
            # chase it: a photograph can be looked at again, a paste cannot be looked
            # at at all beyond the characters somebody sent.
            read_from = 'pasted text' if source == 'llm_text' else 'a photograph'
            detail = (f'Verification code {receipt.receipt_verification_code}, read from '
                      f'{read_from} and not yet confirmed against the portal.')
        assessment.checks.append(Check(
            'verification', 'TRA verification', PASS if source == 'tra_html' else WARN, detail, weight=20,
        ))
        return

    assessment.checks.append(Check(
        'verification', 'TRA verification', FAIL,
        'No verification code, so this receipt cannot be confirmed against TRA. Treat '
        'it as unsupported documentation.', weight=20,
    ))


def _check_receipt_date(receipt, assessment, today):
    """A receipt with no date cannot be put in a period, or in a return."""
    if receipt.receipt_date is None:
        assessment.checks.append(Check(
            'receipt_date', 'Receipt date', FAIL,
            'No date on record, so this receipt cannot be assigned to a tax period.', weight=10,
        ))
        return

    if receipt.receipt_date > today:
        assessment.checks.append(Check(
            'receipt_date', 'Receipt date', WARN,
            f'Dated {receipt.receipt_date.isoformat()}, which is in the future. Check the '
            'EFD clock before filing on it.', weight=10,
        ))
        return

    assessment.checks.append(Check(
        'receipt_date', 'Receipt date', PASS, f'Dated {receipt.receipt_date.isoformat()}.', weight=10,
    ))


def _check_vendor_identity(receipt, assessment):
    """A supplier with no TIN cannot be traced, matched or reconciled."""
    if receipt.vendor_tin:
        assessment.checks.append(Check(
            'vendor_tin', 'Supplier TIN', PASS,
            f'{receipt.vendor_name or "The supplier"} trades under TIN {receipt.vendor_tin}.', weight=15,
        ))
        return

    assessment.checks.append(Check(
        'vendor_tin', 'Supplier TIN', FAIL,
        'The supplier\'s TIN is not on the receipt, so the expense cannot be tied to a '
        'registered taxpayer.', weight=15,
    ))


def _check_buyer_tin(receipt, assessment, business_tin):
    """
    Whether the tax invoice is made out to us.

    This is the check that decides most rejected input-VAT claims: a receipt issued to
    a walk-in customer, or to another TIN, supports no claim however genuine the
    purchase was. Returns True when the receipt is addressed to this business.
    """
    customer_id = (receipt.customer_id or '').strip()
    id_type = (receipt.customer_id_type or '').strip().upper()
    named = (receipt.customer_name or '').strip()

    if not business_tin:
        assessment.checks.append(Check(
            'buyer_tin', 'Issued to you', INFO,
            'Your own TIN is not set, so this receipt cannot be matched to your business. '
            'Add it under Configuration to turn this into a real check.',
        ))
        # Unknown, not wrong: do not block the recoverable figure on a missing setting.
        return True

    if not customer_id:
        assessment.checks.append(Check(
            'buyer_tin', 'Issued to you', FAIL,
            'Issued to a walk-in customer with no TIN on it. Input VAT is not claimable '
            'on a receipt that does not name the buyer.', weight=25,
        ))
        return False

    if _same_tin(customer_id, business_tin):
        assessment.checks.append(Check(
            'buyer_tin', 'Issued to you', PASS,
            f'Made out to your TIN ({business_tin}).', weight=25,
        ))
        return True

    if id_type and id_type != 'TIN':
        assessment.checks.append(Check(
            'buyer_tin', 'Issued to you', WARN,
            f'The buyer is identified by {id_type} ({customer_id}), not by TIN, so it '
            'cannot be matched to your registration.', weight=25,
        ))
        return False

    whose = f' ({named})' if named else ''
    assessment.checks.append(Check(
        'buyer_tin', 'Issued to you', FAIL,
        f'Issued to TIN {customer_id}{whose}, not to yours ({business_tin}). Input VAT on '
        'this receipt belongs to that taxpayer, not to you.', weight=25,
    ))
    return False


def _check_vendor_vrn(receipt, assessment):
    """
    Tax charged by an unregistered supplier is not input tax.

    Returns True when the tax on the receipt, if any, was charged by a registered
    supplier - i.e. when it is capable of being input tax at all.
    """
    charged = assessment.input_vat_cents > 0

    if receipt.vrn:
        assessment.checks.append(Check(
            'vendor_vrn', 'Supplier VAT registration', PASS,
            f'Registered for VAT under VRN {receipt.vrn}.', weight=15,
        ))
        return True

    if charged:
        assessment.checks.append(Check(
            'vendor_vrn', 'Supplier VAT registration', FAIL,
            'Tax was charged but the supplier printed no VRN. An unregistered supplier '
            'may not charge VAT, and this claim would be rejected. Worth querying with '
            'them before paying.', weight=15,
        ))
        return False

    assessment.checks.append(Check(
        'vendor_vrn', 'Supplier VAT registration', NA,
        'No VRN and no tax charged, which is consistent with a supplier below the VAT '
        'registration threshold.',
    ))
    return False


def _split_by_rate(receipt, assessment):
    """
    Splits the receipt into what carries tax and what does not.

    Only lines taxed at a positive rate produce input tax; zero-rated and exempt lines
    carry none, so 'the VAT on this receipt' and 'the recoverable VAT on this receipt'
    are different numbers whenever a receipt mixes the two.
    """
    rates = _rates_by_code(receipt)
    standard = exempt = 0

    for item in _printed(receipt):
        amount = item.amount_cents or 0
        rate = rates.get((item.tax_code or '').strip().upper())
        if rate is not None and rate > 0:
            standard += amount
        else:
            exempt += amount

    # A receipt with no usable item lines still has totals; fall back to the tax lines
    # so the split is never silently zero.
    if not standard and not exempt:
        taxed_lines = sum(line.amount_cents or 0 for line in _tax_lines(receipt) if (line.rate or 0) > 0)
        total = receipt.total_incl_tax_cents or 0
        standard = total if taxed_lines else 0
        exempt = 0 if taxed_lines else total

    assessment.standard_rated_cents = standard
    assessment.zero_or_exempt_cents = exempt
    assessment.input_vat_cents = sum(
        line.amount_cents or 0 for line in _tax_lines(receipt) if (line.rate or 0) > 0
    )


def _check_totals(receipt, assessment):
    """excl + tax = incl. It is the one identity every receipt must satisfy."""
    excl = receipt.total_excl_tax_cents
    tax = receipt.total_tax_cents
    incl = receipt.total_incl_tax_cents

    if excl is None or tax is None or incl is None:
        assessment.checks.append(Check(
            'totals', 'Totals add up', NA,
            'The receipt does not carry all three totals, so they cannot be reconciled.',
        ))
        return True

    difference = excl + tax - incl
    if abs(difference) <= BASE_TOLERANCE_CENTS:
        assessment.checks.append(Check(
            'totals', 'Totals add up', PASS,
            f'{_money(excl)} excluding tax plus {_money(tax)} tax equals the printed '
            f'total of {_money(incl)}.', weight=15,
        ))
        return True

    assessment.checks.append(Check(
        'totals', 'Totals add up', FAIL,
        f'{_money(excl)} plus {_money(tax)} tax comes to {_money(excl + tax)}, but the '
        f'receipt totals {_money(incl)} - a difference of {_money(abs(difference))}.', weight=15,
    ))
    return False


def _check_tax_arithmetic(receipt, assessment):
    """
    Recomputes the tax from the line items and their rate codes.

    Amounts printed against each item on a TRA receipt are tax-inclusive, so the tax on
    a group of lines at rate r is total x r/(100+r). A mismatch means the EFD's own
    arithmetic disagrees with its printed rate, which is either a faulty machine or a
    doctored receipt - and either way it is not a claim to file.
    """
    lines = [line for line in _tax_lines(receipt) if (line.rate or 0) > 0]
    if not lines:
        assessment.checks.append(Check(
            'tax_arithmetic', 'Tax calculation', NA, 'No tax was charged, so there is nothing to recompute.',
        ))
        return True

    items = _printed(receipt)
    coded = [item for item in items if (item.tax_code or '').strip()]
    if not coded or len(coded) != len(items):
        assessment.checks.append(Check(
            'tax_arithmetic', 'Tax calculation', NA,
            'Not every line carries a tax code, so the tax cannot be recomputed from the '
            'items. Only the printed totals are available.',
        ))
        return True

    problems = []
    for line in lines:
        code = (line.code or '').strip().upper()
        base = sum(item.amount_cents or 0 for item in coded if (item.tax_code or '').strip().upper() == code)
        if not base:
            problems.append(f'tax was charged at rate {code} but no line item is coded {code}')
            continue

        expected = _tax_from_inclusive(base, line.rate)
        printed = line.amount_cents or 0
        tolerance = BASE_TOLERANCE_CENTS * max(1, sum(
            1 for item in coded if (item.tax_code or '').strip().upper() == code
        ))
        if abs(expected - printed) > tolerance:
            problems.append(
                f'{_money(base)} at {_rate(line.rate)}% should carry {_money(expected)} tax, '
                f'but {_money(printed)} is printed'
            )

    if problems:
        assessment.checks.append(Check(
            'tax_arithmetic', 'Tax calculation', FAIL,
            'The tax does not follow from the items: ' + '; '.join(problems) + '.', weight=10,
        ))
        return False

    assessment.checks.append(Check(
        'tax_arithmetic', 'Tax calculation', PASS,
        'Every printed tax figure follows from the line items at the rate shown.', weight=10,
    ))
    return True


def _check_claim_window(receipt, assessment, today):
    """
    How long is left to claim the input tax on this receipt.

    Input tax has to be claimed within six months of the invoice date; after that the
    receipt is still a deductible expense but the VAT on it is simply lost. Returns
    True while the window is open.
    """
    if receipt.receipt_date is None:
        assessment.checks.append(Check(
            'claim_window', 'Input VAT deadline', NA,
            'The receipt carries no date, so the claim deadline cannot be computed.',
        ))
        return False

    if assessment.input_vat_cents <= 0:
        assessment.checks.append(Check(
            'claim_window', 'Input VAT deadline', NA,
            'No input VAT was charged, so no claim deadline applies.',
        ))
        return True

    deadline = add_months(receipt.receipt_date, CLAIM_WINDOW_MONTHS)
    days_left = (deadline - today).days
    assessment.claim_deadline = deadline
    assessment.claim_days_left = days_left

    if days_left < 0:
        assessment.checks.append(Check(
            'claim_window', 'Input VAT deadline', FAIL,
            f'The six-month window closed on {deadline.isoformat()}, {abs(days_left)} days '
            f'ago. The {_money(assessment.input_vat_cents)} of input VAT on this receipt '
            'can no longer be claimed.', weight=10,
        ))
        return False

    if days_left <= CLAIM_WINDOW_WARNING_DAYS:
        assessment.checks.append(Check(
            'claim_window', 'Input VAT deadline', WARN,
            f'{days_left} day(s) left to claim: the six-month window closes on '
            f'{deadline.isoformat()}.', weight=10,
        ))
        return True

    assessment.checks.append(Check(
        'claim_window', 'Input VAT deadline', PASS,
        f'{days_left} days left to claim; the window closes on {deadline.isoformat()}.', weight=10,
    ))
    return True


def _check_timing(receipt, assessment):
    """Weekend and out-of-hours purchases, which audits look at first."""
    if receipt.receipt_date is None:
        return

    weekend = receipt.receipt_date.weekday() >= 5
    receipt_time = getattr(receipt, 'receipt_time', None)
    after_hours = bool(receipt_time) and not (BUSINESS_HOURS[0] <= receipt_time <= BUSINESS_HOURS[1])

    if not weekend and not after_hours:
        return

    when = []
    if weekend:
        when.append(f'a {receipt.receipt_date.strftime("%A")}')
    if after_hours:
        when.append(f'{receipt_time.strftime("%H:%M")}, outside business hours')

    assessment.checks.append(Check(
        'timing', 'Purchase timing', INFO,
        f'Bought on {" at ".join(when)}. Business purchases do happen then, but this is '
        'the pattern personal spending makes, so the business purpose is worth recording.',
    ))


def _resolve_recovery(assessment, voided, buyer_ok, vendor_ok, window_ok, sums_ok, business_vrn):
    """
    Turns the input VAT charged into the input VAT actually recoverable.

    Every gate that closes is recorded, because 'you cannot claim this' is only useful
    with the reason attached.
    """
    # Blockers explain a recoverable figure that is lower than the VAT charged, so they
    # are only meaningful on a receipt that charged some.
    blockers = []
    if assessment.input_vat_cents > 0:
        if voided:
            blockers.append('the receipt is not a live sale')
        if not buyer_ok:
            blockers.append('it is not issued to your TIN')
        if not vendor_ok:
            blockers.append('the supplier is not VAT-registered')
        if not window_ok:
            blockers.append('the six-month claim window has closed')
        if not sums_ok:
            blockers.append('the tax on it does not add up')

    assessment.recovery_blockers = blockers
    assessment.recoverable_vat_cents = 0 if blockers else assessment.input_vat_cents

    if assessment.input_vat_cents <= 0:
        detail = 'No VAT was charged on this receipt, so there is no input tax to recover.'
        status = NA
    elif blockers:
        detail = (
            f'None of the {_money(assessment.input_vat_cents)} VAT charged is recoverable, '
            f'because {"; and ".join(blockers)}.'
        )
        status = FAIL
    else:
        detail = (
            f'{_money(assessment.recoverable_vat_cents)} of input VAT is recoverable on '
            f'{_money(assessment.standard_rated_excl_cents)} of standard-rated purchases '
            f'({_money(assessment.standard_rated_cents)} including tax).'
        )
        if assessment.zero_or_exempt_cents:
            detail += (
                f' The remaining {_money(assessment.zero_or_exempt_cents)} is zero-rated or '
                'exempt and carries none.'
            )
        status = PASS

    if not business_vrn and status == PASS:
        detail += ' (Assumes your own VAT registration is current.)'

    assessment.checks.append(Check('input_vat', 'Input VAT recoverable', status, detail))


def _printed(receipt):
    """
    The lines the document itself carries.

    Every check that recomputes something the paperwork asserts has to run on these
    alone. A receipt can also hold lines read out of the sender's note - what the buyer
    says they bought, on a payment record that itemises nothing - and those have no tax
    code and never had one. Counted here they would report that a fully verified receipt
    could not have its tax checked, the moment somebody typed what was in the bag.
    """
    printed = getattr(receipt, 'printed_items', None)
    if printed is not None:
        return list(printed)
    return list(getattr(receipt, 'items', None) or [])


def _read_line_items(receipt, assessment, capital_threshold_cents):
    """
    The judgments that belong to individual lines: category, capital, WHT, restrictions.

    Kept separate from the checks above because these are about what was bought, not
    about whether the paperwork holds up.
    """
    # Everything the receipt is known to have bought, which on a mobile money record is
    # only ever what the sender's note said. Unlike the two checks above this one reads
    # descriptions rather than recomputing a printed figure, and a line the payer wrote
    # is a far better description of the purchase than 'LIPA JACLINE NGILISHO MOLLEL'.
    # The amounts it also reads cannot double-count: a note line carries one only where
    # the printed lines named nothing, and a line that names nothing matches none of the
    # keywords that put an amount into a finding.
    items = getattr(receipt, 'items', None) or []
    assessment.computed_category = categorise_receipt(item.description for item in items)

    excl_ratio = _exclusive_ratio(receipt)

    for item in items:
        description = item.description or ''
        amount = item.amount_cents or 0

        capital, keyword = is_capital_item(description, amount, capital_threshold_cents)
        if capital:
            assessment.capital_items.append({
                'line_number': item.line_number,
                'description': description,
                'amount_cents': amount,
                'matched': keyword,
                'note': (
                    f'{_money(amount)} on what reads as a {keyword}. Above '
                    f'{_money(capital_threshold_cents)} this is normally a depreciable asset '
                    'claimed through capital allowances, not an expense deducted in full.'
                ),
            })

        service = wht_class(description)
        if service:
            name, rate, keyword = service
            # Withholding is computed on the fee, which is the amount before VAT.
            base = int((Decimal(amount) * excl_ratio).quantize(Decimal('1'), rounding=ROUND_HALF_UP))
            withheld = int((Decimal(base) * Decimal(rate) / 100).quantize(Decimal('1'), rounding=ROUND_HALF_UP))
            assessment.wht_lines.append({
                'line_number': item.line_number,
                'description': description,
                'wht_class': name,
                'rate': rate,
                'matched': keyword,
                'base_cents': base,
                'amount_cents': withheld,
            })

        for flag, explanation in deductibility_flags(description):
            assessment.restrictions.append({
                'line_number': item.line_number,
                'description': description,
                'flag': flag,
                'note': explanation,
            })

    assessment.wht_total_cents = sum(line['amount_cents'] for line in assessment.wht_lines)

    if assessment.wht_lines:
        classes = sorted({line['wht_class'].replace('_', ' ') for line in assessment.wht_lines})
        assessment.checks.append(Check(
            'wht', 'Withholding tax', WARN,
            f'Looks like {", ".join(classes)}. Around {_money(assessment.wht_total_cents)} '
            'should have been withheld and remitted if you are a withholding agent and '
            f'payments to this supplier pass {_money(WHT_MONTHLY_THRESHOLD_CENTS)} in the month. '
            'Confirm the supplier\'s residence and status before withholding.',
        ))

    if assessment.capital_items:
        assessment.checks.append(Check(
            'capital', 'Capital vs revenue', WARN,
            f'{len(assessment.capital_items)} line(s) look like capital assets rather than '
            'running costs; deducting them in full would overstate the expense.',
        ))

    if assessment.restrictions:
        flags = sorted({entry['flag'].replace('_', ' ') for entry in assessment.restrictions})
        assessment.checks.append(Check(
            'restricted', 'Restricted expenditure', WARN,
            f'Contains {", ".join(flags)}, which the Income Tax Act restricts or disallows.',
        ))


def _score(checks, voided):
    """
    The compliance badge, 0-100, derived from the weighted checks.

    Checks that do not apply are dropped from the denominator rather than scored as
    zero, so a receipt with no VAT on it is not marked down for having no VRN.
    """
    if voided:
        return 0

    possible = sum(check.weight for check in checks if check.scored)
    if not possible:
        return None

    earned = sum(check.weight * _STATUS_CREDIT[check.status] for check in checks if check.scored)
    return int(Decimal(earned * 100 / possible).quantize(Decimal('1'), rounding=ROUND_HALF_UP))


# --- Helpers ----------------------------------------------------------------

def _tax_lines(receipt):
    return getattr(receipt, 'tax_lines', None) or []


def _rates_by_code(receipt):
    """{'A': Decimal('18'), 'EX': Decimal('0')} from the receipt's printed tax lines."""
    return {
        (line.code or '').strip().upper(): Decimal(line.rate or 0)
        for line in _tax_lines(receipt)
    }


def _tax_from_inclusive(base_cents, rate):
    """Tax contained in a tax-inclusive amount: total x rate/(100+rate)."""
    rate = Decimal(rate or 0)
    if rate <= 0:
        return 0
    return int((Decimal(base_cents) * rate / (100 + rate)).quantize(Decimal('1'), rounding=ROUND_HALF_UP))


def _exclusive_ratio(receipt):
    """
    How much of a tax-inclusive line is the fee itself, as a Decimal in [0, 1].

    Derived from the receipt's own totals rather than assumed to be 100/118, so a
    receipt that mixes rated and exempt lines still nets down sensibly.
    """
    incl = receipt.total_incl_tax_cents or 0
    excl = receipt.total_excl_tax_cents
    if not incl or excl is None:
        return Decimal(1)
    return Decimal(excl) / Decimal(incl)


def _same_tin(left, right):
    """TINs compare on digits alone: '100-147-181' and '100147181' are one taxpayer."""
    strip = lambda value: ''.join(character for character in str(value or '') if character.isalnum())
    return bool(strip(left)) and strip(left) == strip(right)


def add_months(start, months):
    """The same day-of-month `months` later, clamped to the end of a shorter month."""
    month_index = start.month - 1 + months
    year = start.year + month_index // 12
    month = month_index % 12 + 1
    day = min(start.day, _days_in_month(year, month))
    return date(year, month, day)


def _days_in_month(year, month):
    if month == 12:
        return 31
    return (date(year, month + 1, 1) - date(year, month, 1)).days


def _money(cents):
    """'1,234.56' - the format every figure in a check's wording uses."""
    return f'{(Decimal(cents or 0) / 100):,.2f}'


def _rate(rate):
    """'18' rather than '18.00', for wording that reads like a receipt."""
    value = Decimal(rate or 0).normalize()
    if value == value.to_integral_value():
        value = value.to_integral_value()
    return format(value, 'f')
