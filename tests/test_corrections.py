# tests/test_corrections.py
"""
The two things an admin can fix by hand, and what each one is allowed to touch.

Karani exists so that nobody types receipts in, so the interesting question here is not
"can a field be edited" but "does correcting it put the automation back on the rails".
Two answers, and they are different in kind:

  * The address. TRA is asked for <code>_<HHMMSS>, both halves are guesses whenever the
    QR square would not decode, and a single misread digit strands a receipt that exists
    perfectly well on the portal. Correcting those two fields enters no data at all - it
    tells the pipeline where to look and the portal supplies the facts. That correction
    has to *stick*, too: a retry hours later that went back to reading the broken address
    would undo it silently, which is what the precedence test below is about.

  * The receipt read off a photograph, where there is no portal to fall back on and the
    numbers are a model's reading of a crumpled thermal print. Editable, but only that
    one: a receipt parsed from TRA's own verified page is the portal's record of the sale
    and is not anybody's to revise here.

The portal and the model are stubbed throughout. What is under test is which of them is
asked, what is stored afterwards, and what the next scheduled attempt will do.
"""
import json
import shutil
import subprocess

import pytest

from models.user import db, Receipt, Submission, Vendor
from tests.test_dashboard_javascript import ScriptCollector
from utils.llm_processor import reconstructed_receipt_url
from utils.tra import build_receipt_url, TraReceiptNotUploaded, TraTransportError, TraWrongReceiptTime


# --- Building the address ---------------------------------------------------

@pytest.mark.parametrize('time_text', ['10:47:40', '104740', '10.47.40', ' 10:47:40 '])
def test_the_time_is_read_in_every_form_it_gets_typed_in(time_text):
    """
    Read as fields, not as a run of digits.

    Somebody copying the six digits out of a URL, somebody typing what is printed on the
    paper and somebody using stops instead of colons are all reading the same time off
    the same receipt, and refusing two of them would send them back to the photograph to
    do it again.
    """
    assert build_receipt_url('010FF9418267', time_text) == (
        'https://verify.tra.go.tz/010FF9418267_104740'
    )


def test_a_receipt_printed_without_seconds_still_builds_an_address():
    """Some EFDs print HH:MM. The portal still wants all six digits."""
    assert build_receipt_url('58E41A514', '9:20').endswith('_092000')
    assert build_receipt_url('58E41A514', '9:20:22').endswith('_092022')


@pytest.mark.parametrize('code, time_text', [
    ('', '10:47:40'),          # nothing to ask for
    ('58E41A514', ''),         # half an address
    ('58E41A514', '25:00:00'), # not a time of day
    ('58E41A514', '10:99'),
    ('58E41A514', '104'),      # neither three fields nor six digits
])
def test_a_half_guessed_address_is_refused_rather_than_sent(code, time_text):
    """
    Guessing costs a request against a rate-limited portal and can land on somebody
    else's receipt, so the refusal is the feature. It carries a sentence, because the
    person who reads it is the person who just typed it.
    """
    with pytest.raises(ValueError) as raised:
        build_receipt_url(code, time_text)
    assert str(raised.value)


def test_the_model_and_the_admin_build_the_same_address():
    """
    Both rebuild the portal address from a code and a time read off the same paper. The
    construction lives in one place precisely so they cannot come to differ about it.
    """
    built = reconstructed_receipt_url(
        {'receipt_verification_code': '58E41A514', 'receipt_time': '09:20:22'},
    )
    assert built == build_receipt_url('58E41A514', '09:20:22')


def test_a_transcription_that_will_not_build_an_address_is_not_an_error():
    """The vision path has somewhere else to go, so it gets None rather than a raise."""
    assert reconstructed_receipt_url({'receipt_verification_code': '', 'receipt_time': '09:20'}) is None
    assert reconstructed_receipt_url({'receipt_verification_code': 'ABC', 'receipt_time': 'nope'}) is None


# --- Correcting the address on a submission ---------------------------------

@pytest.fixture
def configured(app):
    from models.user import InstanceConfig

    config = InstanceConfig(
        admin_email='admin@example.com', totp_secret='SECRET',
        llm_provider='groq', llm_api_key='test-key',
    )
    db.session.add(config)
    db.session.commit()
    return config


@pytest.fixture
def admin(app, configured):
    client = app.test_client()
    with client.session_transaction() as session:
        session['admin_logged_in'] = True
    return client


@pytest.fixture
def portal(monkeypatch, receipt_html):
    """Serves saved HTML, or raises, in place of the live portal."""
    import main

    fetched = []

    def _serve(html=None, error=None):
        def _fetch(url):
            fetched.append(url)
            if error is not None:
                raise error
            return html if html is not None else receipt_html
        monkeypatch.setattr(main, 'fetch_receipt_html', _fetch)
        return fetched
    return _serve


@pytest.fixture
def judgment(monkeypatch):
    """The categorisation step, which is not what these tests are about."""
    import main
    monkeypatch.setattr(main, 'analyse_receipt', lambda *a, **k: {
        'category': 'other', 'llm_extracted_description': 'A purchase.',
        'llm_tax_analysis': 'Deductible.',
    })


@pytest.fixture
def stuck_photo(app, device):
    """A photo submission that failed with the wrong time in its recovered address."""
    submission = Submission(
        device_id=device.id, input_type='photo', input_data='receipt.jpg',
        status='failed', failure_reason='TraWrongReceiptTime',
        error_message='TraWrongReceiptTime: the time is wrong.',
        recovered_url='https://verify.tra.go.tz/58E41A514_091122',
        receipt_code='58E41A514',
    )
    db.session.add(submission)
    db.session.commit()
    return submission


def test_a_corrected_address_is_verified_there_and_then(
        app, admin, stuck_photo, portal, judgment):
    """
    The whole point. A stranded submission becomes an ordinary verified receipt carrying
    TRA's own numbers, without anybody typing an amount.

    Asked immediately rather than queued: the person who typed the correction is watching
    the button, and the queue's ten-second wake-up is not an answer to them.
    """
    fetched = portal()

    response = admin.post(f'/submissions/{stuck_photo.id}/correct', data={
        'receipt_code': '58E41A514', 'receipt_time': '09:20:22',
    })

    assert response.status_code == 200
    assert response.json['verified'] is True
    assert fetched == ['https://verify.tra.go.tz/58E41A514_092022']

    receipt = Receipt.query.one()
    assert response.json['receipt_id'] == receipt.id
    # TRA's page, not a reading of one: the numbers came from the portal.
    assert receipt.extraction_source == 'tra_html'
    assert receipt.vendor_tin == '100147181'
    assert db.session.get(Submission, stuck_photo.id).status == 'completed'


def test_a_corrected_address_survives_the_next_scheduled_attempt(
        app, admin, stuck_photo, portal, judgment):
    """
    The correction has to outlive the request that made it.

    A retryable failure puts the submission back on the queue, and if the next attempt
    read the address from anywhere but the correction, it would quietly go back to asking
    for the broken one - and the admin would watch their fix evaporate.
    """
    fetched = portal(error=TraTransportError('portal unreachable'))

    admin.post(f'/submissions/{stuck_photo.id}/correct', data={
        'receipt_code': '58E41A514', 'receipt_time': '09:20:22',
    })

    submission = db.session.get(Submission, stuck_photo.id)
    assert submission.recovered_url == 'https://verify.tra.go.tz/58E41A514_092022'
    # Back on the queue, and from the top: the attempts already spent were spent asking
    # for a different address.
    assert submission.status == 'queued'
    assert submission.retry_count == 0

    import main
    main.process_submission(submission)
    assert fetched[-1] == 'https://verify.tra.go.tz/58E41A514_092022'


def test_an_address_the_portal_calls_wrong_stays_failed(app, admin, stuck_photo, portal):
    """
    A wrong time is not worth retrying - the next attempt sends the same wrong time - so
    it stays failed and says so, and the form stays open for another go.
    """
    portal(error=TraWrongReceiptTime('the portal asked for the time again'))

    response = admin.post(f'/submissions/{stuck_photo.id}/correct', data={
        'receipt_code': '58E41A514', 'receipt_time': '09:20:23',
    })

    assert response.json['verified'] is False
    assert response.json['reason'] == 'TraWrongReceiptTime'
    submission = db.session.get(Submission, stuck_photo.id)
    assert submission.status == 'failed'
    # Saved even so: it is closer than what was there, and it is what the next try edits.
    assert submission.recovered_url.endswith('_092023')
    assert submission.corrected_at is not None


def test_a_correction_that_names_a_receipt_we_already_hold_says_so(
        app, admin, device, stuck_photo, portal, judgment):
    """
    Two photographs of one receipt is an ordinary thing to happen, and the answer is not
    a failure - it is 'this is receipt #4, which you already have'.
    """
    twin = Submission(device_id=device.id, input_type='url', input_data='x', status='completed')
    db.session.add(twin)
    db.session.commit()
    held = Receipt(
        receipt_verification_code='58E41A514', device_id=device.id, submission_id=twin.id,
    )
    db.session.add(held)
    db.session.commit()

    portal()
    response = admin.post(f'/submissions/{stuck_photo.id}/correct', data={
        'receipt_code': '58E41A514', 'receipt_time': '09:20:22',
    })

    assert response.json['verified'] is False
    assert response.json['receipt_id'] == held.id
    assert db.session.get(Submission, stuck_photo.id).status == 'duplicate'
    assert Receipt.query.count() == 1


def test_an_unbuildable_address_never_reaches_the_portal(app, admin, stuck_photo, portal):
    """Refused with the sentence build_receipt_url raised, before a request is spent."""
    fetched = portal()

    response = admin.post(f'/submissions/{stuck_photo.id}/correct', data={
        'receipt_code': '58E41A514', 'receipt_time': '99:99',
    })

    assert response.status_code == 400
    assert '99:99' in response.json['error']
    assert fetched == []


# --- Correcting a receipt read off a photograph -----------------------------

@pytest.fixture
def photo_receipt(app, device):
    """A receipt the vision model read, which is the only kind that is editable."""
    from datetime import date

    submission = Submission(
        device_id=device.id, input_type='photo', input_data='receipt.jpg', status='completed',
    )
    db.session.add(submission)
    db.session.commit()

    receipt = Receipt(
        device_id=device.id, submission_id=submission.id, extraction_source='llm_vision',
        vendor_name='EFRA1M M0TORS', vendor_tin='10347036',  # as misread
        vrn='NOT REGISTERED', receipt_date=date(2025, 5, 28),
        total_incl_tax_cents=7_600_000, llm_status='ok',
    )
    receipt.vendor = Vendor.upsert(tin='10347036', name='EFRA1M M0TORS')
    db.session.add(receipt)
    db.session.commit()
    return receipt


def test_a_corrected_tin_re_files_the_receipt_under_the_right_supplier(app, admin, photo_receipt):
    """
    Not a cosmetic edit. Suppliers are grouped on TIN, so a misread digit files this
    receipt under a supplier of its own and every per-supplier total is wrong until it
    moves - which means the vendor row has to follow the correction, not just the column.
    """
    before = photo_receipt.vendor_id

    response = admin.post(f'/receipts/{photo_receipt.id}/correct', data={
        'vendor_tin': '103470362', 'vendor_name': 'EFRAIM MOTORS',
    })

    assert response.status_code == 200
    receipt = db.session.get(Receipt, photo_receipt.id)
    assert receipt.vendor_tin == '103470362'
    assert receipt.vendor_id != before
    assert receipt.vendor.lookup_key == 'tin:103470362'


def test_amounts_are_read_as_they_are_printed(app, admin, photo_receipt):
    """
    Typed off paper, so they arrive punctuated. Refusing '76,000' would be refusing the
    receipt as it is actually written.
    """
    response = admin.post(f'/receipts/{photo_receipt.id}/correct', data={
        'total_incl_tax_cents': '76,000', 'total_tax_cents': '11593.22 TZS',
    })

    assert response.status_code == 200
    receipt = db.session.get(Receipt, photo_receipt.id)
    assert receipt.total_incl_tax_cents == 7_600_000
    assert receipt.total_tax_cents == 1_159_322


def test_a_value_that_will_not_read_is_refused_without_saving_the_rest(app, admin, photo_receipt):
    """
    All or nothing. A form that stored eight fields and dropped the ninth would leave a
    receipt half corrected, and the only person who could tell has already left the page.
    """
    response = admin.post(f'/receipts/{photo_receipt.id}/correct', data={
        'vendor_name': 'EFRAIM MOTORS', 'receipt_date': 'last tuesday',
    })

    assert response.status_code == 400
    assert 'Receipt date' in response.json['error']
    assert db.session.get(Receipt, photo_receipt.id).vendor_name == 'EFRA1M M0TORS'


def test_clearing_a_vrn_the_model_invented_clears_it_on_the_supplier_too(app, admin, photo_receipt):
    """
    'NOT REGISTERED' is printed on the paper and gets transcribed as if it were a VRN,
    and a supplier holding one is a supplier whose input VAT looks claimable. Deleting it
    is a correction, so it has to be able to travel - upsert alone only ever fills blanks.
    """
    response = admin.post(f'/receipts/{photo_receipt.id}/correct', data={
        'vrn': '', 'vendor_tin': '10347036', 'vendor_name': 'EFRA1M M0TORS',
    })

    assert response.status_code == 200
    receipt = db.session.get(Receipt, photo_receipt.id)
    assert receipt.vrn is None
    assert receipt.vendor.vrn is None
    assert receipt.vendor.is_vat_registered is False


def test_what_a_human_overwrote_is_recorded(app, admin, photo_receipt):
    """
    Otherwise a corrected field is indistinguishable from one the model happened to get
    right, and the question this page has to answer is 'which of these is still a guess'.
    """
    import json

    admin.post(f'/receipts/{photo_receipt.id}/correct', data={'vendor_tin': '103470362'})
    admin.post(f'/receipts/{photo_receipt.id}/correct', data={'total_incl_tax_cents': '80000'})

    receipt = db.session.get(Receipt, photo_receipt.id)
    # Cumulative: the second correction does not erase the record of the first.
    assert json.loads(receipt.corrected_fields) == ['Total incl. tax', 'Vendor TIN']
    assert receipt.corrected_at is not None


def test_a_receipt_from_tras_own_page_is_not_editable(app, admin, device):
    """
    The pipeline's whole premise is that TRA's numbers beat anybody's reading of them.
    A verified receipt that looks wrong is a parser problem, and letting it be typed over
    here would hide exactly the bug worth finding.
    """
    submission = Submission(
        device_id=device.id, input_type='url', input_data='x', status='completed',
    )
    db.session.add(submission)
    db.session.commit()
    receipt = Receipt(
        device_id=device.id, submission_id=submission.id, extraction_source='tra_html',
        vendor_name='PLASCO LIMITED', total_incl_tax_cents=100,
    )
    db.session.add(receipt)
    db.session.commit()

    response = admin.post(f'/receipts/{receipt.id}/correct', data={'vendor_name': 'Something else'})

    assert response.status_code == 409
    assert db.session.get(Receipt, receipt.id).vendor_name == 'PLASCO LIMITED'


def test_two_receipts_cannot_be_given_the_same_verification_code(app, admin, device, photo_receipt):
    """
    The code is the receipt's identity and what a second submission of it is caught on.
    Typing one that is already taken is nearly always the same purchase entered twice, so
    it is worth saying that rather than failing on a constraint.
    """
    other_submission = Submission(
        device_id=device.id, input_type='url', input_data='x', status='completed',
    )
    db.session.add(other_submission)
    db.session.commit()
    other = Receipt(
        device_id=device.id, submission_id=other_submission.id,
        receipt_verification_code='58E41A514',
    )
    db.session.add(other)
    db.session.commit()

    response = admin.post(f'/receipts/{photo_receipt.id}/correct', data={
        'receipt_verification_code': '58E41A514',
    })

    assert response.status_code == 409
    assert f'#{other.id}' in response.json['error']


def test_correcting_the_code_and_verifying_replaces_the_transcription(
        app, admin, photo_receipt, portal, judgment):
    """
    The ending the form is built to reach: what was typed is thrown away in favour of
    TRA's own page. The transcription must not survive alongside it - one submission
    holds one receipt, and two rows for one purchase is the failure mode being avoided.
    """
    fetched = portal()

    response = admin.post(f'/receipts/{photo_receipt.id}/correct', data={
        'receipt_verification_code': '58E41A514', 'receipt_time': '09:20:22',
        'total_incl_tax_cents': '76000', 'verify': '1',
    })

    assert response.status_code == 200
    assert response.json['verified'] is True
    assert fetched == ['https://verify.tra.go.tz/58E41A514_092022']

    receipt = Receipt.query.one()
    assert receipt.extraction_source == 'tra_html'
    assert receipt.vendor_name == 'PLASCO LIMITED'
    # The hand-typed total is gone, superseded by the portal's.
    assert receipt.total_incl_tax_cents == 2_427_300_000
    # The reading of the photograph was a different row, and the page has to be told
    # where its replacement lives rather than reloading an address that may have gone.
    assert response.json['receipt_id'] == receipt.id


def test_a_photo_receipt_survives_a_portal_that_will_not_answer(
        app, admin, photo_receipt, portal):
    """
    A receipt we can already read is not thrown away because the portal is down. The
    upgrade simply did not happen, and the corrections still saved.
    """
    portal(error=TraReceiptNotUploaded('vendor has not uploaded it'))

    response = admin.post(f'/receipts/{photo_receipt.id}/correct', data={
        'receipt_verification_code': '58E41A514', 'receipt_time': '09:20:22',
        'vendor_name': 'EFRAIM MOTORS', 'verify': '1',
    })

    assert response.json['verified'] is False
    receipt = db.session.get(Receipt, photo_receipt.id)
    assert receipt is not None
    assert receipt.vendor_name == 'EFRAIM MOTORS'
    assert receipt.extraction_source == 'llm_vision'
    assert db.session.get(Submission, receipt.submission_id).status == 'completed'


# --- The pages that offer all this ------------------------------------------

def test_the_submission_page_offers_the_two_fields_next_to_the_photograph(
        app, admin, stuck_photo):
    """
    The form is only usable beside the paper it is read off, and it opens by itself when
    the portal has said in so many words that the time is wrong - which is the one case
    where nothing moves until a person acts.
    """
    body = admin.get(f'/submissions/{stuck_photo.id}').get_data(as_text=True)

    assert 'The address we ask TRA for' in body
    assert 'Verification code' in body and 'Receipt time' in body
    # Prefilled with what is already known, so a wrong digit is edited, not retyped.
    assert '"58E41A514"' in body and '"09:11:22"' in body
    assert 'editing: true' in body


@pytest.mark.skipif(not shutil.which('node'), reason='node is not installed')
def test_the_correction_form_opens_with_its_buttons_live(app, admin, photo_receipt, tmp_path):
    """
    Both buttons must be pressable the moment the form opens, and this is not something
    reading the HTML can tell you.

    The failure it was written for: Alpine sets a boolean attribute for any value that is
    not null, undefined or false - an empty string included. A `busy: ''` idle state
    therefore renders `:disabled="busy"` as disabled="disabled" on a form that has done
    nothing yet, and the whole panel is dead on arrival while looking perfectly correct
    in the template. So the component is built and its guards evaluated the way the
    browser evaluates them.
    """
    from datetime import time

    # A receipt the model did read a code and a time off, so asking TRA is on the table
    # and both buttons should be live.
    photo_receipt.receipt_verification_code = '010FF9418267'
    photo_receipt.receipt_time = time(10, 47, 40)
    db.session.commit()

    body = admin.get(f'/receipts/{photo_receipt.id}').get_data(as_text=True)
    collector = ScriptCollector()
    collector.feed(body)

    driver = """
    const component = receiptPage();
    const alpineWouldDisable = value => ![null, undefined, false].includes(value);
    console.log(JSON.stringify({
        saveDisabled: alpineWouldDisable(!!component.busy),
        verifyDisabled: alpineWouldDisable(!component.preview || !!component.busy),
        previewWhileWorking: (component.busy = 'verify', alpineWouldDisable(!!component.busy)),
        preview: (component.busy = null, component.preview),
    }));
    """
    bundle = tmp_path / 'receipt.js'
    bundle.write_text('\n'.join(collector.scripts) + driver)

    result = subprocess.run(['node', str(bundle)], capture_output=True, text=True, timeout=30)
    assert result.returncode == 0, result.stderr

    state = json.loads(result.stdout)
    assert state['saveDisabled'] is False
    assert state['verifyDisabled'] is False
    # And they do still lock while a request is in flight, which is the point of the flag.
    assert state['previewWhileWorking'] is True
    # The address the fields build, offered to the portal only once it is whole.
    assert state['preview'] == 'https://verify.tra.go.tz/010FF9418267_104740'


def test_the_receipt_page_offers_the_pencil_only_where_it_is_allowed(
        app, admin, photo_receipt, device):
    """A photograph's reading is correctable; TRA's own page is not."""
    body = admin.get(f'/receipts/{photo_receipt.id}').get_data(as_text=True)
    assert 'Correct by hand' in body
    assert 'Read off the photograph by the model' in body

    submission = Submission(
        device_id=device.id, input_type='url', input_data='x', status='completed',
    )
    db.session.add(submission)
    db.session.commit()
    verified = Receipt(
        device_id=device.id, submission_id=submission.id, extraction_source='tra_html',
    )
    db.session.add(verified)
    db.session.commit()

    body = admin.get(f'/receipts/{verified.id}').get_data(as_text=True)
    assert 'Correct by hand' not in body
    assert 'As TRA printed it' in body
