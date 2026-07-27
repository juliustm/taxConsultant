# utils/money.py
"""
Money handling for the whole application.

Amounts are stored as an integer number of cents. Receipts are added up, split by
tax rate and reconciled against TRA's own totals, and none of that survives being
carried in a binary float: 0.1 + 0.2 is not 0.3, and a few thousand receipts of
that is a reconciliation that never closes.

Decimal is the working type, int-cents is the storage type, and the two
conversions live here so nothing else has to think about rounding.
"""
from decimal import Decimal, ROUND_HALF_UP, InvalidOperation

CENTS_PER_UNIT = Decimal('100')
_QUANTUM = Decimal('0.01')


def to_decimal(value):
    """Coerces a str/int/float/Decimal to Decimal, or None when it isn't a number."""
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, Decimal):
        return value
    try:
        # str() first: Decimal(0.1) is 0.1000000000000000055511151231257827.
        return Decimal(str(value).strip())
    except (InvalidOperation, ValueError, TypeError):
        return None


def to_cents(value):
    """Converts an amount to whole cents, rounding half up. None passes through."""
    amount = to_decimal(value)
    if amount is None:
        return None
    return int((amount * CENTS_PER_UNIT).quantize(Decimal('1'), rounding=ROUND_HALF_UP))


def from_cents(cents):
    """Converts stored cents back to a Decimal amount. None passes through."""
    if cents is None:
        return None
    return (Decimal(int(cents)) / CENTS_PER_UNIT).quantize(_QUANTUM)


def format_cents(cents):
    """Renders stored cents as a plain '1234.56' string, for CSV and JSON."""
    amount = from_cents(cents)
    return None if amount is None else f'{amount:.2f}'
