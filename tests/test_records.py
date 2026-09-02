# tests/test_records.py
"""
Reading a written payment record without asking a model.

Two near-identical SMS prompted this. Both were TTCL internet bills paid through the same
gateway, and the only difference between them was the name of the subscriber:

    You have paid 55000 TZS for  ATANA VENTURES - 994944252324 -
    TANZANIA TELECOMMUNICATION CORPORATION. ...

The model read one of them as a purchase from 'ATANA VENTURES - 994944252324 - TANZANIA
TELECOMMUNICATION CORPORATION' and the other as a purchase from 'KELSIA BUSINESS
CONSULTANCY LIMITED'. Neither answer named the party that received the money, and one of
them named the business that sent it. Everything below is that failure written as
assertions: the same shape read twice gives the same answer, the payee is the vendor, the
subscriber is the customer, and the balance left in the wallet is not the amount paid.

The corpus is the specification. A format this app cannot read is a format missing from
CORPUS, and adding one is a case here and a tuple in records.TEMPLATES.
"""

import pytest

from utils.records import (
    IDENTIFYING, NON_IDENTIFYING, anchor_block, is_payment_narration, reconcile, scan,
)


# The message that prompted the module, exactly as the gateway sent it - en dashes and
# doubled spaces included. Both copies are kept: the pair is the determinism test.
TTCL_ATANA = (
    'You have paid 55000 TZS for  ATANA VENTURES – 994944252324 – TANZANIA '
    'TELECOMMUNICATION CORPORATION. 17-07-2026 12:17:40. New Balance  44,202.04 . '
    'TransID MP260717.1217.W74283.'
)
TTCL_KELSIA = (
    'You have paid 55000 TZS for  KELSIA BUSINESS  CONSULTANCY LIMITED – 994944252324 – '
    'TANZANIA TELECOMMUNICATION CORPORATION. 17-07-2026 12:17:40. New Balance  44,202.04 . '
    'TransID MP260717.1217.W74283.'
)

MPESA_SEND = (
    'FT25071712345 Umetuma Tsh5,000.00 kwa JOHN DOE - 0754123456 tarehe 17/7/2026 '
    'saa 12:17. Salio jipya ni Tsh10,000.00. Makato ni Tsh300.00.'
)
MPESA_LIPA = (
    'FT25071798765 Umelipa Tsh10,000.00 kwa DUKA LA MAJI, Lipa Namba 123456, '
    'tarehe 17/07/2026. Salio jipya ni Tsh5,000.00.'
)
MPESA_RECEIVE = (
    'FT25071711111 Umepokea Tsh20,000.00 kutoka JANE DOE - 0755000000 tarehe 17/07/2026. '
    'Salio jipya ni Tsh25,000.00.'
)
AIRTEL_PAY = (
    'Umelipa TZS 5,000 kwa MAMA LISHE CATERING. TxnID PP260717123456. Salio TZS 2,000.'
)
LUKU_TOKEN = (
    'LUKU\nMeter: 01234567890\nToken: 1234 5678 9012 3456 7890\nUnits: 96.4 kWh\n'
    'Amount: TZS 30,000'
)
LUKU_SWAHILI = (
    'Umenunua LUKU TANESCO. Mita 04123456789. Tokeni 5678-9012-3456-7890-1234. '
    'Uniti 45.5 kWh. Kiasi TZS 20,000. Gharama ya huduma TZS 1,000. VAT TZS 3,050.'
)
GEPG_PAYMENT = (
    'Malipo yamefanikiwa. Namba ya kudhibiti 991234567890. Kiasi TZS 50,000. '
    'Taasisi: TANZANIA REVENUE AUTHORITY. Muamala BK12345678.'
)
CRDB_DEBIT = (
    'CRDB: Debit Alert. A/C 0150123456700 debited TZS 250,000 on 17/07/2026. '
    'Ref TXN123456789. Available balance TZS 1,200,000.'
)
NMB_CREDIT = (
    'NMB: Akaunti 20123456789 imepokelewa TZS 400,000 tarehe 17/07/2026. '
    'Kumbukumbu FT26071812345. Salio TZS 900,000.'
)
AIRTIME = (
    'Umenunua muda wa maongezi wa TZS 5,000. Salio la simu TZS 5,200. '
    'TransID CP260717.1234.A12345.'
)
UNRECOGNISED = 'Umefanya malipo ya kitu fulani 12,500.00 tarehe 3 Aug 2026. Asante.'

CORPUS = (
    TTCL_ATANA, TTCL_KELSIA, MPESA_SEND, MPESA_LIPA, MPESA_RECEIVE, AIRTEL_PAY,
    LUKU_TOKEN, LUKU_SWAHILI, GEPG_PAYMENT, CRDB_DEBIT, NMB_CREDIT, AIRTIME, UNRECOGNISED,
)


def _references(result):
    return {reference.kind: reference.value for reference in result.references}


def _parties(result):
    return {party.role: party.name for party in result.parties}


# --------------------------------------------------------------------------------------
# The invariants. These hold for every message, in every format, for ever.
# --------------------------------------------------------------------------------------

@pytest.mark.parametrize('text', CORPUS)
def test_every_value_is_literally_in_the_text(text):
    """
    Nothing may exist in a scan that is not in the record.

    The whole reason this module can be trusted over a model: a span is a claim about
    where a value was, and a claim that can be checked is not a guess. If this ever fails
    the scanner has started inventing, which is the failure it was written to replace.
    """
    result = scan(text)
    values = list(result.parties) + list(result.references) + list(result.amounts)
    assert values, 'a record this shape should give up something'
    for value in values:
        assert result.text[value.span.start:value.span.end] == value.span.text


@pytest.mark.parametrize('text', CORPUS)
def test_scanning_twice_gives_the_same_answer(text):
    """The same characters read twice are the same purchase. This is failure #5."""
    assert scan(text).as_dict() == scan(text).as_dict()


@pytest.mark.parametrize('text', CORPUS)
def test_a_scan_is_json_serialisable(text):
    """as_dict is stored on the submission, so it has to survive json.dumps."""
    import json
    json.dumps(scan(text).as_dict())


def test_empty_and_junk_records_scan_without_raising():
    for text in (None, '', '   ', 'asante sana', '...'):
        result = scan(text)
        assert result.parties == [] and result.references == []


# --------------------------------------------------------------------------------------
# The message that prompted all of this.
# --------------------------------------------------------------------------------------

def test_the_bill_payment_names_the_party_that_was_paid():
    result = scan(TTCL_ATANA)

    assert result.template == 'bill_payment_three_part'
    assert _parties(result) == {
        'payee': 'TANZANIA TELECOMMUNICATION CORPORATION',
        'account_holder': 'ATANA VENTURES',
    }


def test_the_bill_payment_types_its_two_numbers_apart():
    """
    The transaction id identifies the payment; the account number identifies the customer.

    Both are twelve-ish characters of digits and letters and nothing about their shape
    tells them apart - only the word printed beside one of them, and the position of the
    other. Getting this wrong is what makes every future bill on the account a duplicate.
    """
    result = scan(TTCL_ATANA)

    assert _references(result) == {
        'transaction_id': 'MP260717.1217.W74283',
        'account_no': '994944252324',
    }
    assert result.primary_reference().kind == 'transaction_id'
    assert result.reference('account_no').kind in NON_IDENTIFYING


def test_the_balance_is_not_the_amount():
    """55,000 was paid. 44,202.04 is what was left in the wallet, and is not a figure here."""
    result = scan(TTCL_ATANA)

    assert result.paid().cents == 5_500_000
    assert result.balance_after().cents == 4_420_204
    assert result.paid().span.text == '55000'


def test_the_bill_payment_reads_its_timestamp():
    result = scan(TTCL_ATANA)

    assert result.occurred_at.isoformat() == '2026-07-17T12:17:40'
    assert result.occurred_at_has_time is True


def test_the_two_bill_payments_differ_only_in_the_customer():
    """
    The pair test, and the point of the whole module.

    The model read these two as purchases from two different suppliers. They are the same
    payment to the same corporation, differing in the one field the payment system varied.
    """
    atana, kelsia = scan(TTCL_ATANA), scan(TTCL_KELSIA)

    assert atana.template == kelsia.template
    assert atana.payee().name == kelsia.payee().name == 'TANZANIA TELECOMMUNICATION CORPORATION'
    assert atana.primary_reference().value == kelsia.primary_reference().value
    assert atana.paid().cents == kelsia.paid().cents
    assert atana.account_holder().name == 'ATANA VENTURES'
    assert kelsia.account_holder().name == 'KELSIA BUSINESS  CONSULTANCY LIMITED'


def test_an_ascii_hyphen_reads_the_same_as_an_en_dash():
    """The gateway picks the dash; the reader must not care which it picked."""
    result = scan(TTCL_ATANA.replace('–', '-'))

    assert result.payee().name == 'TANZANIA TELECOMMUNICATION CORPORATION'
    assert result.account_holder().name == 'ATANA VENTURES'


# --------------------------------------------------------------------------------------
# The rest of the corpus, one family at a time.
# --------------------------------------------------------------------------------------

def test_mobile_money_transfer_separates_the_amount_the_balance_and_the_fee():
    result = scan(MPESA_SEND)

    assert result.template == 'mobile_money_send'
    assert result.payee().name == 'JOHN DOE'
    assert result.paid().cents == 500_000
    assert result.balance_after().cents == 1_000_000
    assert result.amount('fee').cents == 30_000
    assert result.occurred_at.isoformat() == '2026-07-17T12:17:00'


def test_a_till_payment_names_the_merchant_and_types_the_till_number():
    result = scan(MPESA_LIPA)

    assert result.payee().name == 'DUKA LA MAJI'
    assert result.paid().cents == 1_000_000
    assert _references(result)['merchant_no'] == '123456'


def test_money_received_is_not_recorded_as_money_paid():
    """A credit is not a purchase, and its figure must not become a total."""
    result = scan(MPESA_RECEIVE)

    assert result.party('payer').name == 'JANE DOE'
    assert result.paid() is None
    assert result.amount('received').cents == 2_000_000


def test_a_wallet_payment_to_a_merchant_reads_its_transaction_id():
    result = scan(AIRTEL_PAY)

    assert result.payee().name == 'MAMA LISHE CATERING'
    assert result.paid().cents == 500_000
    assert _references(result)['transaction_id'] == 'PP260717123456'


def test_a_luku_token_keeps_the_token_whole():
    """
    Twenty digits printed in fives, which a lazier pattern returns the first group of.

    A quarter of a token is not a token: it identifies nothing, and stored as a reference
    it would collide with every other purchase that happened to start the same way.
    """
    result = scan(LUKU_TOKEN)

    assert result.channel == 'utility'
    assert _references(result) == {
        'meter_no': '01234567890',
        'token': '1234 5678 9012 3456 7890',
    }
    assert result.paid().cents == 3_000_000


def test_a_swahili_luku_message_reads_the_same_fields():
    result = scan(LUKU_SWAHILI)

    references = _references(result)
    assert references['meter_no'] == '04123456789'
    assert references['token'] == '5678-9012-3456-7890-1234'
    assert result.payee().name == 'TANESCO'
    assert result.amount('tax').cents == 305_000
    assert result.amount('fee').cents == 100_000


def test_a_government_payment_reads_its_control_number_as_an_identity():
    """A control number is issued for one bill, so unlike an account number it identifies."""
    result = scan(GEPG_PAYMENT)

    assert result.channel == 'government'
    assert result.payee().name == 'TANZANIA REVENUE AUTHORITY'
    assert _references(result)['control_number'] == '991234567890'
    assert result.reference('control_number').kind in IDENTIFYING


def test_a_bank_debit_alert_reads_the_amount_and_leaves_the_balance_alone():
    result = scan(CRDB_DEBIT)

    assert result.channel == 'bank'
    assert result.paid().cents == 25_000_000
    assert result.balance_after().cents == 120_000_000
    references = _references(result)
    assert references['account_no'] == '0150123456700'
    assert references['transaction_id'] == 'TXN123456789'


def test_a_bank_credit_alert_is_not_a_purchase():
    result = scan(NMB_CREDIT)

    assert result.paid() is None
    assert result.amount('received').cents == 40_000_000


def test_an_airtime_topup_reads_as_telecom():
    result = scan(AIRTIME)

    assert result.channel == 'telecom'
    assert result.paid().cents == 500_000
    assert _references(result)['transaction_id'] == 'CP260717.1234.A12345'


def test_an_unrecognised_format_still_gives_up_what_it_can():
    """
    A scan degrades, it does not fail.

    No template matches, so no name is placed - roles are the one thing a regex cannot
    infer without knowing the format, and a name promoted to vendor on a guess is the
    original failure with a new author. Everything a label or a currency marker settles
    is still read.
    """
    result = scan(UNRECOGNISED)

    assert result.template is None
    assert result.parties == []
    assert result.paid().cents == 1_250_000
    assert result.occurred_at.date().isoformat() == '2026-08-03'


def test_an_account_number_is_never_mistaken_for_an_amount():
    """Without the separator rule, every long reference in every record becomes money."""
    result = scan(TTCL_ATANA)

    assert 994944252312 not in [amount.cents for amount in result.amounts]
    assert all(amount.span.text != '994944252324' for amount in result.amounts)


def test_a_transaction_id_is_not_read_as_a_date():
    """'MP260717.1217.W74283' has two dot-separated number groups in it and is not a date."""
    result = scan('TransID MP260717.1217.W74283. Asante.')

    assert result.occurred_at is None


def test_an_ambiguous_date_is_returned_but_marked():
    """03/04/2026 is two dates. It is still read, and said to be uncertain."""
    result = scan('Umelipa TZS 5,000 kwa DUKA. Tarehe 03/04/2026.')

    assert result.occurred_at.date().isoformat() == '2026-04-03'
    assert result.occurred_at_confidence < 0.8


# --------------------------------------------------------------------------------------
# is_payment_narration - the gate that keeps utils/compliance off a payment sentence.
# --------------------------------------------------------------------------------------

@pytest.mark.parametrize('description, expected', [
    # The line that produced a fabricated 2,750 TZS withholding finding on a phone bill.
    ('Payment to KELSIA BUSINESS CONSULTANCY LIMITED - 994944252324 - '
     'TANZANIA TELECOMMUNICATION CORPORATION', True),
    ('You have paid 55000 TZS for ATANA VENTURES – 994944252324 – TANZANIA '
     'TELECOMMUNICATION CORPORATION.', True),
    ('Payment of TZS 55,000 for telecom services', True),
    ('TransID MP260717.1217.W74283', True),
    ('LIPA JACLINE NGILISHO MOLLEL', False),
    # The three that matter most: a real service must still reach the withholding rules.
    ('Consultancy services', False),
    ('Payment for cleaning services', False),
    ('Security guarding - March', False),
    ('Audit fee for the year', False),
    ('Mayai x 6', False),
    ('Diesel 20 litres', False),
    ('', False),
])
def test_is_payment_narration(description, expected):
    assert is_payment_narration(description) is expected


def test_is_payment_narration_is_stricter_than_products_is_opaque():
    """
    The two answer different questions, and this is the case that separates them.

    utils.products.is_opaque calls 'Payment for cleaning services' boilerplate, which is
    right for its own purpose - deciding whether the sender's note is the only description
    of the purchase. Reused here it would suppress a genuine withholding finding on a
    genuine cleaning contract, which is a false negative on real money.
    """
    from utils.products import is_opaque

    assert is_opaque('Payment for cleaning services') is True
    assert is_payment_narration('Payment for cleaning services') is False


# --------------------------------------------------------------------------------------
# reconcile - one test per rule, against the answer the model actually gave.
# --------------------------------------------------------------------------------------

# What the model returned for TTCL_KELSIA, field for field, as stored on the receipt.
BAD_ANSWER = {
    'vendor_name': 'KELSIA BUSINESS CONSULTANCY LIMITED',
    'receipt_date': '2026-07-17',
    'receipt_number': '994944252324',
    'total_amount': 55000,
    'vat_amount': 8474.58,
    'document_type': 'other_receipt',
    'items': [{
        'description': 'Payment to KELSIA BUSINESS CONSULTANCY LIMITED - 994944252324 - '
                       'TANZANIA TELECOMMUNICATION CORPORATION',
        'amount': 55000,
    }],
    'llm_extracted_description': 'Payment for telecom services',
    'llm_tax_analysis': 'Subject to 18% VAT; approx 8,474 TZS claimable as input tax.',
}


def _rules(adjustments):
    return {adjustment['rule'] for adjustment in adjustments}


def test_reconcile_puts_the_right_party_on_each_side():
    data, adjustments = reconcile(BAD_ANSWER, scan(TTCL_KELSIA))

    assert data['vendor_name'] == 'TANZANIA TELECOMMUNICATION CORPORATION'
    assert data['customer_name'] == 'KELSIA BUSINESS  CONSULTANCY LIMITED'
    assert {'R1', 'R2'} <= _rules(adjustments)


def test_reconcile_says_the_model_returned_the_account_holder():
    """A correction nobody can see is indistinguishable from a bug."""
    _, adjustments = reconcile(BAD_ANSWER, scan(TTCL_KELSIA))

    vendor = next(a for a in adjustments if a['field'] == 'vendor_name')
    assert vendor['from'] == 'KELSIA BUSINESS CONSULTANCY LIMITED'
    assert vendor['to'] == 'TANZANIA TELECOMMUNICATION CORPORATION'
    assert 'account holder' in vendor['why']


def test_reconcile_replaces_a_total_taken_from_the_balance():
    answer = dict(BAD_ANSWER, total_amount=44202.04)

    data, adjustments = reconcile(answer, scan(TTCL_KELSIA))

    assert data['total_amount'] == 55000.0
    assert any(a['rule'] == 'R3' and 'balance' in a['why'] for a in adjustments)


def test_reconcile_drops_a_total_that_is_nowhere_in_the_record():
    data, _ = reconcile(dict(BAD_ANSWER, total_amount=99999), scan(TTCL_KELSIA))

    assert data['total_amount'] == 55000.0


def test_reconcile_keeps_a_total_it_cannot_replace_and_flags_it():
    """Deleting the only total leaves an expense nobody can file. Flag it instead."""
    text = 'Malipo yamekamilika. Kumbukumbu ZZ99887766.'

    data, adjustments = reconcile({'total_amount': 12345}, scan(text))

    assert data['total_amount'] == 12345
    assert 'R3' in _rules(adjustments)


def test_reconcile_drops_vat_the_record_never_stated():
    """18% of the total is arithmetic, not transcription. This is the 8,474 in the prose."""
    data, adjustments = reconcile(BAD_ANSWER, scan(TTCL_KELSIA))

    assert data['vat_amount'] is None
    assert 'R4' in _rules(adjustments)


def test_reconcile_keeps_vat_the_record_does_state():
    data, _ = reconcile({'total_amount': 20000, 'vat_amount': 3050}, scan(LUKU_SWAHILI))

    assert data['vat_amount'] == 3050


def test_reconcile_replaces_an_account_number_used_as_a_receipt_number():
    """
    The rule that matters most.

    994944252324 is the subscriber's account with TTCL. Left in `receipt_number` it
    becomes the duplicate identity, and every later bill on that account is filed as a
    copy of this one.
    """
    data, adjustments = reconcile(BAD_ANSWER, scan(TTCL_KELSIA))

    assert data['receipt_number'] == 'MP260717.1217.W74283'
    reference = next(a for a in adjustments if a['field'] == 'receipt_number')
    assert reference['rule'] == 'R5'
    assert 'duplicate' in reference['why']


def test_reconcile_drops_a_reference_the_record_does_not_contain():
    answer = {'total_amount': 55000, 'receipt_number': 'INVENTED123456'}

    data, _ = reconcile(answer, scan(TTCL_ATANA))

    assert data['receipt_number'] == 'MP260717.1217.W74283'


def test_reconcile_keeps_an_unlabelled_reference_that_is_in_the_record():
    """
    An M-Pesa reference is printed first, with no word beside it, so the scanner does not
    type it. It is still in the record, and it is the only identifier the payment has.
    """
    data, _ = reconcile({'total_amount': 5000, 'receipt_number': 'FT25071712345'},
                        scan(MPESA_SEND))

    assert data['receipt_number'] == 'FT25071712345'


def test_reconcile_leaves_no_receipt_number_when_the_record_has_none():
    text = 'Umelipa TZS 3,000 kwa BODABODA. Asante.'

    data, _ = reconcile({'total_amount': 3000, 'receipt_number': 'MADEUP99'}, scan(text))

    assert data['receipt_number'] is None


def test_reconcile_prefers_the_printed_timestamp():
    answer = dict(BAD_ANSWER, receipt_date='2026-09-01', receipt_time='09:00:00')

    data, _ = reconcile(answer, scan(TTCL_KELSIA))

    assert data['receipt_date'] == '2026-07-17'
    assert data['receipt_time'] == '12:17:40'


def test_reconcile_does_not_invent_a_time_from_a_date():
    """A date with no time parses to midnight, and midnight is a time nobody printed."""
    data, _ = reconcile({'total_amount': 250000}, scan(CRDB_DEBIT))

    assert data['receipt_date'] == '2026-07-17'
    assert 'receipt_time' not in data


def test_reconcile_will_not_overrule_a_model_on_an_ambiguous_date():
    text = 'Umelipa TZS 5,000 kwa DUKA. Tarehe 03/04/2026.'

    data, _ = reconcile({'total_amount': 5000, 'receipt_date': '2026-03-04'}, scan(text))

    assert data['receipt_date'] == '2026-03-04'


def test_reconcile_drops_a_line_amount_that_is_nowhere_in_the_record():
    answer = dict(BAD_ANSWER, items=[{'description': 'Internet', 'amount': 12345}])

    data, adjustments = reconcile(answer, scan(TTCL_KELSIA))

    assert data['items'][0]['amount'] is None
    assert data['items'][0]['description'] == 'Internet', 'descriptions are never rewritten'
    assert 'R7' in _rules(adjustments)


def test_reconcile_drops_a_tin_the_record_never_carried():
    data, _ = reconcile(dict(BAD_ANSWER, vendor_tin='123-456-789'), scan(TTCL_KELSIA))

    assert data['vendor_tin'] is None


def test_reconcile_does_not_modify_what_it_was_given():
    """The stored draft and the corrected answer are two objects, not one aliased twice."""
    answer = dict(BAD_ANSWER)

    reconcile(answer, scan(TTCL_KELSIA))

    assert answer['vendor_name'] == 'KELSIA BUSINESS CONSULTANCY LIMITED'


def test_reconcile_without_a_scan_changes_nothing():
    data, adjustments = reconcile(BAD_ANSWER, None)

    assert data == BAD_ANSWER and adjustments == []


def test_reconcile_leaves_the_judgment_to_the_model():
    """Category, description and analysis are the model's, and nothing here touches them."""
    data, _ = reconcile(BAD_ANSWER, scan(TTCL_KELSIA))

    assert data['llm_extracted_description'] == BAD_ANSWER['llm_extracted_description']
    assert data['llm_tax_analysis'] == BAD_ANSWER['llm_tax_analysis']


# --------------------------------------------------------------------------------------
# anchor_block - what the model is shown before it answers.
# --------------------------------------------------------------------------------------

def test_the_anchor_block_names_both_parties_and_both_figures():
    block = anchor_block(scan(TTCL_ATANA))

    assert 'Payee, i.e. the vendor: TANZANIA TELECOMMUNICATION CORPORATION' in block
    assert 'Account holder, i.e. the customer: ATANA VENTURES' in block
    assert 'Paid: 55,000.00 TZS' in block
    assert 'Balance after the payment: 44,202.04' in block
    assert 'It is not an amount on this document.' in block


def test_the_anchor_block_types_each_reference_and_shows_its_label():
    block = anchor_block(scan(TTCL_ATANA))

    assert 'transaction id: MP260717.1217.W74283 (labelled "TransID")' in block
    assert 'account no: 994944252324' in block


def test_the_anchor_block_closes_the_gaps_it_leaves():
    """Without the last line the list reads as a list of fields to fill in."""
    assert 'Do not invent it.' in anchor_block(scan(TTCL_ATANA))


def test_the_anchor_block_is_empty_when_there_is_nothing_to_anchor():
    assert anchor_block(scan('asante sana')) == ''
    assert anchor_block(None) == ''
