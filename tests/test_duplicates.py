# tests/test_duplicates.py
"""
The same purchase, submitted twice.

A verified EFD receipt has never had this problem: TRA prints a verification code, the
code is the sale's primary key, and the second submission naming it is caught before
the portal is even asked. Everything else did. A LUKU SMS pasted on Monday and again on
Friday became two expenses; a mobile money confirmation forwarded from one phone and
typed on another became two; a handwritten chit photographed twice became two. None of
them carried a code, so nothing compared them at all.

These cover the three checks that now run in front of that (see the DUPLICATES section
in main.py) and, just as importantly, the cases that must *not* be blocked: two real
purchases from one meter, and a submission somebody is re-sending because the first one
failed. A wrongly blocked receipt is an expense nobody can file, which is worse than the
double record it was trying to prevent - so the rule is that only a reference and an
amount agreeing may block, and everything softer than that is reported to a human.
"""
import io
from datetime import date

import pytest

from models.user import db, Device, Receipt, Submission, Vendor
from utils import fingerprint
from utils.device_auth import consume_enrolment_token, issue_enrolment_token


# A real LUKU SMS is the shape this path exists for: long enough that the characters
# themselves are an identity, and carrying a token that nothing else will ever repeat.
LUKU_SMS = (
    'LUKU\n'
    'Meter: 01234567890\n'
    'Token: 1234 5678 9012 3456 7890\n'
    'Units: 96.4 kWh\n'
    'Amount: TZS 30,000'
)


@pytest.fixture
def configured(app):
    """An instance with an LLM provider, which every reading path requires."""
    from models.user import InstanceConfig

    config = InstanceConfig(
        admin_email='admin@example.com', totp_secret='SECRET',
        llm_provider='groq', llm_api_key='test-key',
    )
    db.session.add(config)
    db.session.commit()
    return config


@pytest.fixture
def admin(app):
    """A logged-in browser, for the pages that report a possible duplicate."""
    browser = app.test_client()
    with browser.session_transaction() as session:
        session['admin_logged_in'] = True
    return browser


@pytest.fixture
def phone(app, monkeypatch):
    """An activated device, plus a client that authenticates as it."""
    import main
    monkeypatch.setattr(main.gevent, 'spawn', lambda *a, **kw: None)

    device = Device(name='Field phone')
    db.session.add(device)
    db.session.flush()
    token = issue_enrolment_token(device)
    db.session.commit()
    session_token, _ = consume_enrolment_token(token)

    client = app.test_client()

    class Phone:
        def __init__(self):
            self.device = device

        def paste(self, text, client_uuid):
            return client.post(
                '/scan/api/sync',
                headers={'Authorization': f'Bearer {session_token}'},
                json={'items': [{'client_uuid': client_uuid, 'text': text}]},
            ).get_json()['results'][0]

        def snap(self, raw, client_uuid):
            return client.post(
                '/scan/api/sync/photo',
                headers={'Authorization': f'Bearer {session_token}'},
                data={'client_uuid': client_uuid,
                      'receiptphoto': (io.BytesIO(raw), 'receipt.jpg')},
                content_type='multipart/form-data',
            ).get_json()

    return Phone()


@pytest.fixture
def reader(monkeypatch):
    """
    Stubs the reading step and records every call it was asked to make.

    The call count is half of what is being tested: a duplicate that is only noticed
    after the model has read the document has already cost what the check exists to
    save.
    """
    import main

    calls = []

    def _stub(*answers):
        base = {
            'vendor_name': 'TANESCO', 'receipt_date': '2026-07-27',
            'total_amount': 30000, 'document_type': 'other_receipt',
            'receipt_number': '1234 5678 9012 3456 7890',
            'llm_extracted_description': 'LUKU electricity token, 96.4 kWh.',
            'llm_tax_analysis': 'Deductible as a utility cost.',
        }
        # The last answer stands for every call after it, so a test that expects one
        # reading and gets two fails on the count rather than on an IndexError.
        queue = [dict(base, **answer) for answer in (answers or ({},))]

        def _extract(content, is_image, config, user_note=None, catalogue=None):
            calls.append(content)
            return queue[min(len(calls) - 1, len(queue) - 1)]

        monkeypatch.setattr(main, 'extract_receipt_details', _extract)
        return calls
    return _stub


@pytest.fixture
def judgment(monkeypatch):
    """The categorisation step, which is not what these tests are about."""
    import main
    monkeypatch.setattr(main, 'analyse_receipt', lambda *a, **k: {
        'category': 'other', 'llm_extracted_description': 'A purchase.',
        'llm_tax_analysis': 'Deductible.',
    })


def run(*submissions):
    """
    Processes the submissions a task-runner tick would actually claim.

    Only the queued ones, because that is the whole point of settling a duplicate at
    intake: the row is born with its outcome on it and no runner ever looks at it
    again. A helper that processed every row regardless would hide exactly the saving
    being tested.
    """
    import main
    for submission in submissions:
        if submission.status == 'queued':
            main.process_submission(submission)


# --- What a reference is worth ----------------------------------------------

def test_a_reference_survives_however_it_was_printed():
    """
    A token is grouped in fours so a human can read it back, and a bank reference is
    hyphenated for the same reason. Neither is part of the reference.
    """
    assert fingerprint.normalise_reference('1234 5678 9012 3456 7890') == '12345678901234567890'
    assert fingerprint.normalise_reference('mp-2405/1234') == 'MP24051234'


def test_a_placeholder_is_not_a_reference():
    """
    The one failure a blocking check may not have.

    'N/A' left in would become an identity shared by every document that had no
    reference to print - and the second such document would be filed as a duplicate of
    the first.
    """
    for nothing in ('N/A', 'none', '---', '0000', '   ', ''):
        assert fingerprint.normalise_reference(nothing) is None


def test_a_reference_on_its_own_never_asserts_an_identity():
    """Without an amount beside it there is nothing to catch a misread reference."""
    assert fingerprint.identity_key(reference='MPESA12345678') is None


def test_two_purchases_on_one_meter_are_not_the_same_purchase():
    """
    The reason the amount is part of the key.

    A model that puts the meter number where the token belongs - and they are printed
    two lines apart - hands us a reference that repeats on every purchase that customer
    ever makes. Blocking on it would file a household's entire year of electricity as
    duplicates of its first token. The amount is what tells them apart.
    """
    august = fingerprint.identity_key(reference='01234567890', total_cents=30_000_00)
    september = fingerprint.identity_key(reference='01234567890', total_cents=50_000_00)

    assert august and september and august != september


def test_a_number_out_of_a_receipt_book_means_nothing_on_its_own():
    """
    '87' is unique to one shop's book and often only to that month, so it is only an
    identity beside the supplier who wrote it and the day they wrote it.
    """
    assert fingerprint.identity_key(reference='87', total_cents=5_000_00) is None

    ours = fingerprint.identity_key(reference='87', total_cents=5_000_00,
                                    vendor_key='tin:100147181', on_date=date(2026, 5, 4))
    theirs = fingerprint.identity_key(reference='87', total_cents=5_000_00,
                                      vendor_key='tin:100200300', on_date=date(2026, 5, 4))
    assert ours and theirs and ours != theirs


def test_a_paste_is_identified_by_its_words_and_not_its_line_breaks():
    """An SMS copied out of two different apps is the same SMS."""
    assert fingerprint.text_key(LUKU_SMS) == fingerprint.text_key(LUKU_SMS.replace('\n', '  '))


def test_a_scrap_of_typing_is_not_an_identity():
    """
    'parking 2000' is a plausible thing to type again next Tuesday about an entirely
    different two thousand shillings, so the characters alone must not block it.
    """
    assert fingerprint.text_key('parking 2000') is None


# --- Caught at intake, before anything reads it -----------------------------

def test_the_same_sms_pasted_twice_is_caught_before_the_model(
        phone, configured, reader, judgment):
    """
    The complaint this whole check exists for.

    Nothing compared two pastes, so the second LUKU SMS became a second expense - and
    was read by the model first, at the instance owner's cost, to get there.
    """
    asked = reader()

    first = phone.paste(LUKU_SMS, 'paste-1')
    second = phone.paste(LUKU_SMS, 'paste-2')

    assert first['status'] == 'accepted'
    assert second['status'] == 'duplicate'

    run(*Submission.query.order_by(Submission.id).all())

    assert Receipt.query.count() == 1
    assert len(asked) == 1, 'the second paste must never have reached the model'

    repeat = db.session.get(Submission, second['submission_id'])
    assert repeat.status == 'duplicate'
    assert f"Duplicate of submission ID {first['submission_id']}" in repeat.error_message
    assert 'character for character' in repeat.error_message


def test_a_paste_that_failed_can_be_sent_again(phone, configured, reader, judgment):
    """
    Re-pasting is how a person retries a receipt nobody could read.

    Answering that with 'duplicate of the thing that failed' would leave them no way to
    file the expense at all, so only a submission still queued or already completed
    stands in the way of another.
    """
    reader()
    first = phone.paste(LUKU_SMS, 'paste-1')

    failed = db.session.get(Submission, first['submission_id'])
    failed.status = 'failed'
    db.session.commit()

    assert phone.paste(LUKU_SMS, 'paste-2')['status'] == 'accepted'


def test_the_same_photograph_uploaded_twice_is_caught_at_intake(phone, configured):
    """
    The same file picked out of the gallery a second time.

    Two photographs of one receipt are two different files and are not comparable this
    way - that is what the near-duplicate report is for - but the same bytes twice is
    settled here for the price of a hash.
    """
    first = phone.snap(b'the-same-jpeg-bytes', 'photo-1')
    second = phone.snap(b'the-same-jpeg-bytes', 'photo-2')

    assert first['status'] == 'accepted'
    assert second['status'] == 'duplicate'
    assert Submission.query.count() == 2, 'the row is kept; it is the outcome that differs'
    assert db.session.get(Submission, second['submission_id']).status == 'duplicate'


def test_two_photographs_of_one_receipt_are_both_read(phone, configured):
    """The bytes differ, so intake has nothing to compare and must not invent it."""
    assert phone.snap(b'first-frame', 'photo-1')['status'] == 'accepted'
    assert phone.snap(b'second-frame', 'photo-2')['status'] == 'accepted'


# --- Caught by what the document says ---------------------------------------

def test_the_same_reference_in_a_differently_worded_message_is_a_duplicate(
        app, device, configured, reader, judgment):
    """
    The case a hash cannot reach: one payment, two different texts.

    A confirmation forwarded from another phone, or typed out by hand rather than
    copied, is not the same characters - but it names the same token and the same
    amount, and there is only one purchase behind it.
    """
    reader()

    forwarded = Submission(device_id=device.id, input_type='text',
                           input_data='Fwd: ' + LUKU_SMS + ' (sent from Amina)')
    typed = Submission(device_id=device.id, input_type='text',
                       input_data='Luku 30,000 token 1234 5678 9012 3456 7890 meter 01234567890')
    db.session.add_all([forwarded, typed])
    db.session.commit()

    run(forwarded, typed)

    assert Receipt.query.count() == 1
    assert typed.status == 'duplicate'
    assert 'the same reference and amount' in typed.error_message


def test_two_tokens_bought_on_one_meter_are_both_recorded(
        app, device, configured, reader, judgment):
    """
    The false positive that would matter most, because it repeats every month.

    Same vendor, same meter, same wording - a different purchase. Only the amount and
    the token separate them, and the check has to be reading both.
    """
    reader(
        {'receipt_number': '1234 5678 9012 3456 7890', 'total_amount': 30000},
        {'receipt_number': '9876 5432 1098 7654 3210', 'total_amount': 50000,
         'receipt_date': '2026-08-27'},
    )

    august = Submission(device_id=device.id, input_type='text', input_data=LUKU_SMS)
    september = Submission(device_id=device.id, input_type='text',
                           input_data=LUKU_SMS.replace('30,000', '50,000'))
    db.session.add_all([august, september])
    db.session.commit()

    run(august, september)

    assert Receipt.query.count() == 2
    assert september.status == 'completed'


def test_a_reference_the_model_could_not_read_leaves_the_receipt_alone(
        app, device, configured, reader, judgment):
    """
    A document with nothing to identify it is still a document.

    It is stored, its identity key is empty, and the next one like it is stored too -
    the near-duplicate report is what looks at those, not the blocking check.
    """
    reader({'receipt_number': None})

    chit = Submission(device_id=device.id, input_type='text',
                      input_data='Paid the fundi for the workshop door, thirty thousand')
    db.session.add(chit)
    db.session.commit()
    run(chit)

    assert Receipt.query.one().identity_key is None


# --- Reported, never blocked ------------------------------------------------

def test_a_handwritten_receipt_photographed_twice_is_reported_not_blocked(
        app, admin, device):
    """
    Nothing on a handwritten chit is an identity: no code, no reference, no till.

    What there is - the supplier, the day and the total - is enough to say 'these look
    like one purchase' and not enough to act on it, because two people can pay one
    fundi two identical amounts on one day. So both are kept and the receipt page says
    so.
    """
    from tests.test_dashboard_routes import store

    vendor = Vendor.upsert(tin=None, name='Mama Ntilie kiosk')
    db.session.flush()
    _, first = store(device, vendor_id=vendor.id, vendor='Mama Ntilie kiosk', tin=None,
                     when=date(2026, 5, 4), total=12_000_00)
    _, second = store(device, vendor_id=vendor.id, vendor='Mama Ntilie kiosk', tin=None,
                      when=date(2026, 5, 4), total=12_000_00)

    assert first.near_key == second.near_key
    assert first.possible_duplicates() == [second]

    body = admin.get(f'/receipts/{first.id}').get_data(as_text=True)
    assert 'Possibly the same purchase as' in body


def test_a_supplier_named_two_ways_is_still_one_supplier(app, device):
    """
    'PLASCO LIMITED' and 'Plasco Ltd.' are one shop, and the near key groups on the
    same thing the vendor row does - so a difference in how a model transcribed the
    name does not hide the twin.
    """
    from tests.test_dashboard_routes import store

    _, first = store(device, vendor='PLASCO LIMITED', when=date(2026, 5, 4), total=118_00)
    _, second = store(device, vendor='Plasco Ltd.', when=date(2026, 5, 4), total=118_00)

    assert first.possible_duplicates() == [second]


def test_the_insights_page_puts_the_two_copies_side_by_side(app, admin, device):
    """
    A duplicated expense reads as perfectly ordinary one receipt at a time.

    It is only obvious with both on the screen, which is why the pair is reported on
    the page somebody reviews a month on rather than only on the receipt they would
    have to already suspect.
    """
    from tests.test_dashboard_routes import store

    _, first = store(device, when=date(2026, 5, 4), total=118_00)
    _, second = store(device, when=date(2026, 5, 4), total=118_00)
    store(device, when=date(2026, 5, 4), total=236_00)

    body = admin.get('/insights?start_date=2026-05-01&end_date=2026-05-31').get_data(as_text=True)

    assert 'Possibly recorded twice' in body
    assert f'#{first.id}' in body and f'#{second.id}' in body


def test_two_purchases_from_one_supplier_are_not_reported_as_one(app, device):
    """
    Asked of the finding rather than of the page, because the whole 'worth a second
    look' section hides itself when there is nothing in any of its cards - so a page
    with no duplicates on it says nothing at all, which is not the same as saying no.
    """
    from tests.test_dashboard_routes import store
    from utils import analytics

    _, first = store(device, when=date(2026, 5, 4), total=118_00)
    _, second = store(device, when=date(2026, 5, 4), total=236_00)

    assert analytics.double_records([first, second]) == []


def test_a_cancelled_receipt_is_not_a_double_record(app, device):
    """It is not money spent, so it cannot be money spent twice."""
    from tests.test_dashboard_routes import store
    from utils import analytics

    _, first = store(device, when=date(2026, 5, 4), total=118_00)
    _, voided = store(device, when=date(2026, 5, 4), total=118_00, is_cancelled=True)

    assert analytics.double_records([first, voided]) == []


# --- Keys follow the receipt ------------------------------------------------

# --- What the scanner does to the identity ----------------------------------
#
# The duplicate keys are only ever as good as what lands in receipt_number, and until the
# record scanner existed that was whichever identifier the model reached for first. These
# cover the consequence: the right one gets in, and the wrong one is kept out.

TTCL_SMS = (
    'You have paid 55000 TZS for  ATANA VENTURES – 994944252324 – TANZANIA '
    'TELECOMMUNICATION CORPORATION. 17-07-2026 12:17:40. New Balance  44,202.04 . '
    'TransID MP260717.1217.W74283.'
)


def test_an_account_number_never_becomes_the_identity(app, device, configured, monkeypatch):
    """
    Two bills on one account, a month apart, are two purchases.

    The model put the subscriber's account number in `receipt_number` on both. Left
    there, the account number plus the amount would be the identity of every bill ever
    paid on that account - and where the bill is the same each month, which is what a
    fixed-price internet subscription is, the second one would be refused as a copy of
    the first.
    """
    import main
    from utils import llm_processor

    monkeypatch.setattr(llm_processor, 'get_llm_client', lambda config: object())
    monkeypatch.setattr(llm_processor, '_call_with_fallback', lambda *a, **k: {
        'vendor_name': 'TANZANIA TELECOMMUNICATION CORPORATION',
        'receipt_date': '2026-07-17', 'receipt_number': '994944252324',
        'total_amount': 55000, 'document_type': 'other_receipt',
        'llm_extracted_description': 'Internet bill.', 'llm_tax_analysis': 'Deductible.',
    })
    monkeypatch.setattr(main, 'analyse_receipt', lambda *a, **k: {'category': 'telecom'})

    august = (TTCL_SMS.replace('17-07-2026', '17-08-2026')
              .replace('MP260717.1217.W74283', 'MP260817.1017.W88991'))
    submissions = []
    for text in (TTCL_SMS, august):
        submission = Submission(device_id=device.id, input_type='text', input_data=text)
        db.session.add(submission)
        db.session.commit()
        main.process_submission(submission)
        submissions.append(submission)

    assert [s.status for s in submissions] == ['completed', 'completed']
    assert [r.receipt_number for r in Receipt.query.order_by(Receipt.id)] == [
        'MP260717.1217.W74283', 'MP260817.1017.W88991']


def test_one_payment_submitted_from_two_phones_is_one_receipt(
        app, device, configured, monkeypatch):
    """
    The other side of the same coin, and the behaviour to expect on the two records that
    prompted this work.

    Both messages carry the same transaction id, the same amount and the same instant.
    They are one payment confirmed twice, so the second is a duplicate - which it was not
    before, because the identity was built out of an account number that told the two
    apart only by accident.
    """
    import main
    from utils import llm_processor

    monkeypatch.setattr(llm_processor, 'get_llm_client', lambda config: object())
    monkeypatch.setattr(llm_processor, '_call_with_fallback', lambda *a, **k: {
        'vendor_name': 'TANZANIA TELECOMMUNICATION CORPORATION',
        'receipt_date': '2026-07-17', 'receipt_number': '994944252324',
        'total_amount': 55000, 'document_type': 'other_receipt',
        'llm_extracted_description': 'Internet bill.', 'llm_tax_analysis': 'Deductible.',
    })
    monkeypatch.setattr(main, 'analyse_receipt', lambda *a, **k: {'category': 'telecom'})

    # Not byte-identical, so the content hash cannot settle it - only the reference can.
    second_text = TTCL_SMS.replace('ATANA VENTURES', 'KELSIA BUSINESS CONSULTANCY LIMITED')
    outcomes = []
    for text in (TTCL_SMS, second_text):
        submission = Submission(device_id=device.id, input_type='text', input_data=text)
        db.session.add(submission)
        db.session.commit()
        main.process_submission(submission)
        outcomes.append(submission.status)

    assert outcomes == ['completed', 'duplicate']
    assert Receipt.query.count() == 1


def test_a_corrected_total_is_what_the_receipt_is_matched_on(app, device):
    """
    The keys are derived, not recorded.

    A receipt whose total was corrected by hand is a different purchase from the one it
    was a minute ago, and a key written once at intake would go on describing the
    reading somebody has just overruled.
    """
    from tests.test_dashboard_routes import store

    _, first = store(device, when=date(2026, 5, 4), total=118_00)
    _, second = store(device, when=date(2026, 5, 4), total=236_00)
    assert first.possible_duplicates() == []

    second.total_incl_tax_cents = 118_00
    db.session.commit()

    assert second.near_key == first.near_key
    assert first.possible_duplicates() == [second]
