# tests/test_text_pipeline.py
"""
What happens to a purchase that never produced a receipt.

A large share of what an organisation actually spends leaves no document behind. LUKU
electricity comes back as an SMS carrying a meter number, a token and an amount; water
bills, government control numbers, mobile money transfers and bank alerts all arrive
the same way. Until this pipeline existed the only way to file one was to screenshot
it, save the screenshot and upload the screenshot - a photograph of a screen, handed to
a vision model to do OCR on text we already had exactly.

So the text is read as text. These cover the two things that separates it from the
photo path it is modelled on: it never claims a receipt is an EFD receipt when the
model said nothing about it, and it does not go looking up TRA codes it has invented
out of a transaction reference.
"""

import pytest

from models.user import db, Receipt, Submission


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
def record(app, device):
    """Queues a pasted-record submission."""
    def _submit(text=LUKU_SMS, description=None):
        submission = Submission(
            device_id=device.id, input_type='text', input_data=text,
            # Both, as main.ingest_submission writes them: the sender's note lands in
            # each, and only `description` is overwritten once a receipt is stored.
            description=description, user_note=description,
        )
        db.session.add(submission)
        db.session.commit()
        return submission
    return _submit


@pytest.fixture
def reader(monkeypatch):
    """
    Stubs the extraction call and records what it was handed.

    What it was handed is half the point: the text path must send the characters
    themselves and say they are not an image, or it is the vision model being asked to
    open a file named after an SMS.
    """
    import main

    calls = []
    # The sender's note, as the model was given it. Hung off the fixture rather than
    # returned beside `calls`, so that every test already asserting on what was read
    # goes on reading the same list.
    notes = []

    def _stub(**fields):
        data = {
            'vendor_name': 'TANESCO', 'receipt_date': '2026-07-27',
            'total_amount': 30000, 'document_type': 'other_receipt',
            'receipt_number': '1234 5678 9012 3456 7890',
            'llm_extracted_description': 'LUKU electricity token, 96.4 kWh.',
            'llm_tax_analysis': 'Deductible as a utility cost.',
        }
        data.update(fields)

        def _extract(content, is_image, config, user_note=None, catalogue=None):
            calls.append((content, is_image))
            notes.append(user_note)
            return data
        monkeypatch.setattr(main, 'extract_receipt_details', _extract)
        return calls
    _stub.notes = notes
    return _stub


@pytest.fixture
def portal(monkeypatch):
    """The live portal, replaced by a recorder. Nothing here should reach it."""
    import main

    fetched = []

    def _fetch(url):
        fetched.append(url)
        raise AssertionError(f'the text path asked TRA about {url}')
    monkeypatch.setattr(main, 'fetch_receipt_html', _fetch)
    return fetched


@pytest.fixture
def judgment(monkeypatch):
    """The categorisation step, which is not what these tests are about."""
    import main
    monkeypatch.setattr(main, 'analyse_receipt', lambda *a, **k: {
        'category': 'other', 'llm_extracted_description': 'A purchase.',
        'llm_tax_analysis': 'Deductible.',
    })


def test_a_pasted_record_becomes_a_receipt_read_from_text(
        app, configured, record, reader, portal, judgment):
    """The whole path, end to end: an SMS in, a receipt in the ledger out."""
    import main

    asked = reader()
    submission = record()
    main.process_submission(submission)

    receipt = Receipt.query.one()
    assert receipt.vendor_name == 'TANESCO'
    assert receipt.total_incl_tax_cents == 3000000
    # Marked as what it is. Every reader of a receipt uses this to say where its numbers
    # came from, and 'a model read somebody's paste' is a weaker claim than either of
    # the other two sources.
    assert receipt.extraction_source == 'llm_text'
    assert receipt.document_type == 'other_receipt'
    assert submission.status == 'completed'

    # The characters themselves, and said not to be an image.
    assert asked == [(LUKU_SMS, False)]


def test_a_record_the_model_says_nothing_about_is_not_called_an_efd_receipt(
        app, configured, record, reader, portal, judgment):
    """
    The default that would quietly corrupt the ledger.

    A photograph with no document_type on it is overwhelmingly an EFD receipt, and the
    photo path defaults to one. A pasted LUKU SMS never is. Sharing that default would
    file a made-up EFD receipt every time the model left the field out.
    """
    import main

    reader(document_type=None)
    main.process_submission(record())

    assert Receipt.query.one().document_type == 'other_receipt'


def test_a_transaction_reference_is_not_chased_to_the_portal(
        app, configured, record, reader, portal, judgment):
    """
    The failure this path exists to avoid.

    A LUKU token is a plausible run of digits. Treated as a TRA verification code it
    becomes a portal address nobody will ever confirm, and the submission then spends
    its entire retry schedule finding that out. The address is only rebuilt when the
    model has actually said the text is a transcription of an EFD receipt.
    """
    import main

    reader(document_type='other_receipt',
           receipt_verification_code='58E41A514', receipt_time='09:20:22')
    submission = record()
    main.process_submission(submission)

    # The portal fixture raises if it is called at all.
    assert portal == []
    assert submission.recovered_url is None
    assert Receipt.query.one().extraction_source == 'llm_text'


def test_a_pasted_efd_transcription_is_still_put_to_the_portal(
        app, configured, record, reader, judgment, receipt_html, monkeypatch):
    """
    The one case the rebuild is for, and it must not have been thrown out with the rest.

    Somebody reading the verification code and time off a receipt in their hand and
    pasting them in is the strongest thing this path can produce: TRA answers, and its
    own figures replace everything the model read.
    """
    import main

    fetched = []

    def _fetch(url):
        fetched.append(url)
        return receipt_html
    monkeypatch.setattr(main, 'fetch_receipt_html', _fetch)

    reader(document_type='tra_efd_receipt',
           receipt_verification_code='58E41A514', receipt_time='09:20:22')
    submission = record(text='58E41A514 at 09:20:22')
    main.process_submission(submission)

    assert fetched == ['https://verify.tra.go.tz/58E41A514_092022']
    assert Receipt.query.one().extraction_source == 'tra_html'


def test_what_the_model_read_is_stored_before_anything_acts_on_it(
        app, configured, record, reader, portal, judgment):
    """
    Same guarantee the photo path makes, for the same reason.

    Everything after the reading can end in a retry booked for tomorrow, and until the
    draft was stored that outcome took the whole transcription with it - leaving an
    admin looking at a submission with a countdown on it and nothing to act on.
    """
    import main

    reader()
    submission = record()
    main.process_submission(submission)

    draft = main._stored_llm_draft(submission)
    assert draft and draft['vendor_name'] == 'TANESCO'


def test_an_unconfigured_instance_fails_the_submission_rather_than_the_runner(
        app, record, portal):
    """No LLM means no reading. It must fail this row and leave the queue alone."""
    import main
    from models.user import InstanceConfig

    db.session.add(InstanceConfig(admin_email='a@example.com', totp_secret='S'))
    db.session.commit()

    submission = record()
    main.process_submission(submission)

    assert submission.status == 'failed'
    assert Receipt.query.count() == 0


def test_the_senders_note_is_read_alongside_the_pasted_record(
        app, configured, record, reader, portal, judgment):
    """
    The note goes to the model with the paste.

    A mobile money confirmation says who was paid and how much, and nothing whatever
    about what for. 'Deposit for the Mwanza site fence' is the entire difference between
    a repair and a capital asset, and it is typed by the person sending it - which is
    the only moment anybody knows it.
    """
    import main

    reader()
    main.process_submission(record(description='Deposit for the Mwanza site fence'))

    assert reader.notes == ['Deposit for the Mwanza site fence']


def test_a_pasted_record_with_no_note_sends_none(app, configured, record, reader, portal, judgment):
    """The absence of a note is not an empty note; nothing is added to the prompt."""
    import main

    reader()
    main.process_submission(record())

    assert reader.notes == [None]


# --- Through the real scanner ----------------------------------------------
#
# The `reader` fixture above replaces main.extract_receipt_details wholesale, which is
# right for the tests about routing - they are asking which path a submission takes, not
# what the model said. It also steps straight over the deterministic scan and the
# reconciliation that live inside that function, so the tests below stub one level deeper.

# The SMS that prompted all of this, as the gateway sent it: en dashes, doubled spaces.
TTCL_SMS = (
    'You have paid 55000 TZS for  KELSIA BUSINESS  CONSULTANCY LIMITED – 994944252324 – '
    'TANZANIA TELECOMMUNICATION CORPORATION. 17-07-2026 12:17:40. New Balance  44,202.04 . '
    'TransID MP260717.1217.W74283.'
)

# What the model actually returned for it, field for field. The vendor is the subscriber,
# the receipt number is the account number, and the VAT is 18% of a total no tax was
# charged on.
TTCL_MODEL_ANSWER = {
    'vendor_name': 'KELSIA BUSINESS CONSULTANCY LIMITED',
    'receipt_date': '2026-07-17',
    'receipt_number': '994944252324',
    'total_amount': 55000,
    'vat_amount': 8474.58,
    'document_type': 'other_receipt',
    'category': 'telecom',
    'items': [{'description': 'Payment to KELSIA BUSINESS CONSULTANCY LIMITED - 994944252324 '
                              '- TANZANIA TELECOMMUNICATION CORPORATION', 'amount': 55000}],
    'llm_extracted_description': 'Payment for telecom services.',
    'llm_tax_analysis': 'Subject to 18% VAT; approx 8,474 TZS claimable as input tax.',
}


@pytest.fixture
def raw_reader(monkeypatch):
    """
    The model's answer, stubbed below extract_receipt_details rather than instead of it.

    So the prompt is really built, the record is really scanned, and the answer really
    passes through reconciliation - which is the seam these tests exist to cover.
    """
    from utils import llm_processor

    sent = []

    def _stub(answer):
        def _call(client, config, kind, messages, tools=None, expected_name=None):
            sent.append(messages)
            return dict(answer)
        monkeypatch.setattr(llm_processor, '_call_with_fallback', _call)
        monkeypatch.setattr(llm_processor, 'get_llm_client', lambda config: object())
        return sent
    return _stub


def test_the_scanner_corrects_the_model_on_who_was_paid(
        app, configured, record, raw_reader, portal, judgment):
    """
    The whole point, end to end.

    The model read the subscriber as the supplier, the account number as the receipt
    number, the balance-shaped arithmetic as VAT. The record says otherwise in every case,
    and the record is what gets stored.
    """
    import main

    raw_reader(TTCL_MODEL_ANSWER)
    submission = record(text=TTCL_SMS)
    main.process_submission(submission)

    receipt = Receipt.query.one()
    assert receipt.vendor_name == 'TANZANIA TELECOMMUNICATION CORPORATION'
    assert receipt.customer_name == 'KELSIA BUSINESS  CONSULTANCY LIMITED'
    assert receipt.receipt_number == 'MP260717.1217.W74283'
    assert receipt.total_incl_tax_cents == 5_500_000
    assert receipt.total_tax_cents is None


def test_the_record_is_scanned_before_the_model_is_asked(
        app, configured, record, raw_reader, portal, judgment):
    """The facts reach the model as facts, the way a verified receipt's already do."""
    import main

    sent = raw_reader(TTCL_MODEL_ANSWER)
    main.process_submission(record(text=TTCL_SMS))

    prompt = sent[0][1]['content']
    assert 'Payee, i.e. the vendor: TANZANIA TELECOMMUNICATION CORPORATION' in prompt
    assert 'Account holder, i.e. the customer: KELSIA BUSINESS  CONSULTANCY LIMITED' in prompt
    assert 'transaction id: MP260717.1217.W74283' in prompt
    assert 'It is not an amount on this document.' in prompt


def test_the_identifiers_are_stored_under_the_kind_each_one_is(
        app, configured, record, raw_reader, portal, judgment):
    import main

    raw_reader(TTCL_MODEL_ANSWER)
    main.process_submission(record(text=TTCL_SMS))

    references = {r.kind: r.value for r in Receipt.query.one().references}
    assert references == {
        'transaction_id': 'MP260717.1217.W74283',
        'account_no': '994944252324',
    }


def test_what_the_scanner_overruled_is_kept_on_the_submission(
        app, configured, record, raw_reader, portal, judgment):
    """A correction nobody can see is indistinguishable from a bug."""
    import json
    import main

    raw_reader(TTCL_MODEL_ANSWER)
    submission = record(text=TTCL_SMS)
    main.process_submission(submission)

    report = json.loads(submission.record_scan)
    assert report['scan']['template'] == 'bill_payment_three_part'
    assert report['llm_before']['vendor_name'] == 'KELSIA BUSINESS CONSULTANCY LIMITED'
    assert {a['rule'] for a in report['adjustments']} >= {'R1', 'R2', 'R4', 'R5'}


def test_the_stored_draft_is_the_corrected_answer(
        app, configured, record, raw_reader, portal, judgment):
    """
    An admin accepting the draft must not re-introduce what was just corrected.

    accept_submission_extraction builds a receipt straight from llm_draft, so the draft
    has to be the answer as reconciled rather than as the model gave it.
    """
    import json
    import main

    raw_reader(TTCL_MODEL_ANSWER)
    submission = record(text=TTCL_SMS)
    main.process_submission(submission)

    draft = json.loads(submission.llm_draft)
    assert draft['vendor_name'] == 'TANZANIA TELECOMMUNICATION CORPORATION'
    assert '_record_scan' not in draft


def test_the_fabricated_withholding_finding_is_gone(
        app, configured, record, raw_reader, portal, judgment):
    """
    2,750.00 of withholding tax on an internet bill, from the word CONSULTANCY in a
    payee's trading name. It was rendered on the receipt page as a thing to go and do.
    """
    import main

    raw_reader(TTCL_MODEL_ANSWER)
    main.process_submission(record(text=TTCL_SMS))

    assessment = main.assess_receipt(Receipt.query.one())
    assert assessment.wht_lines == []
    assert assessment.wht_total_cents == 0


def test_the_receipt_page_shows_the_references_and_what_was_corrected(
        app, configured, record, raw_reader, portal, judgment):
    """
    The page has to say what it did.

    Rendered at the highest density, because the provenance panel is where the question
    'why does this row say that' is answered eighteen months later.
    """
    import main
    from models.user import InstanceConfig

    raw_reader(TTCL_MODEL_ANSWER)
    main.process_submission(record(text=TTCL_SMS))
    InstanceConfig.query.first().receipt_detail_level = 'full'
    db.session.commit()

    client = app.test_client()
    with client.session_transaction() as session:
        session['admin_logged_in'] = True
    page = client.get(f'/receipts/{Receipt.query.one().id}').get_data(as_text=True)

    assert 'References' in page
    assert 'MP260717.1217.W74283' in page
    assert 'transaction id' in page
    # The account number is shown, and shown as the customer's rather than as an identity.
    assert '994944252324' in page
    assert 'Identifies the customer, not this payment' in page
    assert 'What the scanner corrected' in page
    assert 'KELSIA BUSINESS CONSULTANCY LIMITED' in page, 'what the model had said'
    assert 'Read deterministically' in page


def test_the_receipt_page_flags_an_analysis_that_claims_tax_nobody_charged(
        app, configured, record, raw_reader, portal, judgment):
    """The 8,474 TZS of input VAT, sitting under a card reading 0.00."""
    import main

    raw_reader(TTCL_MODEL_ANSWER)
    main.process_submission(record(text=TTCL_SMS))

    client = app.test_client()
    with client.session_transaction() as session:
        session['admin_logged_in'] = True
    page = client.get(f'/receipts/{Receipt.query.one().id}').get_data(as_text=True)

    assert 'No tax is recorded on this receipt' in page


def test_a_record_no_template_reads_is_left_to_the_model(
        app, configured, record, raw_reader, portal, judgment):
    """
    Degrading, not failing.

    Nothing here places a party, so the model's vendor stands. The figure and the date it
    reported are still checked against the characters, which is most of the value.
    """
    import main

    raw_reader({
        'vendor_name': 'MAMA NTILIE', 'receipt_date': '2026-08-03',
        'total_amount': 12500, 'document_type': 'other_receipt',
        'llm_extracted_description': 'Lunch.', 'llm_tax_analysis': 'Deductible.',
    })
    main.process_submission(
        record(text='Umefanya malipo ya chakula 12,500.00 tarehe 3 Aug 2026. Asante.'))

    receipt = Receipt.query.one()
    assert receipt.vendor_name == 'MAMA NTILIE'
    assert receipt.total_incl_tax_cents == 1_250_000
