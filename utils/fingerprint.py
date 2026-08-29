# utils/fingerprint.py
"""
What makes two submissions the same purchase, when nobody issued the purchase an id.

A TRA EFD receipt is the easy case and the only one this app used to handle. It carries
a verification code, that code is the portal's own primary key for the sale, and a
second submission naming it is the same receipt by definition - one indexed equality,
no judgment involved. See main._duplicate_of_code.

Everything else arrives with no such code. A LUKU token, an M-Pesa confirmation, a GePG
control number, a photograph of a handwritten chit: none of them will ever be verified
by anyone, and until now none of them could be recognised twice either. The same SMS
pasted on Monday and again on Friday became two expenses, two VAT positions and two
rows nobody could tell apart.

They are not identityless, though. Every one of them carries *some* reference issued by
whoever produced it - and beside it the details that make a purchase what it is: who
was paid, when, how much. That is enough to build the same kind of key the verification
code gives us for free, at two strengths, and the strength is the whole design:

  * An **identity key** is an assertion that two documents are the same document. It
    blocks: the second submission is filed as a duplicate and never reaches the model.
    So it is only ever built where being wrong is close to impossible - a reference
    *and* the amount agreeing. A reference alone is not enough, because the commonest
    way this goes wrong is a model putting a meter number, an account number or a till
    id where the transaction reference belongs, and those repeat on every purchase the
    customer ever makes. The amount is what tells the two apart.

  * A **near key** is an observation that two receipts look alike: same supplier, same
    day, same total. It never blocks - two genuine purchases can match, and a wrongly
    blocked receipt is an expense nobody can file. It is reported, on the receipt page
    and in peek, and a human decides.

Both are plain strings, stored on the row and matched by equality, so a duplicate check
is an index lookup rather than a scan with a similarity function in it. Nothing here
touches the database or the models: keys are derived from values, so the same rules
apply to a receipt already stored, to what a model just read off a photograph, and to
what an admin typed into a correction form.
"""

import hashlib
import re

# How many characters a reference has to have, once formatting is stripped out of it,
# before it is treated as an identity in its own right rather than as a number that
# only means something beside the supplier who printed it.
#
# Eight is where the two populations separate. A transaction reference issued by a
# payment system is longer than that and is unique across the country - a LUKU token is
# twenty digits, an M-Pesa reference ten characters, a control number twelve. A number
# printed in a receipt book or counted up by a till is shorter - '87', '0041' - and is
# unique only to the shop, and often only to the month.
DISTINCTIVE_REFERENCE_LENGTH = 8

# A paste has to be at least this long before the characters themselves are treated as
# an identity. Every SMS this path exists for is far longer; what this excludes is the
# person who types 'parking 2000' into the box, which is a plausible thing to type again
# next Tuesday about an entirely different two thousand shillings.
DISTINCTIVE_TEXT_LENGTH = 24

# What a model writes in a reference field when there is no reference. Left in, each of
# these would become an identity shared by every document that lacked one - which is the
# one failure mode a blocking check cannot be allowed to have.
_NOT_A_REFERENCE = frozenset({
    'NA', 'N', 'NONE', 'NIL', 'NULL', 'NOTAPPLICABLE', 'NOTAVAILABLE', 'NOTPRINTED',
    'UNKNOWN', 'UNAVAILABLE', 'NOREFERENCE', 'NORECEIPT', 'X', 'XX', 'XXX', 'XXXX',
})


def normalise_reference(value):
    """
    A reference stripped to the characters that carry it, or None if it carries none.

    'MP-2405 1234' and 'mp24051234' are one reference written twice: the spaces and the
    dash are how a phone chose to display it, and a LUKU token is printed in groups of
    four purely so a human can read it back. So everything that is not a letter or a
    digit goes, and case goes with it.

    None for the placeholders a model writes when there was nothing to read, and for a
    run of zeroes, which is the same statement made in digits.
    """
    if value is None:
        return None

    cleaned = re.sub(r'[^A-Za-z0-9]', '', str(value)).upper()
    if not cleaned or cleaned in _NOT_A_REFERENCE:
        return None
    # '0', '000000' and '0000000000' are all 'nothing was printed here'.
    if not cleaned.strip('0'):
        return None
    return cleaned


def normalise_text(value):
    """A paste reduced to its words, so that a stray newline is not a second purchase."""
    if not value:
        return None
    return ' '.join(str(value).split()).casefold() or None


def text_key(value):
    """
    A key for a pasted record, or None if the paste is too slight to be an identity.

    The cheapest duplicate check there is, and the only one that costs nothing at all:
    the same SMS pasted twice is the same characters twice, and that can be settled at
    intake, before a model has been asked to read either of them.
    """
    text = normalise_text(value)
    if not text or len(text) < DISTINCTIVE_TEXT_LENGTH:
        return None
    return 'text:' + hashlib.sha256(text.encode('utf-8')).hexdigest()


def photo_key(digest):
    """
    A key for a stored photograph, given the digest of its bytes, or None without one.

    The hashing itself happens in utils.images, where the bytes are: they exist for the
    length of one upload and are written straight to disk, so anything computed from
    them has to be computed there or read back off the disk afterwards.

    This catches the same *file* twice - a picture picked out of the gallery a second
    time, an upload retried past its acknowledgement - and nothing else. Two photographs
    of one receipt are two different files and are not comparable this way; they are
    what the near key is for.
    """
    if not digest:
        return None
    return f'photo:{digest}'


def identity_key(verification_code=None, reference=None, vendor_key=None,
                 total_cents=None, on_date=None):
    """
    A key asserting *this is that same document*, or None when nothing here says so.

    Built in the order of how much the evidence is worth:

      1. A TRA verification code. It is issued by the revenue authority, it names one
         sale, and nothing else is needed beside it.

      2. A distinctive reference - a token, a transaction id, a control number - with
         the amount paid beside it. The reference is what a payment system issued; the
         amount is the guard against a model having put a meter or account number in
         its place, because a meter is billed a different amount every time.

      3. A short reference - a number out of a receipt book or a till counter - which
         means nothing on its own and everything beside the supplier who printed it and
         the day they printed it. All four have to agree.

    Anything weaker returns None and is left to the near key, which asks a human.
    """
    code = normalise_reference(verification_code)
    if code:
        return f'code:{code}'

    ref = normalise_reference(reference)
    # No amount, no assertion. Every form below rests on the amount agreeing, which is
    # what stops a repeated account number from filing every future purchase on it as a
    # duplicate of the first.
    if not ref or total_cents is None:
        return None

    if len(ref) >= DISTINCTIVE_REFERENCE_LENGTH:
        return f'ref:{ref}|{int(total_cents)}'

    day = _day(on_date)
    if not vendor_key or not day:
        return None
    return f'ref:{vendor_key}|{day}|{ref}|{int(total_cents)}'


def near_key(vendor_key=None, on_date=None, total_cents=None):
    """
    A key for 'the same supplier, the same day, the same total', or None.

    What one purchase recorded twice looks like when neither copy carries anything to
    match on exactly: a photograph and a TRA link of one receipt, or a handwritten chit
    photographed on the day and again when the book was handed in. Two real purchases
    can produce it - the same fare paid twice in a day - which is why this is reported
    and never enforced.
    """
    day = _day(on_date)
    if not vendor_key or not day or total_cents is None:
        return None
    return f'near:{vendor_key}|{day}|{int(total_cents)}'


def _day(value):
    """A date as YYYY-MM-DD, whether it arrived as a date or as text."""
    if value is None:
        return None
    if hasattr(value, 'isoformat'):
        return value.isoformat()[:10]
    return str(value).strip()[:10] or None
