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
