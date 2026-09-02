# utils/products.py
"""
What was actually bought, as one thing across every way of writing it.

A receipt says what was paid for only when somebody sold you a receipt. A great deal of
what an organisation buys arrives as a mobile money line reading `LIPA JACLINE NGILISHO
MOLLEL`, which names the person paid and says nothing whatever about the purchase - and
beside it, typed on a phone in the two seconds the payer could spare, `Mayai x 6`.

That note is the only record of what the money bought, and it is written the way people
write: in Swahili one week and English the next, with the count before the noun or after
it, misspelled, abbreviated, or trailing a unit. Left as text it produces a catalogue
where eggs are six different products and none of them has a price history.

So the text is reduced to a key here, and the key is what the catalogue is built on:

  * `parse_entry` pulls a count off a name - 'Mayai x 6', '6 mayai', 'mayai 6pcs' are one
    product bought six times over, not three products.
  * `normalise` strips it to the letters that identify it, so 'Mayai', 'MAYAI.' and
    'mayai ' collide on their own.
  * `best_match` catches the typo that normalising cannot - 'mayay' against 'mayai' -
    and refuses to guess on words too short for a guess to be safe.
  * `SEED_ALIASES` carries the one thing string distance can never do: 'mayai' and
    'eggs' are the same product and look nothing alike. The list is deliberately short -
    the common goods a Tanzanian business buys weekly - because past that it is the
    model's job, and whatever the model works out is written into the alias table and
    never asked again.

Nothing here decides tax. It decides identity, which is what lets utils/analytics say
that eggs cost more this month than last, no matter which language the buyer was
thinking in when they typed the note.
"""
import re
from difflib import SequenceMatcher

from utils.classify import normalise_description

# Units that count things, and units that measure them. The distinction matters when a
# number sits at the end of a name: 'mayai 6' is six eggs, and '6' is a quantity, but
# 'coca cola 500ml' is one product whose name happens to end in its pack size. Only the
# counting units below let a trailing number be read as a quantity.
COUNT_UNITS = {
    'pc', 'pcs', 'piece', 'pieces', 'unit', 'units', 'pkt', 'pkts', 'packet', 'packets',
    'pack', 'packs', 'box', 'boxes', 'carton', 'cartons', 'crate', 'crates', 'tray',
    'trays', 'bag', 'bags', 'bottle', 'bottles', 'tin', 'tins', 'dozen', 'dz', 'sachet',
    'sachets', 'roll', 'rolls', 'bunch', 'bunches', 'set', 'sets', 'pair', 'pairs',
    # Swahili, as they are actually typed into the note box.
    'kipande', 'vipande', 'mfuko', 'mifuko', 'chupa', 'katoni', 'sanduku', 'trei',
}

MEASURE_UNITS = {
    'kg', 'kgs', 'g', 'gm', 'gms', 'gram', 'grams', 'kilo', 'kilos', 'kilogram',
    'kilograms', 'l', 'lt', 'ltr', 'ltrs', 'litre', 'litres', 'liter', 'liters', 'ml',
    'm', 'cm', 'mm', 'km', 'kwh', 'unit',
}

UNITS = COUNT_UNITS | MEASURE_UNITS

# Words that are how the payment was made rather than what was bought. A line built out
# of nothing but these says nothing, which is precisely when the sender's note is the
# only description of the purchase that exists - see `is_opaque`.
PAYMENT_WORDS = {
    'lipa', 'malipo', 'payment', 'paid', 'pay', 'to', 'kwa', 'for', 'transfer',
    'transferred', 'sent', 'umetuma', 'imepokelewa', 'received', 'from', 'kutoka',
    'purchase', 'ununuzi', 'txn', 'transaction', 'ref', 'reference', 'summarized',
    'summarised', 'sale', 'sales', 'goods', 'bidhaa', 'item', 'items', 'misc',
    'miscellaneous', 'general', 'various', 'assorted', 'cash', 'pesa', 'tzs', 'tsh',
    'shs', 'amount', 'kiasi', 'mpesa', 'tigopesa', 'airtelmoney', 'halopesa', 'merchant',
    'agent', 'wakala', 'till', 'account', 'akaunti',
}

# The one hop string distance cannot make: two words for the same thing. Canonical name
# on the left, everything it has ever been called on the right. Kept small on purpose -
# these are the goods a small Tanzanian business buys most weeks, and the point is that
# the first receipt naming one lands in the right place before the catalogue has learned
# anything at all. Everything past this list is the model's job, and what the model
# concludes becomes a stored alias, so each new word is worked out exactly once.
SEED_ALIASES = {
    'Eggs': ('mayai', 'yai', 'egg'),
    'Bread': ('mkate', 'mikate'),
    'Milk': ('maziwa',),
    'Sugar': ('sukari',),
    'Salt': ('chumvi',),
    'Rice': ('mchele', 'wali'),
    'Maize flour': ('unga wa mahindi', 'unga', 'sembe'),
    'Wheat flour': ('unga wa ngano',),
    'Cooking oil': ('mafuta ya kupikia', 'mafuta ya kula', 'korie', 'salad oil'),
    'Beans': ('maharage', 'maharagwe'),
    'Meat': ('nyama',),
    'Chicken': ('kuku',),
    'Fish': ('samaki',),
    'Tomatoes': ('nyanya',),
    'Onions': ('vitunguu', 'kitunguu'),
    'Potatoes': ('viazi',),
    'Bananas': ('ndizi',),
    'Charcoal': ('mkaa',),
    'Firewood': ('kuni',),
    'Drinking water': ('maji ya kunywa', 'maji', 'water'),
    'Soda': ('soda', 'soft drink', 'softdrink'),
    'Tea': ('chai', 'majani ya chai'),
    'Coffee': ('kahawa',),
    'Soap': ('sabuni',),
    'Detergent': ('omo', 'washing powder'),
    'Petrol': ('petroli', 'unleaded', 'pms'),
    'Diesel': ('dizeli', 'ago', 'gasoil'),
    'Airtime': ('muda wa maongezi', 'vocha', 'voucher'),
    'Data bundle': ('bundle', 'bundles', 'kifurushi'),
    'Electricity token': ('luku', 'umeme', 'token'),
    'Stationery': ('vifaa vya ofisi',),
    'Printing paper': ('karatasi', 'a4 paper', 'ream'),
    'Transport': ('usafiri', 'nauli', 'bodaboda', 'boda'),
}

# alias key -> canonical name, built once. The canonical name is itself an alias of
# itself, so 'Eggs' typed into a note finds the same product 'mayai' does.
SEED_INDEX = {}
for _canonical, _aliases in SEED_ALIASES.items():
    _terms = (_aliases,) if isinstance(_aliases, str) else _aliases
    SEED_INDEX[normalise_description(_canonical)] = _canonical
    for _alias in _terms:
        SEED_INDEX[normalise_description(_alias)] = _canonical

# Below this many characters a near miss is not a typo, it is a different word: 'rice'
# and 'ride' are 75% the same string and nothing about them is the same purchase.
MIN_FUZZY_LENGTH = 5

# How alike two keys have to be before they are called one product. Set where a single
# mistyped letter in an ordinary word still matches ('mayay' against 'mayai' scores
# 0.8) and a different word does not.
FUZZY_CUTOFF = 0.80

# Separators a person uses when they list two things in one note: 'Mayai x 6, mkate 2'.
_SPLIT = re.compile(r'\s*(?:[,;/\n\r]|\+|\band\b|\bna\b|\bpamoja na\b)\s*', re.IGNORECASE)

_NUMBER = r'\d+(?:[.,]\d+)?'

# 'Mayai x 6', 'mayai *6', 'mayai @ 6' - the count written after the name.
_TRAILING_X = re.compile(rf'^(?P<name>.+?)\s*[x×*@]\s*(?P<qty>{_NUMBER})\s*(?P<unit>[a-z]*)\.?$',
                         re.IGNORECASE)

# '6 x mayai', '6x mayai', '6 pcs mayai', '2kg sukari', '6 mayai' - the count written
# first, which is how most people type it.
_LEADING = re.compile(rf'^(?P<qty>{_NUMBER})\s*(?P<unit>[a-z]*)\s*(?:[x×*@]|of|ya|za)?\s+(?P<name>.+)$',
                      re.IGNORECASE)

# The multiplier itself, which is not a unit and not part of the name: in '6 x mkate'
# the leading pattern offers 'x' as the unit, and reading it as a word would file the
# purchase under a product called 'x mkate'.
_MULTIPLIERS = {'x', 'X', '×', '*', '@', 'of', 'ya', 'za'}

# 'mayai 6', 'mayai 6 pcs' - a bare count at the end, accepted only under the guard in
# `parse_entry`, because a great many product names simply end in a number.
_TRAILING_BARE = re.compile(rf'^(?P<name>.+?)\s+(?P<qty>{_NUMBER})\s*(?P<unit>[a-z]*)\.?$',
                            re.IGNORECASE)


def normalise(text):
    """
    The key a product is stored and matched under.

    `utils.classify.normalise_description` already does the hard part - lower case,
    punctuation to spaces, whitespace squashed - and is reused rather than restated so a
    line item and a product name are reduced by exactly the same rule. What is added
    here is dropping a trailing unit word, so 'mayai' and 'mayai pcs' are one key.
    """
    tokens = normalise_description(text).split()
    while tokens and tokens[-1] in UNITS:
        tokens.pop()
    return ' '.join(tokens)


def is_opaque(description):
    """
    Whether a line describes the payment rather than the purchase.

    True for 'LIPA JACLINE NGILISHO MOLLEL', 'SUMMARIZED SALE', 'Payment to merchant' -
    lines that are on a great many mobile money records and every summarised EFD, and
    that tell a reader nothing about what was bought. It is the test for when the
    sender's note is not extra colour but the only description that exists, and so the
    only thing worth itemising the receipt from.

    Deliberately broad. Once a line carries a payment word at all, every other plain word
    on it is read as part of the payee's name - which is what 'LIPA JACLINE NGILISHO
    MOLLEL' needs, and which also means 'LIPA - MAYAI' comes back True even though it
    names the goods. Erring that way is right here: the cost is preferring the sender's
    own note to a line that might have been usable, and the note is the better description
    either way.

    It is the wrong test for anything that must not over-reach - utils/compliance decides
    real money off a description and uses utils.records.is_payment_narration, which is
    strict for exactly that reason. A line with no payment word in it at all is never
    opaque: 'MAYAI TREI' is left alone.
    """
    tokens = normalise_description(description).split()
    if not tokens:
        return True
    return all(
        token in PAYMENT_WORDS or token in UNITS or token.isdigit()
        # A person's name is boilerplate here too: 'LIPA JACLINE NGILISHO MOLLEL' is the
        # payee, and a payee is not a product. Anything long and alphabetic sitting
        # beside a payment word is treated as part of the payee, which is why this test
        # is applied only to lines that already contain one.
        or (token.isalpha() and any(word in PAYMENT_WORDS for word in tokens))
        for token in tokens
    )


def _to_quantity(raw):
    """The count as a float, or None if it is not one worth recording."""
    try:
        value = float((raw or '').replace(',', '.'))
    except (TypeError, ValueError):
        return None
    # A zero or negative count is a misparse, and a four-figure one is a price or a
    # meter number that happened to sit where a count goes.
    return value if 0 < value < 10000 else None


def parse_entry(text):
    """
    One written purchase read as (name, quantity, unit).

    'Mayai x 6' -> ('Mayai', 6.0, None). '2kg sukari' -> ('sukari', 2.0, 'kg'). 'Coca
    cola 500ml' -> ('Coca cola 500ml', None, None), because 500ml is the size of the
    bottle and not the number of them.

    Quantity is None whenever the text does not actually carry one. That is the whole
    discipline of this function: a missing count is recorded as missing, never as one,
    because a made-up count becomes a made-up unit price the moment anything divides by
    it - and unit price is what the catalogue exists to track.
    """
    cleaned = ' '.join((text or '').split()).strip(' .-')
    if not cleaned:
        return None, None, None

    for pattern in (_TRAILING_X, _LEADING):
        found = pattern.match(cleaned)
        if not found:
            continue
        unit = (found.group('unit') or '').lower() or None
        # A letter run that is not a unit belongs to the name: in '6 mayai' the regex
        # offers 'mayai' as the unit and the rest as the name, which is backwards.
        if unit and unit in _MULTIPLIERS:
            unit = None
        if unit and unit not in UNITS:
            if pattern is _LEADING:
                found = _LEADING.match(cleaned)
                name = f"{found.group('unit')} {found.group('name')}".strip()
                quantity = _to_quantity(found.group('qty'))
                if quantity is not None:
                    return name, quantity, None
            continue
        quantity = _to_quantity(found.group('qty'))
        name = (found.group('name') or '').strip(' .-')
        if quantity is not None and name:
            return name, quantity, unit

    found = _TRAILING_BARE.match(cleaned)
    if found:
        unit = (found.group('unit') or '').lower() or None
        # Only a counting unit, or none at all, turns a trailing number into a count.
        # 'sukari 2 kg' is two kilos and reads fine either way; 'coca cola 500 ml' is
        # one product, and reading 500 as its quantity would put a price of one two
        # hundredth of a bottle into the price history.
        quantity = _to_quantity(found.group('qty'))
        name = (found.group('name') or '').strip(' .-')
        if quantity is not None and name and not name[-1].isdigit() and _counts(quantity, unit):
            return name, quantity, unit

    return cleaned, None, None


def _counts(quantity, unit):
    """
    Whether a number written after a product name is how many were bought.

    No unit, or a counting one, and it is: 'mayai 6', 'mayai 6 pcs'. A measuring unit is
    the harder case, because the same shape of text means opposite things - 'sukari 2kg'
    is two kilos bought, and 'coca cola 500ml' is the size of one bottle. Split on the
    size of the number, which is what actually separates them in practice: nobody sells
    sugar in 500kg sacks to a corner shop, and no bottle is labelled 2ml. A pack size
    written in grams or millilitres runs to the hundreds; a count of kilos or litres
    almost never leaves single figures.
    """
    if unit is None or unit in COUNT_UNITS:
        return True
    if unit in {'g', 'gm', 'gms', 'gram', 'grams', 'ml', 'mm', 'cm'}:
        return False
    return quantity <= 20


def parse_note(note):
    """
    Everything a sender's note names, as a list of (name, quantity, unit).

    'Mayai x 6' is one entry. 'Mayai x 6, mkate 2' is two, because people list what they
    bought the way they would say it out loud. Fragments that carry no word at all - a
    stray number, an empty piece between two commas - are dropped rather than stored as
    a product called '6'.

    This is the floor under the model, not a replacement for it: it runs on the note
    whatever the model did or did not return, so a note the model ignored still itemises
    the receipt, and an outage costs the analysis rather than the itemisation.
    """
    entries = []
    for fragment in _SPLIT.split(note or ''):
        name, quantity, unit = parse_entry(fragment)
        if not name or not normalise(name):
            continue
        # A fragment that is only boilerplate ('payment', 'kwa') names nothing.
        if is_opaque(name):
            continue
        entries.append((name, quantity, unit))
    return entries


def best_match(key, candidates):
    """
    The candidate key this one is a misspelling of, or None.

    Deliberately hard to satisfy. Two guards do most of the work: nothing under
    MIN_FUZZY_LENGTH is matched at all, and the first letter has to agree - which is
    what separates a typo from a different word, since people mistype the middle of a
    word far more often than its start ('beans' and 'jeans' score 0.8 and are not the
    same purchase).

    Everything harder than this - that 'mayai' and 'eggs' are one product - is a
    question about meaning rather than spelling, and is answered by SEED_ALIASES or by
    the model, whose answer is then stored as an alias and never asked again.
    """
    if not key or len(key) < MIN_FUZZY_LENGTH:
        return None

    best, score = None, FUZZY_CUTOFF
    for candidate in candidates:
        if not candidate or len(candidate) < MIN_FUZZY_LENGTH or candidate[0] != key[0]:
            continue
        ratio = SequenceMatcher(None, key, candidate).ratio()
        if ratio >= score:
            best, score = candidate, ratio
    return best


def seeded_name(text):
    """
    The canonical name this text is a known synonym of, or None.

    Fuzzy as well as exact, because the seed list is what carries the very first receipt
    on a fresh instance - there is no catalogue behind it yet to catch a slip, and
    'mayay' typed once at a market stall should still be eggs.
    """
    key = normalise(text)
    if not key:
        return None
    if key in SEED_INDEX:
        return SEED_INDEX[key]
    near = best_match(key, SEED_INDEX)
    return SEED_INDEX[near] if near else None


def display_name(text):
    """
    A product name fit to show, from whatever was typed.

    Title case on lower-case input, left alone otherwise - so 'mayai' becomes 'Mayai'
    while 'LUKU' and 'A4 Paper' keep the capitals somebody meant.
    """
    name = ' '.join((text or '').split())
    if not name:
        return None
    return name[0].upper() + name[1:] if name.islower() else name
