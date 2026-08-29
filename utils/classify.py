# utils/classify.py
"""
What a line item *is*, decided from its printed description.

Three questions are asked of every line, and none of them needs a model:

  * Which expense category does it belong to?
  * Is it a capital asset rather than a running cost?
  * Is it a service that should have had withholding tax deducted?

The LLM is still asked for a narrative judgment (see utils/llm_processor), but the
answers here are deterministic, free, offline and identical on every run - which is
what a figure in a tax return has to be. Where the two disagree the receipt is worth
a human's attention, and the dashboard shows both.

Matching is on whole words against a lower-cased description, so 'FUEL' matches and
'refuelling' does not match 'oil'. Rules are ordered most-specific-first and the
first hit wins: 'generator diesel' is fuel, not a capital asset.

Swahili terms appear alongside the English ones because Tanzanian EFDs print both.
"""
import re

# Fixed set, so categories can be grouped and reported on. A free-text category is a
# category that is spelled three different ways by the end of the quarter. This list
# is also what the model's tool schema constrains it to (utils.llm_processor imports
# it from here), so both routes to a category land in the same bucket.
EXPENSE_CATEGORIES = [
    "fuel", "vehicle_running", "travel", "accommodation", "meals_entertainment",
    "utilities", "telecom", "rent", "office_supplies", "professional_services",
    "repairs_maintenance", "insurance", "bank_charges", "marketing",
    "inventory_purchases", "capital_asset", "staff_costs", "taxes_levies", "other",
]

# Above this, an item that also looks like an asset is treated as capital rather than
# a running cost. TRA sets no statutory de minimis, so this is a review trigger and
# not a determination - it decides what gets flagged, never what gets claimed.
CAPITAL_THRESHOLD_CENTS = 1_000_000_00

# Ordered (category, keywords). First matching category wins, so anything that must
# beat a broader rule is listed above it.
CATEGORY_RULES = (
    ('fuel', (
        'petrol', 'diesel', 'dizeli', 'gasoline', 'unleaded', 'kerosene', 'mafuta ya taa',
        'fuel', 'gasoil', 'ago', 'pms', 'lpg', 'gas cylinder',
    )),
    ('vehicle_running', (
        'tyre', 'tire', 'lubricant', 'engine oil', 'gear oil', 'brake fluid', 'coolant',
        'car wash', 'parking', 'toll', 'oil filter', 'air filter', 'spare part', 'spares',
        'battery', 'wiper', 'grease', 'service kit',
    )),
    ('travel', (
        'ticket', 'airfare', 'flight', 'bus fare', 'taxi', 'uber', 'bolt', 'transport',
        'usafiri', 'nauli', 'freight', 'courier', 'delivery charge', 'shipping', 'ferry',
        'mileage', 'per diem', 'posho',
    )),
    ('accommodation', (
        'hotel', 'lodge', 'guest house', 'guesthouse', 'accommodation', 'room charge',
        'bed and breakfast', 'malazi', 'apartment night',
    )),
    ('meals_entertainment', (
        'restaurant', 'lunch', 'dinner', 'breakfast', 'meal', 'chakula', 'food',
        'beverage', 'soda', 'beer', 'bia', 'wine', 'spirits', 'catering', 'refreshment',
        'snack', 'coffee', 'tea', 'chai', 'water bottle', 'entertainment', 'bar',
    )),
    ('utilities', (
        'electricity', 'umeme', 'luku', 'tanesco', 'water bill', 'maji', 'dawasa',
        'dawasco', 'sewerage', 'garbage', 'waste collection', 'taka',
    )),
    ('telecom', (
        'airtime', 'bundle', 'bundles', 'data bundle', 'internet', 'broadband', 'wifi',
        'vodacom', 'airtel', 'tigo', 'halotel', 'ttcl', 'zantel', 'sms', 'voucher',
        'sim card', 'muda wa maongezi',
    )),
    ('rent', (
        'rent', 'rental', 'kodi ya pango', 'pango', 'lease', 'office space',
        'warehouse rent', 'premises',
    )),
    ('insurance', (
        'insurance', 'bima', 'premium', 'cover note', 'third party cover', 'nhif', 'wcf',
    )),
    ('bank_charges', (
        'bank charge', 'ledger fee', 'commission', 'transfer fee', 'withdrawal charge',
        'atm', 'merchant fee', 'mobile money charge', 'transaction fee',
    )),
    ('professional_services', (
        'consultancy', 'consulting', 'audit', 'accounting', 'bookkeeping', 'legal',
        'advocate', 'lawyer', 'notary', 'advisory', 'training', 'seminar', 'workshop',
        'professional fee', 'service fee', 'ushauri', 'security service', 'guard',
        'cleaning service', 'fumigation', 'valuation', 'architect', 'engineering service',
    )),
    ('repairs_maintenance', (
        'repair', 'maintenance', 'servicing', 'matengenezo', 'ukarabati', 'labour charge',
        'installation', 'welding', 'painting', 'plumbing', 'spare service',
    )),
    ('marketing', (
        'advert', 'advertising', 'advertisement', 'branding', 'banner', 'billboard',
        'promotion', 'sponsorship', 'matangazo', 'flyer', 'brochure', 'signage',
    )),
    ('capital_asset', (
        'laptop', 'desktop', 'computer', 'server', 'printer', 'photocopier', 'scanner',
        'projector', 'furniture', 'desk', 'chair', 'cabinet', 'shelf', 'safe',
        'air conditioner', 'refrigerator', 'fridge', 'generator', 'motor vehicle',
        'motorcycle', 'pikipiki', 'machine', 'machinery', 'equipment', 'plant',
        'television', 'monitor', 'ups', 'inverter', 'solar panel', 'camera',
    )),
    ('office_supplies', (
        'stationery', 'paper', 'ream', 'pen', 'pens', 'notebook', 'file', 'folder',
        'toner', 'cartridge', 'ink', 'envelope', 'stapler', 'printing', 'photocopy',
        'binding', 'karatasi',
    )),
    ('staff_costs', (
        'salary', 'wage', 'mshahara', 'staff welfare', 'uniform', 'overalls',
        'protective', 'ppe', 'medical', 'hospital', 'clinic', 'pharmacy', 'dawa',
    )),
    ('taxes_levies', (
        'levy', 'duty', 'excise', 'permit', 'licence', 'license', 'penalty',
        'fine', 'stamp duty', 'ushuru', 'leseni', 'road licence', 'city levy',
    )),
)

# Items that are capital by nature but read as generic without a qualifier.
CAPITAL_KEYWORDS = dict(CATEGORY_RULES)['capital_asset']

# Consumables that carry a capital-sounding word. Checked before the capital test so a
# printer *cartridge* is not booked as a printer.
CAPITAL_EXCLUSIONS = (
    'toner', 'cartridge', 'ink', 'paper', 'ream', 'cable', 'charger', 'mouse pad',
    'repair', 'maintenance', 'servicing', 'spare part', 'filter', 'bulb', 'battery',
    'rental', 'hire', 'subscription', 'licence renewal', 'refill',
)

# Service classes that attract withholding tax at source. The indicative rate follows
# the convention already used in the model's prompt: 5% on resident service fees, 10%
# on rent. Whether the supplier is resident, and whether the payer is a withholding
# agent at all, is not on the receipt - so this is a prompt to check, not a liability.
WHT_RULES = (
    ('rent', 10, (
        'rent', 'rental', 'pango', 'kodi ya pango', 'lease', 'office space',
        'warehouse rent', 'premises',
    )),
    ('professional_fees', 5, (
        'consultancy', 'consulting', 'audit', 'accounting', 'bookkeeping', 'legal',
        'advocate', 'lawyer', 'notary', 'advisory', 'professional fee', 'valuation',
        'architect', 'training', 'ushauri', 'engineering service', 'design',
    )),
    ('transport', 5, (
        'transport', 'haulage', 'freight', 'courier', 'delivery charge', 'usafiri',
        'cartage', 'logistics',
    )),
    ('security', 5, ('security service', 'security services', 'guard', 'guarding', 'ulinzi')),
    ('cleaning', 5, ('cleaning service', 'cleaning services', 'fumigation', 'usafi', 'janitorial')),
    ('technical_services', 5, (
        'installation', 'repair service', 'maintenance service', 'technical service',
        'servicing contract', 'welding', 'plumbing', 'electrical works', 'construction',
        'contract works', 'labour charge',
    )),
)

# WHT on service fees applies once payments to one supplier pass this in a month.
WHT_MONTHLY_THRESHOLD_CENTS = 100_000_00

# Expenditure the Income Tax Act restricts or disallows outright. Each entry is a
# (flag, human explanation, keywords) triple; every hit is a review prompt.
DEDUCTIBILITY_RULES = (
    ('entertainment', 'Entertainment is generally non-deductible unless the business is entertainment itself.', (
        'entertainment', 'bar', 'beer', 'bia', 'wine', 'spirits', 'liquor', 'club',
        'nightclub', 'casino', 'party', 'sherehe',
    )),
    ('gift', 'Gifts and donations are deductible only within the limits the Income Tax Act sets.', (
        'gift', 'donation', 'zawadi', 'msaada', 'hamper', 'souvenir', 'present',
        'contribution', 'charity',
    )),
    ('personal_use', 'Looks personal rather than business; needs a business-purpose justification.', (
        'personal', 'binafsi', 'family', 'household', 'home use', 'birthday', 'wedding',
        'harusi', 'grocery', 'groceries',
    )),
    ('fine_penalty', 'Fines and penalties are not deductible.', (
        'fine', 'penalty', 'faini', 'adhabu', 'late payment charge', 'interest on tax',
    )),
)

_WORD_CACHE = {}


def _matches(text, keyword):
    """
    True when `keyword` appears in `text` on whole-word boundaries.

    Substring matching is not usable here: 'ago' (the local name for gas oil) would
    otherwise match 'Chicago', and 'ups' would match 'cups'.
    """
    pattern = _WORD_CACHE.get(keyword)
    if pattern is None:
        pattern = re.compile(rf'(?<![a-z0-9]){re.escape(keyword)}(?![a-z0-9])')
        _WORD_CACHE[keyword] = pattern
    return pattern.search(text) is not None


def normalise_description(description):
    """
    Lower-cased, whitespace-squashed text with punctuation reduced to spaces.

    Also the key items are grouped on when the same product is tracked across
    receipts: 'DIESEL (AGO)' and 'Diesel  AGO' are one thing being bought twice.
    """
    text = (description or '').lower()
    text = re.sub(r'[^a-z0-9\s]+', ' ', text)
    return ' '.join(text.split())


# Internal alias, kept because the rules below read better with the short name.
_normalise = normalise_description


def _any_match(text, keywords):
    """The first keyword in `keywords` that appears in `text`, or None."""
    return next((keyword for keyword in keywords if _matches(text, keyword)), None)


def categorise_item(description):
    """
    The expense category for one line, or None when the text says too little.

    'SUMMARIZED SALE' - which is what a great many Tanzanian EFDs print instead of the
    goods - deliberately returns None rather than 'other', so that the caller can tell
    "we looked and it is uncategorisable" apart from "we did not look".
    """
    text = _normalise(description)
    if not text:
        return None

    for category, keywords in CATEGORY_RULES:
        if _any_match(text, keywords):
            return category
    return None


def categorise_receipt(descriptions):
    """
    One category for a whole receipt, from all of its line descriptions.

    The most frequently occurring category wins; ties go to whichever appeared first,
    which keeps the result stable for the same receipt on every run.
    """
    ordered = []
    for description in descriptions or []:
        category = categorise_item(description)
        if category:
            ordered.append(category)

    if not ordered:
        return None
    return max(dict.fromkeys(ordered), key=lambda category: (ordered.count(category), -ordered.index(category)))


def is_capital_item(description, amount_cents, threshold_cents=CAPITAL_THRESHOLD_CENTS):
    """
    (True, keyword) when this line looks like a depreciable asset rather than a cost.

    Both tests have to pass: the description has to name an asset, and the line has to
    be big enough to be worth capitalising. A 15,000 TZS desk fan is an office cost by
    any sensible reading, whatever the keyword list says.
    """
    text = _normalise(description)
    if not text or amount_cents is None or amount_cents < threshold_cents:
        return False, None

    if _any_match(text, CAPITAL_EXCLUSIONS):
        return False, None

    keyword = _any_match(text, CAPITAL_KEYWORDS)
    return (True, keyword) if keyword else (False, None)


def wht_class(description):
    """
    (class, indicative_rate_percent, matched_keyword) for a line that looks like a
    withholdable service, or None.
    """
    text = _normalise(description)
    if not text:
        return None

    for name, rate, keywords in WHT_RULES:
        keyword = _any_match(text, keywords)
        if keyword:
            return name, rate, keyword
    return None


def deductibility_flags(description):
    """Every restricted-expenditure flag this line raises, as (flag, explanation)."""
    text = _normalise(description)
    if not text:
        return []

    return [
        (flag, explanation)
        for flag, explanation, keywords in DEDUCTIBILITY_RULES
        if _any_match(text, keywords)
    ]


# Guards the two lists against drifting apart: a category invented here would be
# written to receipt.category and then never match anything the model can produce.
assert {category for category, _ in CATEGORY_RULES} <= set(EXPENSE_CATEGORIES)


# --- Naming a category --------------------------------------------------------------
#
# EXPENSE_CATEGORIES above is the set both automatic routes to a category are held to,
# and for them that is the end of it. An admin re-categorising by hand needs one thing
# more: a bucket this list does not have - a levy only one trade pays, a project the
# receipts are being collected against - without that freedom turning into three
# spellings of one category by the end of the quarter.
#
# So a typed category is never stored as typed. It is reduced to a slug, and the slug is
# matched against the categories that already exist on a key that ignores the
# differences nobody means: case, punctuation, and English plurals. 'Office Supplies',
# 'office-supply' and 'OFFICE SUPPLIES' are one category, and it is the one already in
# use rather than a fourth spelling of it.

# models.user.Receipt.category is VARCHAR(50); a longer slug would be silently cut by
# SQLite on write and stop matching the one already stored.
CATEGORY_MAX_LENGTH = 50

# Never a category of its own. 'uncategorised' is what the dashboard calls the receipts
# that have none, so a real category by that name would filter to the opposite of
# itself.
RESERVED_CATEGORIES = frozenset({'uncategorised', 'uncategorized', 'none', 'null'})


def category_label(category):
    """A category as it is written on screen. Matches the chips already rendered."""
    return category.replace('_', ' ') if category else 'Uncategorised'


def normalise_category(text):
    """Free text as a category slug, or None when nothing usable was typed."""
    slug = re.sub(r'[^a-z0-9]+', '_', (text or '').lower()).strip('_')
    return slug[:CATEGORY_MAX_LENGTH].strip('_') or None


def _singular(word):
    """One word with an English plural ending taken off. For comparison only."""
    if len(word) > 4 and word.endswith('ies'):
        return f'{word[:-3]}y'
    if len(word) > 4 and word.endswith(('ses', 'xes', 'zes', 'ches', 'shes')):
        return word[:-2]
    if len(word) > 3 and word.endswith('s') and not word.endswith('ss'):
        return word[:-1]
    return word


def category_key(category):
    """
    What two spellings of the same category have in common.

    Only ever used to decide whether something typed already exists. Never stored, and
    never shown - it is not a name, it is a comparison.
    """
    return ''.join(_singular(word) for word in (category or '').split('_'))


def resolve_category(text, known=()):
    """
    (category, existed) for a category name an admin typed.

    `existed` True means the text turned out to name a category already in use, which
    is the answer that keeps the set from growing a synonym per correction. The fixed
    set is always considered to exist, whether or not any receipt carries it yet, so
    typing 'Fuel' can only ever land on the bucket everything else is filed under.

    (None, False) when the text does not name a category at all.
    """
    slug = normalise_category(text)
    if slug is None or slug in RESERVED_CATEGORIES:
        return None, False

    # The fixed set first: where an instance has invented something that collapses onto
    # a canonical name, the canonical one is the one to keep.
    candidates = list(EXPENSE_CATEGORIES) + [name for name in known if name not in EXPENSE_CATEGORIES]
    if slug in candidates:
        return slug, True

    key = category_key(slug)
    for candidate in candidates:
        if category_key(candidate) == key:
            return candidate, True
    return slug, False


# The fixed set has to survive its own matching rules: two canonical categories that
# collapse onto one comparison key would make resolve_category return whichever came
# first, and a reserved word among them would make one of them unselectable.
assert not (set(EXPENSE_CATEGORIES) & RESERVED_CATEGORIES)
assert len({category_key(category) for category in EXPENSE_CATEGORIES}) == len(EXPENSE_CATEGORIES)
