# utils/records.py
"""
Reading a written payment record before a model is shown it.

A great deal of what a Tanzanian business spends never produces an EFD receipt. It
produces an SMS: a LUKU token, an M-Pesa confirmation, a bank debit alert, a bill paid
through a wallet. The text path in utils/llm_processor exists for exactly those, and
until now it did the whole job by handing the characters to a language model and storing
whatever came back.

That is more trust than the job needs. Most of what matters in these messages is not a
judgment at all - it is a string sitting in a fixed place:

    You have paid 55000 TZS for  ATANA VENTURES - 994944252324 -
    TANZANIA TELECOMMUNICATION CORPORATION. 17-07-2026 12:17:40.
    New Balance  44,202.04 . TransID MP260717.1217.W74283.

An amount, a timestamp, a transaction id, an account number, and three parties in a row:
the subscriber whose bill was paid, the account it was paid against, and the corporation
that received the money. A regex reads every one of those the same way twice. A model,
asked twice, read the subscriber as the supplier once and the whole dash-separated string
as the supplier the other time - and neither answer named the party actually paid.

So this module reads what can be read, and the model is left the part that genuinely is
judgment: what the purchase was for, which category it belongs to, whether it is
deductible. It is the same division of labour utils/tra_parser gets for free on a
verified receipt, where the figures come off TRA's page and the model is told plainly not
to restate them.

Three ideas carry it:

  * **Spans.** Every value carries the literal characters it came from and where they
    were. Nothing may exist in a scan that is not in the text, which is what makes the
    output checkable rather than merely plausible - and it is what `reconcile` leans on
    when it decides that a figure the model returned came from nowhere.

  * **Roles.** A name in one of these messages is worthless without knowing which party
    it is. `payee` is the vendor; `account_holder` is the customer, and is very often the
    business doing the submitting. Getting those two the wrong way round files a purchase
    under a supplier that does not exist and never can.

  * **Templates over brands.** A format is a shape, not a company. The template that
    reads the message above is written on `paid X for A - N - B`, so it reads every biller
    on that gateway, and adding a format is one tuple in TEMPLATES rather than a change to
    any code. Where no template matches, the generic scanners still run and the roles are
    simply left empty: a scan degrades, it does not fail.

Nothing here touches the database, Flask, or the network. Values in, values out, so the
same rules apply to a paste, to a message a vision model transcribed off a screenshot,
and to a string in a test.
"""

import re
import unicodedata
from dataclasses import dataclass, field
from datetime import datetime

from utils.classify import normalise_description
from utils.fingerprint import normalise_reference
from utils.money import to_cents

# What a reference is worth as an identity, and the whole reason references are typed at
# all. The top group names ONE payment and can safely become the key two submissions are
# called the same purchase on. The bottom group repeats on every payment the same
# customer ever makes - a meter is billed monthly, an account number outlives the
# business - and utils/fingerprint warns about exactly this at its lines 24-30: put one
# of them where the transaction reference belongs and the second purchase on that account
# is filed as a duplicate of the first, for ever.
IDENTIFYING = ('transaction_id', 'token', 'control_number', 'receipt_no', 'invoice_no')
NON_IDENTIFYING = ('account_no', 'meter_no', 'merchant_no', 'till_no', 'phone', 'customer_no')
REFERENCE_KINDS = IDENTIFYING + NON_IDENTIFYING

# Who a name in the record is. `payee` is the vendor and `account_holder` is the
# customer; the two are named separately because on a bill payment they are both present,
# both organisations, and telling them apart is not something the text makes obvious.
ROLES = ('payee', 'payer', 'account_holder', 'intermediary')

# What a number in the record is. `balance_after` earns its place here on its own: a
# wallet balance is the one figure in these messages that looks exactly like the amount
# and is not a figure on the document at all.
AMOUNT_ROLES = ('paid', 'received', 'balance_after', 'fee', 'tax', 'unknown')

# The rail the payment travelled on, not what was bought - utils/classify decides that.
CHANNELS = ('mobile_money', 'bank', 'utility', 'government', 'telecom', 'merchant',
            'bill_payment')

# An SMS gateway writes whichever dash its encoding felt like, and the message that
# prompted this module uses an en dash where a reader would type a hyphen. Any pattern
# that separates parties has to accept all three or it reads the format on one handset
# and not on the next.
_DASH = r'[-–—]'
_NOT_DASH = r'[^-–—\n]'

# A currency marker, written every way this sees. Tsh and TSH are the same shilling.
_CURRENCY = r'TZS|TSH|Tsh|TSh|Sh|SH'

# A number that could be money. Thousands separated or not, at most two decimals.
_NUMBER = r'\d{1,3}(?:,\d{3})+(?:\.\d{1,2})?|\d+(?:\.\d{1,2})?'

# A number that is money even with no currency beside it: it carries a thousands
# separator or a two-decimal tail, which is how a balance is printed and how an account
# number never is. Without this restriction '994944252324' is an amount.
_BARE_NUMBER = r'\d{1,3}(?:,\d{3})+(?:\.\d{1,2})?|\d+\.\d{2}'

# How far back a number's label can sit. Long enough for 'Salio jipya ni ', short enough
# that the previous sentence's verb does not label this sentence's figure.
_LABEL_WINDOW = 30


@dataclass(frozen=True)
class Span:
    """
    Where a value was read, and the characters that were there.

    Carried by every extracted value so that a scan can be checked rather than believed:
    `text[span.start:span.end] == span.text` holds for everything this module produces,
    and the test suite asserts it over the whole corpus. It is also what lets the receipt
    page show a reader the words a figure came out of.
    """
    text: str
    start: int
    end: int


@dataclass(frozen=True)
class Party:
    """A name in the record, and which side of the payment it is on."""
    role: str
    name: str
    span: Span
    confidence: float = 1.0


@dataclass(frozen=True)
class Reference:
    """
    An identifier, and what kind of identifier it is.

    `label` is the word in the text that said so - 'TransID', 'Mita', 'Control No'. Kept
    because it is the evidence for `kind`, and because a reader looking at the receipt
    page deserves to see which word the app read rather than being told to trust it.
    """
    kind: str
    value: str
    normalised: str
    label: str
    span: Span
    confidence: float = 1.0


@dataclass(frozen=True)
class Amount:
    """A figure in the record, in cents, and what it is a figure of."""
    cents: int
    currency: str
    role: str
    span: Span
    confidence: float = 1.0


@dataclass
class RecordScan:
    """
    Everything a machine could read out of one written record.

    The readers below exist so that no caller re-implements precedence - which reference
    counts as the identity, which figure is the amount - because two callers disagreeing
    about that is how a meter number ends up being a receipt number.
    """
    text: str
    # 'pasted' means the characters themselves are the evidence: somebody's phone wrote
    # them and nothing has interpreted them since. 'transcribed' means a vision model
    # read them off a screenshot, so they are the model's own words and the checks in
    # `reconcile` are asking it to agree with itself rather than with the record.
    trust: str = 'pasted'
    channel: str = None
    template: str = None
    parties: list = field(default_factory=list)
    references: list = field(default_factory=list)
    amounts: list = field(default_factory=list)
    occurred_at: datetime = None
    occurred_at_span: Span = None
    occurred_at_confidence: float = 1.0
    # Whether a time was printed beside the date. Midnight is what a date with no time
    # parses to, and storing that as the time of sale would be inventing one.
    occurred_at_has_time: bool = False
    phones: list = field(default_factory=list)
    tins: list = field(default_factory=list)
    # Memoised by _normalised_text; not part of the scan and not serialised.
    _normalised_cache: str = field(default=None, repr=False, compare=False)

    def party(self, role):
        return next((p for p in self.parties if p.role == role), None)

    def payee(self):
        return self.party('payee')

    def account_holder(self):
        return self.party('account_holder')

    def amount(self, role):
        """The single amount in this role, or None where there is not exactly one."""
        found = [a for a in self.amounts if a.role == role]
        return found[0] if len(found) == 1 else None

    def paid(self):
        return self.amount('paid')

    def balance_after(self):
        return self.amount('balance_after')

    def reference(self, kind):
        return next((r for r in self.references if r.kind == kind), None)

    def primary_reference(self):
        """
        The reference worth treating as this payment's identity, or None.

        Searched in IDENTIFYING order rather than in the order they appear, because what
        matters is which kind of thing it is: a transaction id beats a token beats a
        control number, and an account number is never an answer to this question.
        """
        for kind in IDENTIFYING:
            found = self.reference(kind)
            if found:
                return found
        return None

    def mentions_amount(self, cents):
        """
        Whether this figure is in the record at all.

        Asked of every figure a model returns. A scanned amount is the strong answer; the
        rendering check behind it is the forgiving one, for a figure written in a way the
        money scanner did not claim - inside a sentence, or beside a word it did not
        recognise as a currency. Both are containment tests, so neither can approve a
        number the record does not contain.
        """
        if cents is None:
            return False
        if any(a.cents == cents for a in self.amounts):
            return True
        whole, part = divmod(abs(int(cents)), 100)
        renderings = {f'{whole}', f'{whole:,}', f'{whole}.{part:02d}', f'{whole:,}.{part:02d}'}
        return any(r in self.text for r in renderings)

    def contains_reference(self, value):
        """
        Whether these characters are in the record, ignoring how they were punctuated.

        The forgiving half of rule R5. A reference the scanner did not type - unlabelled,
        or in a format no template covers - is still in the record, and dropping it would
        throw away the only identifier the payment has. What this cannot do is approve one
        the model invented, which is the whole point of asking.
        """
        ref = normalise_reference(value)
        return bool(ref) and ref in self._normalised_text()

    def _normalised_text(self):
        """The record's characters alone, computed once - `contains_reference` is asked
        repeatedly, once per reference a model returned."""
        if self._normalised_cache is None:
            self._normalised_cache = re.sub(r'[^A-Za-z0-9]', '', self.text).upper()
        return self._normalised_cache

    def as_dict(self):
        """The scan as JSON, for storage on the submission and for the provenance panel."""
        return {
            'trust': self.trust,
            'channel': self.channel,
            'template': self.template,
            'parties': [{'role': p.role, 'name': p.name, 'confidence': p.confidence}
                        for p in self.parties],
            'references': [{'kind': r.kind, 'value': r.value, 'normalised': r.normalised,
                            'label': r.label, 'confidence': r.confidence}
                           for r in self.references],
            'amounts': [{'role': a.role, 'cents': a.cents, 'currency': a.currency,
                         'text': a.span.text, 'confidence': a.confidence}
                        for a in self.amounts],
            'occurred_at': self.occurred_at.isoformat() if self.occurred_at else None,
            'occurred_at_confidence': self.occurred_at_confidence,
            'occurred_at_has_time': self.occurred_at_has_time,
            'phones': [s.text for s in self.phones],
            'tins': [s.text for s in self.tins],
        }


@dataclass(frozen=True)
class Template:
    """
    One message format, written as a shape rather than as a company.

    `pattern` is the only code in it; everything else says what the named groups mean.
    That is what makes a new format one tuple: a gateway nobody has seen yet is a regex
    and three small dicts, and no function below has to learn about it.

    A group that a pattern leaves unmatched is skipped rather than stored, so a template
    may declare a party or a reference it only sometimes captures.
    """
    name: str
    channel: str
    pattern: re.Pattern
    roles: dict = field(default_factory=dict)        # group -> ROLES
    references: dict = field(default_factory=dict)   # group -> REFERENCE_KINDS
    amounts: dict = field(default_factory=dict)      # group -> AMOUNT_ROLES
    when: str = None                                 # group holding a date or datetime


# The labels that give a value its kind. This is the whole of how an identifier is typed:
# '994944252324' is an account number because the format it sits in says so, and
# 'MP260717.1217.W74283' is a transaction id because the word 'TransID' is printed beside
# it. Nothing here guesses from the shape of the digits, because the shapes overlap
# completely - a control number and an account number are both twelve digits.
LABELS = (
    ('transaction_id', r'TransID|Transaction\s*ID|Txn\s*ID|TxnID|Muamala|Kumbukumbu'
                       r'|Ref(?:erence)?(?:\s*(?:No|Namba))?'),
    ('control_number', r'Control\s*(?:No|Number)|Namba\s*ya\s*kudhibiti|GePG(?:\s*(?:No|Number))?'),
    ('meter_no', r'Meter(?:\s*(?:No|Number))?|Mita'),
    ('token', r'Token|Tokeni'),
    ('account_no', r'Akaunti|Account(?:\s*(?:No|Number|Name))?|A/C'),
    ('merchant_no', r'Merchant(?:\s*(?:No|Number))?|Till(?:\s*(?:No|Number))?'
                    r'|Lipa\s*Namba|Business\s*(?:No|Number)'),
)

# A reference's characters. The alternation is load-bearing and was found by prototyping
# against real messages: a permissive '[A-Z0-9 .-]+' swallows the rest of the sentence
# after 'Akaunti', and without the grouped-digits branch first a twenty-digit LUKU token
# printed in fives comes back as its first group alone.
_REF_VALUE = r'\d{4,5}(?:[ -]\d{4,5}){3,5}|[A-Za-z0-9][A-Za-z0-9./_-]{3,}'

_REFERENCE_PATTERNS = tuple(
    (kind, re.compile(rf'(?i)\b(?P<label>{alternatives})\b\.?\s*(?:[:#=]|ni|is)?\s*'
                      rf'(?P<value>{_REF_VALUE})'))
    for kind, alternatives in LABELS
)

# Parties named by a label rather than by position. This is what lets a GePG confirmation
# or a bank statement line give up its payee without a template of its own: 'Taasisi:
# TANZANIA REVENUE AUTHORITY' says who was paid in any message that chooses to say it.
PARTY_LABELS = (
    ('payee', r'Taasisi|Institution|Mlipwaji|Biller|Payee|Merchant\s*Name|Business\s*Name'
              r'|Jina\s*la\s*biashara'),
    ('account_holder', r'Mteja|Customer\s*Name|Customer|Subscriber|Account\s*Name'
                       r'|Jina\s*la\s*akaunti'),
)

_PARTY_PATTERNS = tuple(
    (role, re.compile(rf'(?i)\b(?P<label>{alternatives})\b\s*[:\-]?\s*'
                      rf'(?P<value>[A-Za-z][^,.\n]{{2,80}})'))
    for role, alternatives in PARTY_LABELS
)

# What the words before a figure say the figure is. Ordered, because a message that reads
# 'Salio baada ya malipo' carries both a balance word and a payment word and the balance
# word is the one that is about this number.
_AMOUNT_ROLE_WORDS = (
    ('balance_after', ('balance', 'salio', 'bakaa', 'baki', 'baada ya malipo')),
    ('fee', ('cost', 'charge', 'charges', 'fee', 'fees', 'makato', 'ada', 'gharama', 'tozo',
             'commission')),
    ('tax', ('vat', 'kodi', 'ushuru', 'tax')),
    ('received', ('received', 'credited', 'umepokea', 'imepokelewa', 'imeingizwa',
                  'umelipwa')),
    ('paid', ('paid', 'pay', 'payment', 'sent', 'send', 'umelipa', 'umelipia', 'umetuma',
              'malipo', 'lipa', 'amount', 'kiasi', 'umenunua', 'debited', 'imetozwa',
              'imekatwa', 'purchased', 'bought', 'jumla', 'total')),
)

# Words that say a line is about moving money rather than about goods. Used only by
# `is_payment_narration`, and deliberately not shared with utils/products.PAYMENT_WORDS,
# which is a much broader list serving a much more forgiving question.
_PAYMENT_VERBS = (
    'paid', 'pay', 'payment', 'sent', 'send', 'transfer', 'transferred', 'received',
    'umelipa', 'umelipia', 'umetuma', 'umepokea', 'malipo', 'lipa', 'amelipa',
    'umenunua', 'purchase', 'purchased', 'withdrawn', 'deposited', 'debited', 'credited',
)

_MONEY_PATTERNS = (
    # Currency first: 'TZS 55,000', 'Tsh5,000.00'.
    re.compile(rf'(?P<currency>{_CURRENCY})\.?\s*(?P<value>{_NUMBER})(?![\d,])'),
    # Currency after: '55000 TZS'. Both orders occur, often in the same message.
    re.compile(rf'(?<![\d,.])(?P<value>{_NUMBER})\s*(?P<currency>{_CURRENCY})\b'),
)

# A figure with no currency beside it, which is how a balance is usually printed.
_BARE_MONEY_PATTERN = re.compile(rf'(?<![\d,.])(?P<value>{_BARE_NUMBER})(?![\d,])')

_MONTHS = {m: i for i, m in enumerate(
    ('jan', 'feb', 'mar', 'apr', 'may', 'jun', 'jul', 'aug', 'sep', 'oct', 'nov', 'dec'), 1)}

_DATE_PATTERNS = (
    ('iso', re.compile(r'(?<!\d)(?P<y>\d{4})-(?P<m>\d{1,2})-(?P<d>\d{1,2})(?!\d)')),
    ('named', re.compile(r'(?i)(?<!\d)(?P<d>\d{1,2})\s*(?:st|nd|rd|th)?\s+'
                         r'(?P<mon>jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)[a-z]*\.?,?\s+'
                         r'(?P<y>\d{4})(?!\d)')),
    # The separator is backreferenced so '17.1217' inside a transaction id cannot read as
    # a date, and the lookbehind stops a match starting in the middle of a longer number.
    ('dmy', re.compile(r'(?<!\d)(?P<d>\d{1,2})(?P<sep>[-/.])(?P<m>\d{1,2})(?P=sep)(?P<y>\d{2,4})(?!\d)')),
)

_TIME_PATTERN = re.compile(r'(?<!\d)(?P<H>\d{1,2}):(?P<M>\d{2})(?::(?P<S>\d{2}))?(?!\d)')

_PHONE_PATTERN = re.compile(r'(?<![\d+])(?:\+?255|0)[67]\d{8}(?!\d)')
_TIN_PATTERN = re.compile(r'(?<!\d)\d{3}-\d{3}-\d{3}(?!\d)')

# A run of digits long enough to be an account or a reference rather than a quantity or a
# price. `is_payment_narration` uses it to recognise a payment narration that carries no
# currency marker at all - 'Payment to X - 994944252324 - Y', which is what a model
# writes into a line item when the record gave it nothing else to write.
_LONG_DIGITS = re.compile(r'(?<!\d)\d{6,}(?!\d)')


TEMPLATES = (
    # A bill paid through a wallet or a bank, for somebody's account with a biller:
    # '<amount> for <holder> - <account> - <biller>'. Written on the shape, so it reads
    # every biller on this gateway rather than the one that prompted it.
    #
    # The account number is typed `account_no` and not `control_number` deliberately. A
    # GePG control number is issued for one bill and identifies it; a subscriber's account
    # with a utility is billed every month and identifies nothing but the subscriber. The
    # kind carries that distinction on its own, so rule R5 needs no per-template override.
    Template(
        name='bill_payment_three_part',
        channel='bill_payment',
        pattern=re.compile(
            rf'(?i)(?:paid|umelipa|umelipia)\s+(?:{_CURRENCY})?\.?\s*'
            rf'(?P<amount>{_NUMBER})\s*(?:{_CURRENCY})?\s+(?:for|kwa|ya)\s+'
            rf'(?P<holder>{_NOT_DASH}+?)\s*{_DASH}\s*(?P<account>\d{{6,}})\s*{_DASH}\s*'
            rf'(?P<biller>[^.\n]+?)\s*(?:[.\n]|$)'),
        roles={'holder': 'account_holder', 'biller': 'payee'},
        references={'account': 'account_no'},
        amounts={'amount': 'paid'},
    ),

    # A wallet payment to a named merchant: 'Umelipa Tsh10,000.00 kwa DUKA LA MAJI'.
    Template(
        name='mobile_money_pay_merchant',
        channel='mobile_money',
        pattern=re.compile(
            rf'(?i)(?:umelipa|umelipia|you\s+have\s+paid|paid)\s+(?:{_CURRENCY})\.?\s*'
            rf'(?P<amount>{_NUMBER})\s+(?:kwa|to)\s+(?P<payee>[A-Za-z][^,.\n]{{2,80}}?)'
            rf'\s*(?:[,.\n]|\s+{_DASH}\s|$)'),
        roles={'payee': 'payee'},
        amounts={'amount': 'paid'},
    ),

    # A wallet transfer to a person: 'Umetuma Tsh5,000.00 kwa JOHN DOE - 0754123456'.
    Template(
        name='mobile_money_send',
        channel='mobile_money',
        pattern=re.compile(
            rf'(?i)(?:umetuma|you\s+have\s+sent|sent)\s+(?:{_CURRENCY})\.?\s*'
            rf'(?P<amount>{_NUMBER})\s+(?:kwa|to)\s+(?P<payee>[A-Za-z][^,.\n]{{2,80}}?)'
            rf'\s*(?:[,.\n]|\s+{_DASH}\s|$)'),
        roles={'payee': 'payee'},
        amounts={'amount': 'paid'},
    ),

    # Money arriving, which is not a purchase. Recorded so the amount is not read as one:
    # 'Umepokea Tsh20,000.00 kutoka JANE DOE'.
    Template(
        name='mobile_money_receive',
        channel='mobile_money',
        pattern=re.compile(
            rf'(?i)(?:umepokea|you\s+have\s+received|received)\s+(?:{_CURRENCY})\.?\s*'
            rf'(?P<amount>{_NUMBER})\s+(?:kutoka|from)\s+(?P<payer>[A-Za-z][^,.\n]{{2,80}}?)'
            rf'\s*(?:[,.\n]|\s+{_DASH}\s|$)'),
        roles={'payer': 'payer'},
        amounts={'amount': 'received'},
    ),

    # A bank taking money out of an account. The account number and the reference are
    # labelled in every one of these, so the generic scanners collect them and the
    # template only has to say which figure is the payment.
    Template(
        name='bank_debit_alert',
        channel='bank',
        pattern=re.compile(
            rf'(?i)\b(?:a/c|akaunti|account)\b[^\n]{{0,60}}?'
            rf'\b(?:debited|imetozwa|imekatwa|withdrawn)\b[^\n]{{0,20}}?'
            rf'(?:{_CURRENCY})?\.?\s*(?P<amount>{_NUMBER})'),
        amounts={'amount': 'paid'},
    ),

    Template(
        name='bank_credit_alert',
        channel='bank',
        pattern=re.compile(
            rf'(?i)\b(?:a/c|akaunti|account)\b[^\n]{{0,60}}?'
            rf'\b(?:credited|imepokelewa|imeingizwa)\b[^\n]{{0,20}}?'
            rf'(?:{_CURRENCY})?\.?\s*(?P<amount>{_NUMBER})'),
        amounts={'amount': 'received'},
    ),

    # Electricity, in two entries rather than one alternation. A single pattern would
    # match whichever marker came first in the message, so 'Umenunua LUKU TANESCO' would
    # match on LUKU and leave the payee uncaptured - alternation picks the earliest
    # position, not the most informative branch. Trying the naming form first is what
    # makes the more useful reading win.
    #
    # Neither entry asserts TANESCO from nowhere. A party with no span would be a name
    # this module made up, and the span invariant exists precisely to make that
    # impossible; where the message does not name the utility, the meter and the token
    # still arrive through the generic scanner and the prompt tells the model whose they
    # are.
    Template(
        name='luku_token_named',
        channel='utility',
        pattern=re.compile(r'(?i)\b(?P<payee>TANESCO)\b'),
        roles={'payee': 'payee'},
    ),
    Template(
        name='luku_token',
        channel='utility',
        pattern=re.compile(r'(?i)\bLUKU\b'),
    ),

    # A government payment against a control number. The payee arrives through the
    # labelled-party scanner ('Taasisi: ...'), which every GePG confirmation carries.
    Template(
        name='gepg_payment',
        channel='government',
        pattern=re.compile(r'(?i)\b(?:namba\s*ya\s*kudhibiti|control\s*(?:no|number)|GePG)\b'),
    ),

    Template(
        name='airtime_topup',
        channel='telecom',
        pattern=re.compile(r'(?i)\b(?:umenunua|purchased|bought|top(?:ped)?\s*[- ]?up)\b'
                           r'[^.\n]{0,40}?'
                           r'\b(?:muda\s*wa\s*maongezi|airtime|vocha|bando|bundle|bundles)\b'),
    ),

    # A till payment where the merchant is identified by number rather than by name.
    Template(
        name='merchant_till_payment',
        channel='merchant',
        pattern=re.compile(r'(?i)\b(?:lipa\s*namba|till\s*(?:no|number)?|merchant\s*(?:no|number))\b'),
    ),
)


def scan(text, *, trust='pasted'):
    """
    Everything a machine can read out of one written record.

    A template is tried first, because it is the only thing that can say which party is
    which. Whatever it did not claim is then filled in by the generic scanners, which
    know nothing about formats and read labels and figures wherever they sit. So a
    recognised format gets roles and a shape; an unrecognised one still gives up its
    amounts, its identifiers and its timestamp, and leaves `parties` empty rather than
    guessing - an unroled name promoted to vendor is the exact failure this module was
    written to stop.
    """
    text = _clean(text)
    result = RecordScan(text=text, trust=trust)
    if not text:
        return result

    matched = _match_template(text)
    if matched:
        template, match = matched
        result.template = template.name
        result.channel = template.channel
        _read_template(result, template, match, text)

    _read_labelled_parties(result, text)
    _read_labelled_references(result, text)
    _read_amounts(result, text)
    _read_timestamp(result, text)

    result.phones = [Span(m.group(0), m.start(), m.end()) for m in _PHONE_PATTERN.finditer(text)]
    result.tins = [Span(m.group(0), m.start(), m.end()) for m in _TIN_PATTERN.finditer(text)]
    return result


def _clean(text):
    """
    The record as characters this module can reason about.

    The one normalisation that is about encoding rather than about meaning: NFKC, which
    folds the full-width digits, the ligatures and the non-breaking spaces an SMS gateway
    occasionally emits into the characters the patterns below are written against.

    What it leaves alone matters as much. An en dash stays an en dash - that is the
    character standing between the three parties in the message this module was written
    for, and every pattern that separates parties accepts it explicitly. Nothing else is
    touched: the text has to stay the text, or the spans stop pointing at what a reader
    sees.
    """
    if not text:
        return ''
    return unicodedata.normalize('NFKC', str(text))


def _match_template(text):
    """The first template that reads this record, or None. Order in TEMPLATES is priority."""
    for template in TEMPLATES:
        match = template.pattern.search(text)
        if match:
            return template, match
    return None


def _span(text, start, end):
    """A span with the punctuation and whitespace at its edges trimmed off."""
    trim = ' \t\r\n.,;:'
    while start < end and text[start] in trim:
        start += 1
    while end > start and text[end - 1] in trim:
        end -= 1
    return Span(text[start:end], start, end)


def _group_span(text, match, group):
    """
    The span of a named group, or None where the pattern left it unmatched.

    A template may name a group its pattern only sometimes captures - an alternation, an
    optional tail - so an absent group is an ordinary outcome here rather than an error.
    """
    if group not in match.re.groupindex or match.group(group) is None:
        return None
    start, end = match.span(group)
    span = _span(text, start, end)
    return span if span.text else None


def _read_template(result, template, match, text):
    """Turns the template's named groups into parties, references and amounts."""
    for group, role in template.roles.items():
        span = _group_span(text, match, group)
        if span and role in ROLES:
            result.parties.append(Party(role=role, name=span.text, span=span))

    for group, kind in template.references.items():
        span = _group_span(text, match, group)
        if not span or kind not in REFERENCE_KINDS:
            continue
        normalised = normalise_reference(span.text)
        if normalised:
            result.references.append(Reference(
                kind=kind, value=span.text, normalised=normalised,
                label=None, span=span))

    for group, role in template.amounts.items():
        span = _group_span(text, match, group)
        if not span or role not in AMOUNT_ROLES:
            continue
        cents = to_cents(span.text.replace(',', ''))
        if cents is not None:
            result.amounts.append(Amount(
                cents=cents, currency=_currency_near(text, span.start), role=role, span=span))

    if template.when:
        span = _group_span(text, match, template.when)
        if span:
            moment, _, confidence, has_time = _parse_moment(span.text)
            if moment:
                result.occurred_at = moment
                result.occurred_at_span = span
                result.occurred_at_confidence = confidence
                result.occurred_at_has_time = has_time


def _overlaps(span, taken):
    return any(span.start < end and start < span.end for start, end in taken)


def _read_labelled_parties(result, text):
    """Parties a message names outright - 'Taasisi: TANZANIA REVENUE AUTHORITY'."""
    taken = [(p.span.start, p.span.end) for p in result.parties]
    for role, pattern in _PARTY_PATTERNS:
        if result.party(role):
            continue
        for match in pattern.finditer(text):
            span = _group_span(text, match, 'value')
            if not span or _overlaps(span, taken):
                continue
            result.parties.append(Party(role=role, name=span.text, span=span, confidence=0.9))
            taken.append((span.start, span.end))
            break


def _read_labelled_references(result, text):
    """
    Identifiers, typed by the word printed beside them.

    Nothing is inferred from the shape of the value, because the shapes do not separate:
    a control number and an account number are both twelve digits, and a transaction id
    and a receipt number are both a run of letters and digits. The label is the evidence,
    so a value with no label is left for the model rather than typed by guesswork.
    """
    taken = [(r.span.start, r.span.end) for r in result.references]
    taken += [(p.span.start, p.span.end) for p in result.parties]
    for kind, pattern in _REFERENCE_PATTERNS:
        for match in pattern.finditer(text):
            span = _group_span(text, match, 'value')
            if not span or _overlaps(span, taken):
                continue
            normalised = normalise_reference(span.text)
            if not normalised:
                continue
            label = _group_span(text, match, 'label')
            result.references.append(Reference(
                kind=kind, value=span.text, normalised=normalised,
                label=label.text if label else None, span=span))
            taken.append((span.start, span.end))


def _currency_near(text, start):
    """The currency written beside a figure, defaulting to shillings."""
    window = text[max(0, start - 12):start + 12]
    match = re.search(_CURRENCY, window)
    return 'TZS' if not match else ('TZS' if match.group(0).upper() in ('TZS', 'TSH', 'SH')
                                    else match.group(0).upper())


def _role_near(text, start):
    """
    What the words immediately before a figure say the figure is.

    The single most valuable rule in this module. 'New Balance  44,202.04' and
    'You have paid 55000 TZS' are the same shape of number a few words apart, and only
    the words say that one of them is the purchase and the other is what was left in the
    wallet afterwards. A model handed both without that distinction stored the balance as
    the total.
    """
    window = normalise_description(text[max(0, start - _LABEL_WINDOW):start])
    for role, words in _AMOUNT_ROLE_WORDS:
        if any(re.search(rf'(?<![a-z0-9]){re.escape(word)}(?![a-z0-9])', window)
               for word in words):
            return role
    return 'unknown'


def _read_amounts(result, text):
    """
    Every figure of money in the record, each one labelled with what it is a figure of.

    Currency-marked figures first, because they are unambiguous. Bare figures are read
    only when they carry a thousands separator or a two-decimal tail - that is what
    separates a balance printed as '44,202.04' from an account number printed as
    '994944252324', and without it every reference in the record becomes an amount.
    """
    taken = [(a.span.start, a.span.end) for a in result.amounts]
    taken += [(r.span.start, r.span.end) for r in result.references]

    def _add(span, confidence):
        if _overlaps(span, taken):
            return
        cents = to_cents(span.text.replace(',', ''))
        if cents is None:
            return
        result.amounts.append(Amount(
            cents=cents, currency=_currency_near(text, span.start),
            role=_role_near(text, span.start), span=span, confidence=confidence))
        taken.append((span.start, span.end))

    for pattern in _MONEY_PATTERNS:
        for match in pattern.finditer(text):
            span = _group_span(text, match, 'value')
            if span:
                _add(span, 1.0)

    for match in _BARE_MONEY_PATTERN.finditer(text):
        span = _group_span(text, match, 'value')
        if span:
            _add(span, 0.8)

    result.amounts.sort(key=lambda a: a.span.start)

    # A record with exactly one figure and no word to label it is naming the amount. Said
    # at low confidence, because it is the one inference in this scanner: reconcile will
    # let a model's total stand against it rather than overrule it.
    if not any(a.role in ('paid', 'received') for a in result.amounts):
        unknown = [a for a in result.amounts if a.role == 'unknown']
        if len(unknown) == 1:
            index = result.amounts.index(unknown[0])
            result.amounts[index] = Amount(
                cents=unknown[0].cents, currency=unknown[0].currency, role='paid',
                span=unknown[0].span, confidence=0.6)


def _parse_moment(fragment):
    """
    A date, and the time beside it if there is one.

    Returns (datetime, (start, end), confidence, has_time). `has_time` is separate from
    the datetime because a date with no time parses to midnight, and midnight stored as
    the time of sale is a figure nobody printed.
    """
    for name, pattern in _DATE_PATTERNS:
        match = pattern.search(fragment)
        if not match:
            continue
        try:
            if name == 'named':
                day = int(match.group('d'))
                month = _MONTHS[match.group('mon').lower()[:3]]
                year = int(match.group('y'))
                confidence = 1.0
            elif name == 'iso':
                year, month, day = (int(match.group(g)) for g in ('y', 'm', 'd'))
                confidence = 1.0
            else:
                day, month = int(match.group('d')), int(match.group('m'))
                year = int(match.group('y'))
                if year < 100:
                    year += 2000
                # Day-first is the Tanzanian convention and is what these gateways write.
                # Where both readings are possible the date is still returned - refusing
                # it would lose a timestamp that is right five times in six - but it is
                # marked, so a caller can decline to overrule a model on it.
                confidence = 0.7 if month <= 12 and day <= 12 else 1.0
            moment = datetime(year, month, day)
        except (ValueError, KeyError):
            continue

        start, end = match.span()
        # A time is part of this timestamp only if it follows the date almost immediately;
        # further away it belongs to another sentence.
        has_time = False
        tail = fragment[end:end + 14]
        time_match = _TIME_PATTERN.search(tail)
        if time_match:
            try:
                moment = moment.replace(
                    hour=int(time_match.group('H')), minute=int(time_match.group('M')),
                    second=int(time_match.group('S') or 0))
                end += time_match.end()
                has_time = True
            except ValueError:
                pass
        return moment, (start, end), confidence, has_time
    return None, None, 1.0, False


def _read_timestamp(result, text):
    """When the payment happened, where the record says so."""
    if result.occurred_at:
        return
    moment, bounds, confidence, has_time = _parse_moment(text)
    if not moment:
        return
    result.occurred_at = moment
    result.occurred_at_span = _span(text, bounds[0], bounds[1])
    result.occurred_at_confidence = confidence
    result.occurred_at_has_time = has_time


def is_payment_narration(text):
    """
    Whether this line is about moving money rather than about what was bought.

    Written for utils/compliance, which reads line-item descriptions through keyword rules
    that were designed for the words on a receipt. On a record that never was a receipt
    the description is often the payment sentence itself, and the rules then read the
    payee's own trading name as a description of a service:

        'Payment to KELSIA BUSINESS CONSULTANCY LIMITED - 994944252324 - TANZANIA
         TELECOMMUNICATION CORPORATION'

    contains the word 'consultancy', so the withholding rule offered 2,750 TZS to withhold
    on a telephone bill. Nobody bought consultancy; a company with the word in its name was
    paid for internet.

    Deliberately strict, and deliberately not utils.products.is_opaque, which answers a
    more forgiving question for a more forgiving purpose and returns True for 'Payment for
    cleaning services'. Suppressing a genuine withholding finding is a false negative on
    real money and is worse than the false positive being fixed here, so a line qualifies
    only on evidence that it is a payment record: a known format, money beside a payment
    word, a labelled identifier, or a payment word beside an account-length number.
    """
    text = _clean(text)
    if not text.strip():
        return False

    if _match_template(text):
        return True

    lowered = normalise_description(text)
    has_verb = any(re.search(rf'(?<![a-z0-9]){re.escape(word)}(?![a-z0-9])', lowered)
                   for word in _PAYMENT_VERBS)
    has_money = any(pattern.search(text) for pattern in _MONEY_PATTERNS)
    labelled = [(kind, pattern.search(text)) for kind, pattern in _REFERENCE_PATTERNS]
    has_reference = any(
        match and normalise_reference(match.group('value'))
        for kind, match in labelled
        if kind in IDENTIFYING or kind in ('account_no', 'meter_no', 'merchant_no'))

    if has_money and (has_verb or has_reference):
        return True
    if has_reference:
        return True
    # No currency marker anywhere, but a payment word beside a number far too long to be a
    # quantity or a price. This is the shape a model writes into a line item when the
    # record gave it nothing to describe - and it is the line that produced the finding
    # in the docstring above.
    return bool(has_verb and _LONG_DIGITS.search(text))


def _money(cents):
    """Cents back to the plain number a transcription carries. Floats, because the answer
    is JSON that gets stored on the submission and Decimal will not serialise."""
    return None if cents is None else round(cents / 100, 2)


def _same(left, right):
    """Whether two field values are the same answer written two ways."""
    if left is None or right is None:
        return left is right
    if isinstance(left, str) and isinstance(right, str):
        return normalise_description(left) == normalise_description(right)
    return left == right


def reconcile(data, scan):
    """
    What the model returned, corrected against what the record actually says.

    The model is good at the question it is uniquely able to answer - what this purchase
    was for - and unreliable at the ones a regex answers perfectly. So each rule below
    takes one field the scanner can speak to and settles it, and every change is recorded
    rather than applied silently: a reader on the receipt page is told the vendor was
    replaced and why, because a correction nobody can see is indistinguishable from a bug.

    Two of the rules are not corrections at all but containment checks. R3 and R5 ask
    whether a figure or a reference the model returned is anywhere in the record, and
    remove it when it is not. Those two are the ones that matter most, because a
    hallucinated reference does not merely misreport this receipt - `receipt_number` is
    what utils/fingerprint builds a duplicate identity out of, so an invented one quietly
    decides which future purchases get filed as copies of this one.

    Returns (data, adjustments). `data` is a new dict; the input is not modified.
    """
    data = dict(data or {})
    if scan is None or not scan.text:
        return data, []

    adjustments = []

    def _record(field_name, before, after, rule, why):
        adjustments.append({'field': field_name, 'from': before, 'to': after,
                            'rule': rule, 'why': why})

    def _set(field_name, value, rule, why):
        before = data.get(field_name)
        if _same(before, value):
            return False
        data[field_name] = value
        _record(field_name, before, value, rule, why)
        return True

    payee, holder = scan.payee(), scan.account_holder()

    # R1. The vendor is the party the money went to. Only ever asserted from a template,
    # because only a template knows which name is which - the generic scanners leave
    # `parties` empty rather than promote a name they cannot place.
    if payee:
        why = 'the scanner read the party paid straight out of the record'
        if holder and _same(data.get('vendor_name'), holder.name):
            why = ('the model returned the account holder as the vendor; the party paid '
                   'is named separately in the record')
        _set('vendor_name', payee.name, 'R1', why)

    # R2. The account holder is the customer - very often the business submitting this.
    # Never overwrites a customer the model read off an explicit line of its own.
    if holder and not (data.get('customer_name') or '').strip():
        _set('customer_name', holder.name, 'R2',
             'the record names whose account was paid, which is the customer, not the supplier')

    _reconcile_total(data, scan, _set, _record)
    _reconcile_tax(data, scan, _set)
    _reconcile_reference(data, scan, _set)
    _reconcile_moment(data, scan, _set)
    _reconcile_items(data, scan, _record)

    # R8. A TIN the record does not contain was not read off it.
    tin = (data.get('vendor_tin') or '').strip()
    if tin and not scan.tins and not scan.contains_reference(tin):
        _set('vendor_tin', None, 'R8', 'no TIN appears anywhere in the record')

    return data, adjustments


def _reconcile_total(data, scan, _set, _record):
    """
    R3. The total has to be a figure the record contains.

    The balance case is checked first and separately because it is the specific mistake
    this rule exists for: a wallet balance is printed beside the amount, looks exactly
    like it, and is the number a model reaches for when the amount is written without a
    thousands separator and the balance is written with one.
    """
    total = to_cents(data.get('total_amount'))
    paid, balance = scan.paid(), scan.balance_after()

    if total is None:
        if paid:
            _set('total_amount', _money(paid.cents), 'R3',
                 'the model gave no total; the record states the amount paid')
        return

    if balance and total == balance.cents and paid and paid.cents != balance.cents:
        _set('total_amount', _money(paid.cents), 'R3',
             'the total matched the wallet balance after the payment, not the amount paid')
        return

    if scan.mentions_amount(total):
        return

    if paid:
        _set('total_amount', _money(paid.cents), 'R3',
             'the total the model gave appears nowhere in the record')
    else:
        # Nothing to replace it with. Left standing and flagged rather than deleted: a
        # receipt with no total at all is worse than one whose total wants checking.
        _record('total_amount', data.get('total_amount'), data.get('total_amount'), 'R3',
                'the total appears nowhere in the record and the record states no amount '
                'to put in its place')


def _reconcile_tax(data, scan, _set):
    """
    R4. Tax that was never charged cannot be recovered.

    These records almost never state tax, and a model asked for a VAT figure will compute
    one - 18% of the total, which is arithmetic rather than transcription. Stored, it
    becomes input VAT the business is told it can claim with no tax invoice behind it.
    """
    vat = to_cents(data.get('vat_amount'))
    if not vat:
        return
    if scan.amount('tax') or scan.mentions_amount(vat):
        return
    _set('vat_amount', None, 'R4',
         'the record states no tax; the figure was computed rather than read')


def _reconcile_reference(data, scan, _set):
    """
    R5. The receipt number must be this payment's own reference, and must be in the record.

    Three ways it goes wrong, in the order they are checked. The model returns an account
    or meter number, which repeats on every payment the customer makes and would file each
    of them as a duplicate of the first. It returns a reference that is in the record but
    untyped, which is fine and is kept. Or it returns one that is in the record nowhere at
    all, which is kept out of the one field where being wrong compounds.
    """
    value = (data.get('receipt_number') or '').strip()
    primary = scan.primary_reference()
    if not value:
        if primary:
            _set('receipt_number', primary.value, 'R5',
                 f"the record labels this the {primary.kind.replace('_', ' ')}")
        return

    normalised = normalise_reference(value)
    matched = next((r for r in scan.references if r.normalised == normalised), None)

    if matched and matched.kind in IDENTIFYING:
        return
    if matched:
        replacement = primary.value if primary else None
        _set('receipt_number', replacement, 'R5',
             f"the {matched.kind.replace('_', ' ')} identifies the customer, not this "
             'payment, and would make every later payment on it look like a duplicate')
        return
    if scan.contains_reference(value):
        return

    replacement = primary.value if primary else None
    _set('receipt_number', replacement, 'R5',
         'the reference the model gave appears nowhere in the record')


def _reconcile_moment(data, scan, _set):
    """
    R6. The timestamp the record prints beats the one the model typed.

    An ambiguous date - both halves twelve or under, so day-first and month-first both
    read - is not used to overrule anything. It still fills an empty field, because a date
    that is right five times in six beats no date at all, but it is not evidence enough to
    contradict a model that may have had a reason.
    """
    if not scan.occurred_at:
        return
    confident = scan.occurred_at_confidence >= 0.8

    on_date = scan.occurred_at.date().isoformat()
    if confident or not (data.get('receipt_date') or '').strip():
        _set('receipt_date', on_date, 'R6', 'the date is printed in the record')

    if not scan.occurred_at_has_time:
        return
    at_time = scan.occurred_at.time().isoformat()
    if confident or not (data.get('receipt_time') or '').strip():
        _set('receipt_time', at_time, 'R6', 'the time is printed in the record')


def _reconcile_items(data, scan, _record):
    """
    R7. A line's amount has to be in the record too, for the same reason the total does.

    Descriptions are left exactly as the model wrote them. Rewriting one would be this
    module inventing prose, and the damage a payment sentence does downstream is handled
    where it is done - utils/compliance, through `is_payment_narration`.
    """
    items = data.get('items')
    if not isinstance(items, list):
        return
    balance = scan.balance_after()

    for index, item in enumerate(items, start=1):
        if not isinstance(item, dict) or item.get('amount') is None:
            continue
        cents = to_cents(item.get('amount'))
        if cents is None:
            continue
        if balance and cents == balance.cents:
            why = 'the line was priced at the wallet balance after the payment'
        elif not scan.mentions_amount(cents):
            why = 'the amount appears nowhere in the record'
        else:
            continue
        before = item['amount']
        item['amount'] = None
        _record(f'items[{index}].amount', before, None, 'R7', why)


def anchor_block(scan):
    """
    The scan as a block of facts to hand the model, or '' when there is nothing to say.

    Written the way utils/tra_parser's facts are written for the judgment prompt, and for
    the same reason: a model told that a figure is already established stops deriving it,
    and a model shown which party is which stops choosing between them. The last line
    matters as much as the rest - a list of what was found reads, without it, as a list of
    what to look for, and the model fills in the gaps.
    """
    if scan is None or not scan.text:
        return ''

    lines = []
    paid = scan.paid()
    if paid:
        lines.append(f'- Paid: {paid.cents / 100:,.2f} {paid.currency}  (from "{paid.span.text}")')
    received = scan.amount('received')
    if received:
        lines.append(f'- Received, not paid: {received.cents / 100:,.2f} {received.currency}  '
                     f'(from "{received.span.text}")')
    balance = scan.balance_after()
    if balance:
        lines.append(f'- Balance after the payment: {balance.cents / 100:,.2f}  '
                     f'(from "{balance.span.text}") - the payer\'s balance afterwards. '
                     'It is not an amount on this document.')
    for role, label in (('fee', 'Charge or fee'), ('tax', 'Tax stated in the record')):
        amount = scan.amount(role)
        if amount:
            lines.append(f'- {label}: {amount.cents / 100:,.2f}  (from "{amount.span.text}")')

    payee = scan.payee()
    if payee:
        lines.append(f'- Payee, i.e. the vendor: {payee.name}')
    holder = scan.account_holder()
    if holder:
        lines.append(f'- Account holder, i.e. the customer: {holder.name}')
    payer = scan.party('payer')
    if payer:
        lines.append(f'- Payer: {payer.name}')

    for reference in scan.references:
        kind = reference.kind.replace('_', ' ')
        labelled = f' (labelled "{reference.label}")' if reference.label else ''
        lines.append(f'- {kind}: {reference.value}{labelled}')

    if scan.occurred_at:
        when = (scan.occurred_at.strftime('%Y-%m-%d %H:%M:%S') if scan.occurred_at_has_time
                else scan.occurred_at.strftime('%Y-%m-%d'))
        lines.append(f'- Paid at: {when}')

    if not lines:
        return ''

    return (
        '\n\nThe record has already been read by a scanner. These are the characters in '
        'the text, not an interpretation of them - use them and do not contradict them:\n'
        + '\n'.join(lines)
        + '\nAnything not listed above was not found in the text. Do not invent it.'
    )
