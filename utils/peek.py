# utils/peek.py
"""
The card behind every linked value on a page.

A receipt is not a row of text. Almost every field printed on it is a key into
everything else we hold: the TIN identifies a supplier we have twelve other receipts
from, the EFD serial identifies one till among that supplier's four, the category is a
slice of the quarter's spending, the date sits in a VAT period with a filing deadline.
The dashboard used to show those as inert strings, so answering "is this vendor always
like this?" meant leaving the page, and most of the time nobody bothered - which is
the same as not holding the data at all.

This module turns each of those keys into one small, uniform answer. Every builder
returns the same `Card` shape, so the browser has a single renderer to write and
adding a hoverable field later is a function here rather than a new widget there.

Three rules, and they are the same ones utils/analytics works under:

  * Every card carries its evidence - how many receipts, over what span - because a
    figure without its denominator is a number the reader has to trust rather than
    judge.
  * Nothing is asserted from a single observation. A price "trend" through one point
    is noise, and a vendor's habits cannot be read off their first receipt.
  * The notes at the foot of a card are the point of it. Totals are lookup; the note
    is the thing the admin would otherwise have had to notice for themselves, and it
    names what it would cost to ignore.

Values are formatted here rather than in the browser so that one receipt's total is
written the same way on the table, in a card and on the receipt page.
"""
from dataclasses import dataclass, field
from datetime import date, timedelta
from decimal import Decimal
from statistics import mean

from sqlalchemy.orm import joinedload, selectinload

from models.user import Receipt, ReceiptItem, Submission, Vendor, db
from utils import classify, compliance
from utils.money import from_cents

# Tones the browser knows how to colour. Kept to five so a card cannot invent a
# meaning the rest of the interface does not already use.
GOOD, WARN, BAD, INFO, MUTED = 'good', 'warn', 'bad', 'info', 'muted'

# How many of a vendor's receipts are assessed individually for one card. The
# aggregates above it are done by the database and cover everything; only the
# claimable/blocked split needs the checks run per receipt, and a hover is not the
# place to run five thousand of them. Where the cap bites, the card says so rather
# than quietly reporting a partial figure as a whole one.
MAX_ASSESSED = 500

# Line items pulled back when pricing one product. The comparison is against what a
# supplier charges *lately*, so the newest few hundred sightings answer it and a
# five-year sweep answers a different question, more slowly.
MAX_PRICE_CANDIDATES = 300


@dataclass
class Card:
    """
    One hover card, in the only shape the browser knows how to draw.

    The sections are deliberately few and always mean the same thing:

      badges   - what this thing *is* (VAT registered, verified by TRA, cancelled)
      stats    - the two or three headline numbers, large
      rows     - label/value pairs, the supporting detail
      checks   - pass/warn/fail items with their own wording, for compliance
      notes    - the finding: prose, tone-coloured, the reason to look
      evidence - what the card was computed from
      href     - where a click goes, which is always a real, linkable page
    """
    kind: str
    key: str
    title: str
    subtitle: str = None
    badges: list = field(default_factory=list)
    stats: list = field(default_factory=list)
    rows: list = field(default_factory=list)
    checks: list = field(default_factory=list)
    notes: list = field(default_factory=list)
    evidence: str = None
    href: str = None
    href_label: str = None

    def badge(self, label, tone=MUTED):
        self.badges.append({'label': label, 'tone': tone})
        return self

    def stat(self, label, value, sub=None):
        self.stats.append({'label': label, 'value': value, 'sub': sub})
        return self

    def row(self, label, value, tone=MUTED):
        self.rows.append({'label': label, 'value': value, 'tone': tone})
        return self

    def money_row(self, label, cents, tone=MUTED):
        return self.row(label, _money(cents), tone)

    def note(self, text, tone=INFO):
        self.notes.append({'text': text, 'tone': tone})
        return self

    def as_dict(self):
        return {
            'kind': self.kind, 'key': self.key, 'title': self.title,
            'subtitle': self.subtitle, 'badges': self.badges, 'stats': self.stats,
            'rows': self.rows, 'checks': self.checks, 'notes': self.notes,
            'evidence': self.evidence, 'href': self.href, 'href_label': self.href_label,
        }


def build(kind, key, business=None, today=None):
    """
    The card for one entity, or None when there is nothing behind the key.

    `business` is the instance config, needed by anything that has to say whether a
    receipt is claimable *by this taxpayer*; `today` is passed through to the claim
    window arithmetic so the caller can pin it in a test.
    """
    builder = BUILDERS.get(kind)
    if builder is None:
        return None

    card = builder(key, business, today or date.today())
    return card.as_dict() if card else None


# --- Vendor -----------------------------------------------------------------

def vendor_card(key, business, today):
    """
    A supplier at a glance: what they cost us, and whether their paperwork holds up.

    The second half is the part that is invisible receipt by receipt. One receipt made
    out to a walk-in customer is an annoyance; the same vendor doing it on nine
    receipts out of twelve is a conversation to have with them, and it is only visible
    once they are added up.
    """
    vendor, query = vendor_query(key)
    if query is None:
        return None

    receipts = _load(query.order_by(Receipt.receipt_date.desc().nullslast()), MAX_ASSESSED)
    count, total_cents, vat_cents, first_seen, last_seen, tills = query.with_entities(
        db.func.count(Receipt.id),
        db.func.sum(Receipt.total_incl_tax_cents),
        db.func.sum(Receipt.total_tax_cents),
        db.func.min(Receipt.receipt_date),
        db.func.max(Receipt.receipt_date),
        db.func.count(db.distinct(Receipt.efd_serial)),
    ).one()

    if not count and vendor is None:
        return None

    name = (vendor.name if vendor else None) or (receipts[0].vendor_name if receipts else None)
    tin = (vendor.tin if vendor else None) or (receipts[0].vendor_tin if receipts else None)
    vrn = (vendor.vrn if vendor else None) or (receipts[0].vrn if receipts else None)
    office = (vendor.tax_office if vendor else None) or (receipts[0].tax_office if receipts else None)

    card = Card('vendor', key, name or 'Unnamed vendor',
                subtitle=' · '.join(filter(None, [f'TIN {tin}' if tin else 'no TIN', office])))
    if vrn:
        card.badge('VAT registered', GOOD)
    else:
        card.badge('no VRN', WARN)
    if tills > 1:
        card.badge(f'{tills} tills', INFO)

    total_cents = total_cents or 0
    card.stat('Total spend', _money(total_cents), 'TZS')
    card.stat('Receipts', str(count))
    card.stat('Average', _money(int(total_cents / count)) if count else '---', 'TZS')

    # Claimable vs merely charged. The two are confused constantly, and the gap is the
    # only figure on this card that is money already lost.
    charged = recoverable = blocked = 0
    blockers, expiring, not_ours = {}, 0, 0
    for receipt in receipts:
        assessment = _assess(receipt, business, today)
        charged += assessment.input_vat_cents
        recoverable += assessment.recoverable_vat_cents
        if assessment.input_vat_cents > 0 and assessment.recoverable_vat_cents == 0:
            blocked += assessment.input_vat_cents
            for blocker in assessment.recovery_blockers:
                blockers[blocker] = blockers.get(blocker, 0) + assessment.input_vat_cents
        if assessment.claim_days_left is not None and 0 <= assessment.claim_days_left <= 30 \
                and assessment.recoverable_vat_cents > 0:
            expiring += 1
        buyer = assessment.check('buyer_tin')
        if buyer is not None and buyer.status == compliance.FAIL:
            not_ours += 1

    card.money_row('VAT charged', vat_cents or 0)
    card.money_row('Recoverable', recoverable, GOOD if recoverable else MUTED)
    if blocked:
        card.money_row('Blocked', blocked, BAD)
    if tills > 1:
        card.row('Tills / branches', str(tills))

    if not_ours and receipts:
        card.note(
            f'{not_ours} of the {len(receipts)} receipts assessed are not made out to your '
            f'TIN. Input VAT on those is not claimable, whoever paid.', BAD if blocked else WARN,
        )
    elif blockers:
        reason = max(blockers, key=blockers.get)
        card.note(f'{_money(blocked)} of input VAT is blocked, mostly: {reason}.', BAD)
    if expiring:
        card.note(f'{expiring} receipt(s) from this supplier fall out of the claim window '
                  'within 30 days.', WARN)
    if vrn is None and (vat_cents or 0) > 0:
        card.note('No VRN on file for this supplier, yet their receipts carry VAT. '
                  'Input VAT from an unregistered supplier cannot be claimed.', BAD)
    if count == 1:
        card.note('First and only receipt from this supplier - nothing to compare it against '
                  'yet.', MUTED)

    card.evidence = _span(count, first_seen, last_seen, assessed=len(receipts))
    card.href = f'/vendors/{key}'
    card.href_label = 'Open vendor profile'
    return card


def vendor_query(key):
    """
    (vendor row, query for its receipts) for a lookup key, or (row, None) if unusable.

    Receipts are matched through the Vendor row where there is one, because that is
    keyed on the TIN TRA issued and survives the supplier spelling their own name
    three ways. A key with no Vendor row behind it - a photographed receipt whose
    vendor block never made it into a row - still resolves on the printed TIN, so the
    card works for those too.

    A receipt that carries the TIN but was never linked to the row is counted with the
    rest. Those exist: every receipt filed before vendors were introduced was linked by
    a backfill, and anything the backfill could not reach would otherwise vanish from
    its own supplier's totals - silently, which is the worst way for money to go
    missing. The unlinked ones are only picked up where there is no vendor_id at all,
    so a receipt cannot be counted under two suppliers.
    """
    vendor = Vendor.query.filter_by(lookup_key=key).first()
    query = (
        Receipt.query.join(Submission, Receipt.submission_id == Submission.id)
        .filter(Submission.status == 'completed',
                Receipt.is_cancelled.is_(False), Receipt.is_test.is_(False))
    )

    if vendor is not None:
        owned = Receipt.vendor_id == vendor.id
        if vendor.tin:
            owned = db.or_(owned, db.and_(Receipt.vendor_id.is_(None),
                                          Receipt.vendor_tin == vendor.tin))
        return vendor, query.filter(owned)
    if key.startswith('tin:'):
        return None, query.filter(Receipt.vendor_tin == key[4:])
    if key.startswith('name:'):
        return None, query.filter(db.func.lower(Receipt.vendor_name) == key[5:])
    return None, None


# --- One receipt's own verdict ----------------------------------------------

def compliance_card(key, business, today):
    """
    Every check on one receipt, with the wording behind it.

    The table has room for a score and two or three words of summary; the sentence
    that says what to actually do about it lives on the receipt page. This puts that
    sentence a hover away, which is the whole difference between a score somebody
    glances at and one they act on.
    """
    receipt = _receipt(key)
    if receipt is None:
        return None

    assessment = _assess(receipt, business, today)
    card = Card('compliance', key, receipt.vendor_name or 'Unnamed vendor',
                subtitle=f'Compliance {assessment.score}/100'
                if assessment.score is not None else 'Not scored')

    failed = assessment.failed_checks
    if failed:
        card.badge(f'{len(failed)} check(s) failed', BAD)
    else:
        card.badge('all checks pass', GOOD)

    card.checks = [
        {'label': check.label, 'status': check.status, 'detail': check.detail}
        for check in assessment.checks
    ]
    card.evidence = 'Computed from the receipt. No model involved.'
    card.href = f'/receipts/{receipt.id}'
    card.href_label = 'Open the receipt'
    return card


def vat_card(key, business, today):
    """
    What is claimable on one receipt, what is not, and how long is left to act.

    Charged and recoverable are different figures and the gap between them is money.
    Where they differ, the blockers are named: a claim is refused for a reason, and
    the reason is usually fixable next time.
    """
    receipt = _receipt(key)
    if receipt is None:
        return None

    assessment = _assess(receipt, business, today)
    card = Card('vat', key, 'Input VAT on this receipt',
                subtitle=receipt.vendor_name or 'Unnamed vendor')

    card.stat('Charged', _money(assessment.input_vat_cents), 'TZS')
    card.stat('Recoverable', _money(assessment.recoverable_vat_cents), 'TZS')
    if assessment.claim_days_left is not None:
        card.stat('Days left', 'expired' if assessment.claim_days_left < 0
                  else str(assessment.claim_days_left))

    card.money_row('Standard rated (excl.)', assessment.standard_rated_excl_cents)
    card.money_row('Zero rated or exempt', assessment.zero_or_exempt_cents)
    if assessment.claim_deadline:
        card.row('Claim window closes', assessment.claim_deadline.strftime('%d %b %Y'),
                 BAD if (assessment.claim_days_left or 0) < 0 else MUTED)

    for blocker in assessment.recovery_blockers:
        card.note(blocker, BAD)
    if not assessment.recovery_blockers and assessment.recoverable_vat_cents > 0:
        card.note('Claimable in full on the return for the period this receipt falls in.', GOOD)

    if receipt.receipt_date:
        period = receipt.receipt_date.strftime('%Y-%m')
        card.href = f'/vat-ledger?period={period}'
        card.href_label = f'Open the {period} VAT ledger'
    card.evidence = 'Computed from the printed tax lines against your own registration.'
    return card


def customer_card(key, business, today):
    """
    Who the receipt was made out to - the field that decides whether VAT is claimable.

    An EFD prints 'CUSTOMER 0 / NIL NIL' when the cashier was not given a TIN, and
    that receipt is a walk-in sale as far as TRA is concerned. It is the single most
    expensive habit this system can catch, and it is invisible unless somebody reads
    a line most people skip.
    """
    receipt = _receipt(key)
    if receipt is None:
        return None

    printed = ' '.join(filter(None, [receipt.customer_id_type, receipt.customer_id])).strip()
    card = Card('customer', key, 'Made out to',
                subtitle=receipt.customer_name or printed or 'nobody in particular')
    card.row('Customer name', receipt.customer_name or '---')
    card.row('Customer ID', printed or '---')

    assessment = _assess(receipt, business, today)
    check = assessment.check('buyer_tin')
    if check is not None:
        card.checks = [{'label': check.label, 'status': check.status, 'detail': check.detail}]

    business_tin = getattr(business, 'business_tin', None)
    if not business_tin:
        card.note('Your own TIN is not configured, so nothing can be checked against it.', WARN)
        card.href = '/admin/configure'
        card.href_label = 'Set your TIN'
    else:
        card.row('Your TIN', business_tin)
        card.href = f'/receipts/{receipt.id}'
        card.href_label = 'Open the receipt'
    return card


def code_card(key, business, today):
    """
    A verification code, which is the receipt's identity as far as TRA is concerned.

    Worth its own card because it is what duplicates are caught on, and because a code
    that appears on two stored receipts is one purchase counted twice.
    """
    receipt = Receipt.query.filter_by(receipt_verification_code=key).options(
        selectinload(Receipt.items), selectinload(Receipt.tax_lines),
        joinedload(Receipt.submission),
    ).first()
    if receipt is None:
        return None

    card = Card('code', key, key, subtitle=receipt.vendor_name or 'Unnamed vendor')
    if receipt.extraction_source == 'tra_html':
        card.badge('verified by TRA', GOOD)
    elif receipt.extraction_source == 'llm_vision':
        card.badge('read from a photo', WARN)
    if receipt.is_cancelled:
        card.badge('cancelled', BAD)
    if receipt.is_test:
        card.badge('test receipt', BAD)

    card.stat('Total', _money(receipt.total_incl_tax_cents), 'TZS')
    card.row('Receipt no.', receipt.receipt_number or '---')
    card.row('Z number', receipt.z_number or '---')
    card.row('Issued', _when(receipt))
    if receipt.efd_serial:
        card.row('Till (EFD)', receipt.efd_serial)

    twins = _duplicates(receipt)
    if twins:
        card.note(f'Same supplier, date and total as receipt #{twins[0].id}. If that is the '
                  'same purchase submitted twice, the expense is being counted twice.', WARN)

    card.evidence = 'The identity TRA issued for this receipt.'
    card.href = f'/receipts/{receipt.id}'
    card.href_label = 'Open the receipt'
    return card


def till_card(key, business, today):
    """
    One EFD serial: a single till, which is a branch or a lane inside a supplier.

    Two serials against one TIN is normal for a supermarket and odd for a one-room
    hardware shop, and either way it is the level at which "the receipt came from the
    other branch" stops being a guess.
    """
    query = (
        Receipt.query.join(Submission, Receipt.submission_id == Submission.id)
        .filter(Submission.status == 'completed', Receipt.efd_serial == key,
                Receipt.is_cancelled.is_(False), Receipt.is_test.is_(False))
    )
    count, total_cents, first_seen, last_seen = query.with_entities(
        db.func.count(Receipt.id), db.func.sum(Receipt.total_incl_tax_cents),
        db.func.min(Receipt.receipt_date), db.func.max(Receipt.receipt_date),
    ).one()
    if not count:
        return None

    newest = query.order_by(Receipt.receipt_date.desc().nullslast()).first()
    card = Card('till', key, f'Till {key}',
                subtitle=newest.vendor_name or 'Unnamed vendor')
    card.stat('Through this till', _money(total_cents or 0), 'TZS')
    card.stat('Receipts', str(count))

    if newest.vendor_id:
        siblings = (
            Receipt.query.join(Submission, Receipt.submission_id == Submission.id)
            .filter(Submission.status == 'completed', Receipt.vendor_id == newest.vendor_id)
            .with_entities(db.func.count(db.distinct(Receipt.efd_serial))).scalar()
        )
        if siblings and siblings > 1:
            card.row('Tills at this supplier', str(siblings))
            card.note(f'This supplier issues receipts from {siblings} separate tills. Prices '
                      'and paperwork can differ between them.', INFO)

    card.evidence = _span(count, first_seen, last_seen)
    if newest.vendor and newest.vendor.lookup_key:
        card.href = f'/vendors/{newest.vendor.lookup_key}'
        card.href_label = 'Open vendor profile'
    return card


# --- Slices of the whole ledger ---------------------------------------------

def category_card(key, business, today):
    """
    One expense category: its share of spending, and what it is normally made of.

    The share is the useful part. A category is unremarkable until you see that it is
    a third of everything, or that one supplier is all of it.
    """
    query = (
        Receipt.query.join(Submission, Receipt.submission_id == Submission.id)
        .filter(Submission.status == 'completed',
                Receipt.is_cancelled.is_(False), Receipt.is_test.is_(False))
    )
    scoped = query.filter(Receipt.category == key)
    count, total_cents, vat_cents, largest = scoped.with_entities(
        db.func.count(Receipt.id), db.func.sum(Receipt.total_incl_tax_cents),
        db.func.sum(Receipt.total_tax_cents), db.func.max(Receipt.total_incl_tax_cents),
    ).one()
    if not count:
        return None

    overall = query.with_entities(db.func.sum(Receipt.total_incl_tax_cents)).scalar() or 0
    total_cents = total_cents or 0

    card = Card('category', key, key.replace('_', ' ').title(), subtitle='Expense category')
    card.stat('Spend', _money(total_cents), 'TZS')
    card.stat('Share', f'{total_cents * 100 / overall:.1f}%' if overall else '--')
    card.stat('Receipts', str(count))
    card.money_row('Average receipt', int(total_cents / count))
    card.money_row('Largest receipt', largest or 0)
    card.money_row('VAT charged', vat_cents or 0)

    top = (
        scoped.with_entities(Receipt.vendor_name, db.func.sum(Receipt.total_incl_tax_cents))
        .group_by(Receipt.vendor_id).order_by(db.func.sum(Receipt.total_incl_tax_cents).desc())
        .limit(3).all()
    )
    for name, cents in top:
        card.row(name or 'Unnamed vendor', _money(cents or 0))

    if top and total_cents:
        leader, leader_cents = top[0][0], top[0][1] or 0
        share = leader_cents * 100 / total_cents
        if share >= 60 and count > 2:
            card.note(f'{leader or "One supplier"} is {share:.0f}% of this category. Worth '
                      'knowing what the alternative costs before the next order.', INFO)

    card.evidence = f'{count} receipt(s), all periods.'
    card.href = f'/?tab=processed&category={key}'
    card.href_label = 'Show these receipts'
    return card


def date_card(key, business, today):
    """
    One day's spending, and the VAT period it falls in.

    A date on a receipt is really two facts: what else was bought that day, and which
    return this VAT belongs on. The second one has a deadline, so it is on the card.
    """
    try:
        day = date.fromisoformat(key)
    except (TypeError, ValueError):
        return None

    query = (
        Receipt.query.join(Submission, Receipt.submission_id == Submission.id)
        .filter(Submission.status == 'completed',
                Receipt.is_cancelled.is_(False), Receipt.is_test.is_(False))
    )
    count, total_cents, vat_cents = query.filter(Receipt.receipt_date == day).with_entities(
        db.func.count(Receipt.id), db.func.sum(Receipt.total_incl_tax_cents),
        db.func.sum(Receipt.total_tax_cents),
    ).one()

    start = day.replace(day=1)
    end = compliance.add_months(start, 1) - timedelta(days=1)
    month_count, month_cents = query.filter(
        Receipt.receipt_date >= start, Receipt.receipt_date <= end,
    ).with_entities(
        db.func.count(Receipt.id), db.func.sum(Receipt.total_incl_tax_cents),
    ).one()

    card = Card('date', key, day.strftime('%d %B %Y'), subtitle=day.strftime('%A'))
    card.stat('Spent that day', _money(total_cents or 0), 'TZS')
    card.stat('Receipts', str(count))
    card.money_row('VAT charged that day', vat_cents or 0)
    card.row(f'{start.strftime("%B %Y")} so far', f'{_money(month_cents or 0)} · {month_count} receipts')

    # The return for a period is due on the 20th of the month after it.
    due = compliance.add_months(start, 1).replace(day=20)
    days_to_due = (due - today).days
    card.row('VAT return due', due.strftime('%d %b %Y'),
             WARN if 0 <= days_to_due <= 7 else (MUTED if days_to_due >= 0 else BAD))
    if 0 <= days_to_due <= 7:
        card.note(f'The {start.strftime("%B %Y")} return is due in {days_to_due} day(s).', WARN)

    card.href = f'/?tab=processed&start_date={key}&end_date={key}'
    card.href_label = 'Show that day'
    return card


def item_card(key, business, today):
    """
    One line off a receipt: what it costs here, what it cost before, what it costs
    elsewhere, and what buying it implies for tax.

    Unit prices only exist where the EFD printed a quantity - plenty print
    'SUMMARIZED SALE' and nothing else - so the price half of this card is silent
    rather than guessed at when there is nothing to divide by.
    """
    receipt_id, _, line_number = key.partition(':')
    try:
        item = ReceiptItem.query.filter_by(
            receipt_id=int(receipt_id), line_number=int(line_number),
        ).first()
    except (TypeError, ValueError):
        return None
    if item is None:
        return None

    receipt = item.receipt
    card = Card('item', key, item.description or 'Unnamed line',
                subtitle=f'Line {item.line_number} · {receipt.vendor_name or "unnamed vendor"}')
    card.stat('Amount', _money(item.amount_cents), 'TZS')
    if item.quantity:
        card.stat('Quantity', f'{float(item.quantity):g}')
    if item.tax_code:
        card.badge(f'tax code {item.tax_code}', INFO)

    unit_cents = _unit_price(item)
    if unit_cents is not None:
        card.stat('Unit price', _money(unit_cents), 'TZS')
        history, elsewhere = _price_history(item, receipt)

        if len(history) >= 2:
            baseline = int(mean(entry['unit_cents'] for entry in history))
            card.money_row('Usual here', baseline)
            change = (unit_cents - baseline) * 100 / baseline if baseline else 0
            if abs(change) >= 10:
                card.note(
                    f'{abs(change):.0f}% {"above" if change > 0 else "below"} the '
                    f'{_money(baseline)} this supplier has averaged over '
                    f'{len(history)} earlier purchase(s).',
                    WARN if change > 0 else GOOD,
                )
            # The last time we bought it here, which is where that baseline came from.
            previous = history[0]
            card.href = f'/receipts/{previous["receipt_id"]}'
            card.href_label = f'Last bought {previous["on"].isoformat()}'
            card.evidence = f'Compared with {len(history)} earlier purchase(s) of the same item.'

        if elsewhere:
            card.money_row('Cheapest elsewhere', elsewhere['unit_cents'], GOOD)
            if unit_cents > 0 and elsewhere['unit_cents'] < unit_cents:
                saving = (unit_cents - elsewhere['unit_cents']) * 100 / unit_cents
                card.note(f'{elsewhere["vendor_name"]} charged {_money(elsewhere["unit_cents"])} a '
                          f'unit, {saving:.0f}% less. Whether they can actually supply it is not '
                          'on the receipt.', INFO)
                # A cheaper supplier is the more useful thing to open than our own history.
                card.href = f'/receipts/{elsewhere["receipt_id"]}'
                card.href_label = 'See that receipt'

    # What buying this implies, read deterministically from the text. Same rules the
    # receipt page reports under 'what follows from the line items'.
    service = classify.wht_class(item.description or '')
    if service:
        name, rate, _matched = service
        card.note(f'Reads as {name.replace("_", " ")}. Around {rate}% withholding tax applies to '
                  'the fee if you are a withholding agent and the supplier is resident.', WARN)
    capital, keyword = classify.is_capital_item(item.description or '', item.amount_cents or 0)
    if capital:
        card.note(f'Reads as a {keyword} above the capitalisation threshold - a depreciable '
                  'asset, not an expense to deduct in full.', WARN)
    for flag, explanation in classify.deductibility_flags(item.description or ''):
        card.note(f'{flag.replace("_", " ")}: {explanation}', WARN)

    # No footer link when there is nothing to compare against. This card is only shown
    # from the receipt the line is printed on, so 'open the receipt' would lead back to
    # the page the reader is already looking at.
    return card


def tax_office_card(key, business, today):
    """The TRA office a supplier is administered by, and who else we buy from there."""
    rows = (
        db.session.query(
            db.func.count(db.distinct(Receipt.vendor_id)),
            db.func.count(Receipt.id),
            db.func.sum(Receipt.total_incl_tax_cents),
        )
        .join(Submission, Receipt.submission_id == Submission.id)
        .filter(Submission.status == 'completed', Receipt.tax_office == key,
                Receipt.is_cancelled.is_(False), Receipt.is_test.is_(False))
        .one()
    )
    vendors, count, total_cents = rows
    if not count:
        return None

    card = Card('tax_office', key, key.title(), subtitle='TRA tax office')
    card.stat('Spend', _money(total_cents or 0), 'TZS')
    card.stat('Suppliers', str(vendors))
    card.stat('Receipts', str(count))
    card.note('Which office a supplier is administered by says how large TRA considers them, '
              'not how compliant they are.', MUTED)
    return card


# --- Shared helpers ---------------------------------------------------------

BUILDERS = {
    'vendor': vendor_card,
    'compliance': compliance_card,
    'vat': vat_card,
    'customer': customer_card,
    'code': code_card,
    'till': till_card,
    'category': category_card,
    'date': date_card,
    'item': item_card,
    'tax_office': tax_office_card,
}


def _receipt(key):
    """A receipt by id, with everything the checks need already loaded."""
    try:
        receipt_id = int(key)
    except (TypeError, ValueError):
        return None
    return Receipt.query.filter_by(id=receipt_id).options(
        selectinload(Receipt.items), selectinload(Receipt.tax_lines),
    ).first()


def _assess(receipt, business, today):
    return compliance.evaluate(
        receipt,
        business_tin=getattr(business, 'business_tin', None),
        business_vrn=getattr(business, 'business_vrn', None),
        today=today,
    )


def _load(query, limit):
    return query.options(
        selectinload(Receipt.items), selectinload(Receipt.tax_lines),
    ).limit(limit).all()


def _duplicates(receipt, limit=3):
    """Other receipts that look like the same purchase. Mirrors main.find_possible_duplicates."""
    if receipt.receipt_date is None or receipt.total_incl_tax_cents is None:
        return []
    query = Receipt.query.filter(
        Receipt.id != receipt.id,
        Receipt.receipt_date == receipt.receipt_date,
        Receipt.total_incl_tax_cents == receipt.total_incl_tax_cents,
    )
    if receipt.vendor_id:
        query = query.filter(Receipt.vendor_id == receipt.vendor_id)
    elif receipt.vendor_tin:
        query = query.filter(Receipt.vendor_tin == receipt.vendor_tin)
    else:
        return []
    return query.limit(limit).all()


def _unit_price(item):
    """What one of them cost, or None when the EFD printed no quantity to divide by."""
    if not item.quantity or not item.amount_cents:
        return None
    quantity = Decimal(item.quantity)
    if quantity <= 0:
        return None
    return int(Decimal(item.amount_cents) / quantity)


def _price_history(item, receipt):
    """
    (earlier purchases of this item here, cheapest current price elsewhere).

    The history is newest first, so history[0] is the last time we bought it from this
    supplier - which is both the most useful thing to link to and where the baseline
    the card compares against came from.

    Candidate lines are narrowed in SQL on the longest word in the description, then
    matched exactly on the normalised text in Python - the same key utils/analytics
    groups items by, so 'DIESEL (AGO)' and 'Diesel AGO' remain one product here too.
    """
    target = classify.normalise_description(item.description)
    if not target:
        return [], None

    token = max(target.split(), key=len, default='')
    candidates = (
        ReceiptItem.query
        .join(Receipt, ReceiptItem.receipt_id == Receipt.id)
        .join(Submission, Receipt.submission_id == Submission.id)
        .filter(
            Submission.status == 'completed',
            Receipt.is_cancelled.is_(False), Receipt.is_test.is_(False),
            ReceiptItem.id != item.id,
            ReceiptItem.description.ilike(f'%{token}%'),
        )
        .options(joinedload(ReceiptItem.receipt))
        .order_by(Receipt.receipt_date.desc().nullslast())
        .limit(MAX_PRICE_CANDIDATES).all()
    )

    same_vendor, others = [], {}
    for other in candidates:
        if classify.normalise_description(other.description) != target:
            continue
        unit = _unit_price(other)
        if unit is None:
            continue

        owner = other.receipt
        sighting = {
            'unit_cents': unit, 'receipt_id': owner.id, 'on': owner.receipt_date,
            'vendor_name': owner.vendor_name or 'another supplier',
        }
        if _same_vendor(owner, receipt):
            # Only prices from *before* this one, so the baseline is history rather
            # than a mixture of history and things bought since.
            if owner.receipt_date and receipt.receipt_date and owner.receipt_date < receipt.receipt_date:
                same_vendor.append(sighting)
        else:
            # Ordered newest first, so the first sighting of a vendor is their latest
            # price - what they would charge if the next order went to them.
            others.setdefault(owner.vendor_id or owner.vendor_tin or owner.vendor_name, sighting)

    cheapest = min(others.values(), key=lambda entry: entry['unit_cents']) if others else None
    return same_vendor, cheapest


def _same_vendor(left, right):
    if left.vendor_id and right.vendor_id:
        return left.vendor_id == right.vendor_id
    if left.vendor_tin and right.vendor_tin:
        return left.vendor_tin == right.vendor_tin
    return (left.vendor_name or '') == (right.vendor_name or '')


def _span(count, first_seen, last_seen, assessed=None):
    """The evidence line: how much was counted, and over what stretch of calendar."""
    parts = [f'{count} receipt(s)']
    if first_seen and last_seen:
        parts.append(f'{first_seen.isoformat()} → {last_seen.isoformat()}')
    if assessed is not None and count > assessed:
        parts.append(f'latest {assessed} assessed individually')
    return ' · '.join(parts)


def _when(receipt):
    if receipt.receipt_date is None:
        return 'undated'
    stamp = receipt.receipt_date.strftime('%d %b %Y')
    return f'{stamp} at {receipt.receipt_time.strftime("%H:%M")}' if receipt.receipt_time else stamp


def _money(cents):
    amount = from_cents(cents)
    return '---' if amount is None else f'{amount:,.2f}'
