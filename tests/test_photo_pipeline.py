# tests/test_photo_pipeline.py
"""
What happens to a photographed receipt, which is the weakest input this app takes.

A photo used to mean one thing: hand it to a vision model and store whatever came
back. These cover the layers now in front of that - the QR code read server-side, and
the verification code the model transcribes off the paper - because the whole point of
them is to produce a *verified* receipt from an image, and the difference between
'verified' and 'a model read it' is invisible in the ledger unless something checks.

The portal and the model are both stubbed. What is being tested is which of them gets
asked, in what order, and what is stored when one of them will not answer.
"""


import pytest
from PIL import Image

from models.user import db, Receipt, Submission
from utils.llm_processor import reconstructed_receipt_url
from utils.tra import TraReceiptNotUploaded


RECEIPT_URL = 'https://verify.tra.go.tz/58E41A514_092022'


def qr_finds(monkeypatch, url):
    """
    Points the server-side decoder at a fixed answer. `None` means it read nothing.

    Stubbed as the whole report rather than just the URL, because the report is stored
    on the submission and the pages read it back - a test that faked only the return
    value would leave the recording half-exercised.
    """
    import main

    report = {
        'outcome': 'decoded' if url else 'no_code', 'url': url,
        'texts': [url] if url else [], 'ignored': [],
        'pass': 'plain' if url else None, 'attempts': 1 if url else 11,
        'ms': 40, 'image': '1500x2000', 'detail': '',
    }
    monkeypatch.setattr(main.qr, 'scan', lambda path: report)
    return report


@pytest.fixture
def configured(app):
    """An instance that has an LLM provider, which the photo path requires."""
    from models.user import InstanceConfig

    config = InstanceConfig(
        admin_email='admin@example.com', totp_secret='SECRET',
        llm_provider='groq', llm_api_key='test-key',
    )
    db.session.add(config)
    db.session.commit()
    return config


@pytest.fixture
def photo(app, device):
    """
    Queues a photo submission with a real image file behind it.

    A real file, because the first thing the pipeline does now is open it: a fixture
    that only wrote a database row would exercise none of the decode path.
    """
    import main
    import os

    def _submit(image=None, description=None):
        image = image or Image.new('RGB', (64, 64), 'white')
        filename = f'test-{os.urandom(4).hex()}.jpg'
        image.save(os.path.join(main.app.config['UPLOAD_FOLDER'], filename), 'JPEG')

        submission = Submission(
            device_id=device.id, input_type='photo', input_data=filename,
            description=description,
        )
        db.session.add(submission)
        db.session.commit()
        return submission
    return _submit


@pytest.fixture
def vision(monkeypatch):
    """Stubs the vision model, and records whether it was consulted at all."""
    import main

    calls = []

    def _stub(**fields):
        data = {
            'vendor_name': 'PLASCO LIMITED', 'receipt_date': '2025-06-01',
            'total_amount': 118000, 'document_type': 'tra_efd_receipt',
            'llm_extracted_description': 'Plastic sheeting for the workshop.',
            'llm_tax_analysis': 'Deductible as a running cost.',
        }
        data.update(fields)

        def _extract(content, is_image, config):
            calls.append(content)
            return data
        monkeypatch.setattr(main, 'extract_receipt_details', _extract)
        return calls
    return _stub


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


# --- Layer 1: the QR code, read on the server -------------------------------

def test_a_photo_whose_qr_decodes_is_verified_against_tra(
        app, configured, photo, portal, vision, judgment, monkeypatch):
    """
    The point of the whole exercise: a photograph becomes an exact receipt.

    The phone could not read this code - that is why it arrived as a photo - but a
    still at full resolution is a different proposition from a preview frame, so the
    server tries again before falling back to anything.
    """
    import main

    qr_finds(monkeypatch, RECEIPT_URL)
    fetched = portal()
    asked = vision()

    submission = photo()
    main.process_submission(submission)

    receipt = Receipt.query.one()
    assert receipt.extraction_source == 'tra_html'
    assert receipt.vendor_name == 'PLASCO LIMITED'
    # The numbers came from the portal, so the model was never asked to read any.
    assert asked == []
    assert fetched == [RECEIPT_URL]
    assert submission.status == 'completed'
    # And the photo is still the submission's input; only its facts changed hands.
    assert submission.input_type == 'photo'
    assert submission.receipt_code == '58E41A514'


def test_a_verified_photo_is_recognised_as_a_duplicate_of_the_same_receipt(
        app, configured, photo, portal, vision, judgment, monkeypatch):
    """Photographing a receipt that was already scanned must not book it twice."""
    import main

    qr_finds(monkeypatch, RECEIPT_URL)
    portal()
    vision()

    main.process_submission(photo())
    second = photo()
    main.process_submission(second)

    assert Receipt.query.count() == 1
    assert second.status == 'duplicate'


# --- Layer 2: the code the model reads off the paper ------------------------

def test_the_transcribed_code_and_time_rebuild_the_verification_url(
        app, configured, photo, portal, vision, judgment, monkeypatch):
    """
    No QR at all - creased, torn, or under a thumb - and the receipt still verifies.

    This is the case the vision prompt exists for. The code and the time are printed in
    plain characters at the foot of the receipt and survive damage that stops the code
    above them scanning.
    """
    import main

    qr_finds(monkeypatch, None)
    fetched = portal()
    vision(receipt_verification_code='58E41A514', receipt_time='09:20:22')

    submission = photo()
    main.process_submission(submission)

    receipt = Receipt.query.one()
    assert receipt.extraction_source == 'tra_html'
    assert fetched == [RECEIPT_URL]
    assert submission.recovered_url == RECEIPT_URL


def test_a_photo_falls_back_to_what_the_model_read_when_tra_gives_up(
        app, configured, photo, portal, vision, judgment, monkeypatch):
    """
    A portal that will not confirm the receipt must not cost us the receipt.

    A URL submission has nowhere else to go and fails. A photograph does: the image is
    still in hand and the model has already read it, so the transcription is stored and
    marked as a transcription.
    """
    import main

    qr_finds(monkeypatch, None)
    portal(error=TraReceiptNotUploaded('not uploaded'))
    vision(receipt_verification_code='58E41A514', receipt_time='09:20:22')

    submission = photo()
    # Out of retries already, so this attempt is the last one.
    submission.retry_count = 99
    db.session.commit()

    main.process_submission(submission)

    receipt = Receipt.query.one()
    assert receipt.extraction_source == 'llm_vision'
    assert receipt.vendor_name == 'PLASCO LIMITED'
    assert submission.status == 'completed'
    # What we tried is still on the record, which is what turns "could not read it"
    # into "we know which receipt this is and TRA has not got it".
    assert submission.recovered_url == RECEIPT_URL
    assert submission.failure_reason == 'TraReceiptNotUploaded'


def test_a_retry_reuses_the_recovered_url_instead_of_paying_to_find_it_again(
        app, configured, photo, portal, vision, judgment, monkeypatch):
    """A scheduled retry must not re-run the decoder and the vision model."""
    import main

    def _never(path):
        pytest.fail('The QR decoder ran again on a submission that already had a URL.')

    portal()
    asked = vision()
    monkeypatch.setattr(main.qr, 'scan', _never)

    submission = photo()
    submission.recovered_url = RECEIPT_URL
    db.session.commit()

    main.process_submission(submission)

    assert Receipt.query.one().extraction_source == 'tra_html'
    assert asked == []


def test_a_retryable_portal_failure_still_schedules_a_retry(
        app, configured, photo, portal, vision, judgment, monkeypatch):
    """
    The fallback is a last resort, not a first one.

    A vendor who has not uploaded yet usually uploads within the hour, and the exact
    receipt is worth waiting for - so the first failure books a retry rather than
    settling immediately for what the model read.
    """
    import main

    qr_finds(monkeypatch, RECEIPT_URL)
    portal(error=TraReceiptNotUploaded('not uploaded'))
    asked = vision()

    submission = photo()
    main.process_submission(submission)

    assert Receipt.query.count() == 0
    assert submission.status == 'queued'
    assert submission.next_attempt_at is not None
    assert asked == []


# --- Layer 3: documents that are not EFD receipts at all --------------------

def test_a_non_efd_document_is_recorded_as_one_rather_than_chased(
        app, configured, photo, portal, vision, judgment, monkeypatch):
    """
    A parking stub has no verification code and never will.

    Sending it to the portal would spend a request to learn what the model already
    said, and storing it as an EFD receipt that failed verification describes it
    wrongly: it is a real expense with no input VAT behind it.
    """
    import main

    qr_finds(monkeypatch, None)
    fetched = portal()
    vision(
        document_type='other_receipt', vendor_name='City Parking',
        receipt_verification_code='NOTACODE', receipt_time='11:00:00',
        llm_extracted_description='Parking for the day.',
    )

    submission = photo()
    main.process_submission(submission)

    receipt = Receipt.query.one()
    assert receipt.document_type == 'other_receipt'
    assert receipt.extraction_source == 'llm_vision'
    assert fetched == []
    assert submission.status == 'completed'


# --- Rebuilding the URL -----------------------------------------------------

@pytest.mark.parametrize('code, time_text, expected', [
    ('58E41A514', '09:20:22', 'https://verify.tra.go.tz/58E41A514_092022'),
    # Printed without seconds, which some EFDs do. The portal wants six digits.
    ('58E41A514', '09:20', 'https://verify.tra.go.tz/58E41A514_092000'),
    # Spacing and stray punctuation in a transcription are not a reason to give up.
    (' 58E41A514 ', '9:20:22 ', 'https://verify.tra.go.tz/58E41A514_092022'),
    ('04B27C2193133', '14:06:57', 'https://verify.tra.go.tz/04B27C2193133_140657'),
])
def test_a_readable_code_and_time_rebuild_the_portal_url(code, time_text, expected):
    assert reconstructed_receipt_url({
        'receipt_verification_code': code, 'receipt_time': time_text,
    }) == expected


@pytest.mark.parametrize('data', [
    {'receipt_verification_code': '58E41A514'},                       # No time printed.
    {'receipt_time': '09:20:22'},                                     # No code read.
    {'receipt_verification_code': '58E41A514', 'receipt_time': '99:99:99'},
    {'receipt_verification_code': '58E41A514', 'receipt_time': 'about nine'},
    {},
])
def test_a_half_read_code_is_not_sent_to_the_portal(data):
    """
    A guessed code does not fail safe - it lands on somebody else's receipt. Missing
    beats plausible, every time.
    """
    assert reconstructed_receipt_url(data) is None


# --- The decoder itself -----------------------------------------------------

def _photographed_qr(text, size=240, blur=0.8):
    """A QR code the way one arrives: small, on paper, slightly soft."""
    import qrcode
    from PIL import ImageFilter

    code = qrcode.QRCode(box_size=3, border=2)
    code.add_data(text)
    code.make()

    page = Image.new('L', (size * 2, size * 3), 235)
    page.paste(code.make_image().convert('L').resize((size, size), Image.LANCZOS),
               (size // 2, size))
    return page.filter(ImageFilter.GaussianBlur(blur))


@pytest.mark.skipif(not __import__('utils.qr', fromlist=['qr']).is_available(),
                    reason='zxing-cpp is not installed')
def test_a_photographed_qr_code_is_read_off_the_image(tmp_path):
    from utils.qr import find_receipt_url

    path = tmp_path / 'receipt.jpg'
    _photographed_qr(RECEIPT_URL).convert('RGB').save(path, 'JPEG', quality=70)

    assert find_receipt_url(str(path)) == RECEIPT_URL


@pytest.mark.skipif(not __import__('utils.qr', fromlist=['qr']).is_available(),
                    reason='zxing-cpp is not installed')
def test_a_qr_code_that_is_not_a_receipt_is_ignored(tmp_path):
    """
    A poster, a wifi barcode or a payment code in shot must not be queued as a
    receipt. The portal client's own parser is the gate.
    """
    from utils.qr import find_receipt_url

    path = tmp_path / 'poster.jpg'
    _photographed_qr('https://example.com/menu').convert('RGB').save(path, 'JPEG')

    assert find_receipt_url(str(path)) is None


def test_a_missing_or_unreadable_file_is_not_a_crash(tmp_path):
    """Decoding runs inside receipt processing; it may never take a submission down."""
    from utils.qr import decode_texts, find_receipt_url

    assert decode_texts(str(tmp_path / 'nothing-here.jpg')) == []

    junk = tmp_path / 'junk.jpg'
    junk.write_bytes(b'not an image')
    assert find_receipt_url(str(junk)) is None

    # A file that is an image and is still nothing: an upload cut off mid-transfer, or
    # a thumbnail where a photograph was meant. The ladder resizes and filters whatever
    # it is handed, and there is next to nothing here to resize.
    stub = tmp_path / 'one-pixel.png'
    Image.new('L', (1, 1), 255).save(stub)
    assert decode_texts(str(stub)) == []


@pytest.mark.skipif(not __import__('utils.qr', fromlist=['qr']).is_available(),
                    reason='zxing-cpp is not installed')
def test_a_code_lit_unevenly_is_read_off_a_difficult_frame(tmp_path):
    """
    A receipt small in the frame, low in contrast and lit unevenly - all at once.

    This is the ordinary indoor photograph rather than a contrived one, and it is the
    arrangement the first version of this decoder was built around: it was assumed that
    what such a frame needed was to be cropped up, so that the black and white points
    came from the receipt rather than from the desk, the floor and the window behind.

    That assumption was wrong, and measurably - the tile passes it produced never once
    read a code the whole-frame passes had not. What this frame actually needs is its
    edges put back; see the module docstring in utils/qr.

    Deliberately a whole-decoder test rather than a check that a particular pass ran:
    what matters is that this image decodes, however the ladder gets there.
    """
    from PIL import ImageChops, ImageEnhance
    from utils.qr import decode_texts

    code = ImageEnhance.Contrast(
        _photographed_qr(RECEIPT_URL, size=130, blur=1.4)).enhance(0.45)

    # A frame sixteen times the receipt's area, on a dark surface, with a light falling
    # steadily across it - the ordinary indoor photograph, and the one arrangement the
    # global passes have no answer to.
    frame = Image.new('L', (code.width * 4, code.height * 4), 60)
    frame.paste(code, (code.width, code.height))
    frame = ImageChops.add(
        frame, Image.linear_gradient('L').resize(frame.size).point(lambda v: int(v * 0.8)))

    path = tmp_path / 'under-a-window.jpg'
    frame.convert('RGB').save(path, 'JPEG', quality=75)

    assert RECEIPT_URL in decode_texts(str(path))


def test_the_ladder_never_asks_the_decoder_something_it_already_tries(tmp_path):
    """
    Inversion, rotation and downscaling are ZXing's own defaults.

    A pass for any of them would double the cost of every failed decode to ask a
    question the library answered on the first one - and a ladder that grows a pass per
    guess is exactly how that happens without anyone noticing.

    The local binarizer is the same kind of duplication one step further out: it already
    thresholds each region against its neighbourhood, which is what a pass that cropped
    the frame into tiles was for. Measured over 84 photographed receipts, those nine
    passes read nothing the whole-frame passes had not - so their absence is asserted
    here, where a well-meant reintroduction would be noticed.
    """
    from utils import qr

    page = Image.new('L', (900, 1200), 235)
    names = [name for name, _ in qr._variants(page)]

    assert 'inverted' not in names
    assert names[0] == 'plain'
    assert names.count('plain') == 1
    assert not [name for name in names if 'tile' in name]


def test_the_upscale_passes_are_bounded_by_a_pixel_budget():
    """
    A photograph tripled is a 75-megapixel allocation, on a worker holding it in memory.

    The budget is what keeps the desperation passes affordable at photograph sizes, and
    it has to bite where it matters: a phone-sized frame can afford both upscales, a
    large one only the first, and neither may be skipped for the small frames that need
    them most.
    """
    from utils import qr

    # A viewfinder-sized frame, and the largest an upload can be: Scanner.toJpeg caps an
    # undecoded photo at 3000 on its long edge, so this is the real upper end rather
    # than a hypothetical one.
    small = [name for name, _ in qr._variants(Image.new('L', (720, 1280), 235))]
    large = [name for name, _ in qr._variants(Image.new('L', (2250, 3000), 235))]

    assert '2x and sharpened' in small and '3x and sharpened' in small
    assert '2x and sharpened' in large and '3x and sharpened' not in large

    for size in ((720, 1280), (2250, 3000), (3000, 4000)):
        for _name, image in qr._variants(Image.new('L', size, 235)):
            assert image.width * image.height <= qr.MAX_UPSCALED_PIXELS


@pytest.mark.skipif(not __import__('utils.qr', fromlist=['qr']).is_available(),
                    reason='zxing-cpp is not installed')
def test_a_code_too_soft_for_the_plain_pass_is_read_by_a_sharpened_one(tmp_path):
    """
    The change that made this decoder worth running, guarded against being tuned away.

    Softness - a hand-held frame, a shallow focus, a JPEG - is the defect that survives
    every other pass here, because it is the one thing neither a binarizer nor a resize
    addresses. A frame blurred past what the first pass will read must still come back,
    and it must come back on a pass that sharpened it: if this ever decodes on 'plain'
    the fixture has gone soft rather than the ladder having improved.
    """
    from utils import qr

    # A 90px code under a 1.5px blur sits in the band this change opened up: the plain
    # pass loses it from about 1.2 and nothing reads it past about 1.8, so it is
    # comfortably inside the window rather than balanced on either edge of it.
    frame = _photographed_qr(RECEIPT_URL, size=90, blur=1.5)
    path = tmp_path / 'soft.jpg'
    frame.convert('RGB').save(path, 'JPEG', quality=80)

    report = qr.scan(str(path))

    assert report['url'] == RECEIPT_URL
    assert 'sharpened' in report['pass']


# --- What the decoder says it did -------------------------------------------
#
# The reason this exists at all: a photograph read by the vision model has walked past
# the decoder on the way, and until the attempt was written down, a decoder that found
# nothing, a decoder that could not open the file and no decoder at all were the same
# blank space on the submission page.

@pytest.mark.skipif(not __import__('utils.qr', fromlist=['qr']).is_available(),
                    reason='zxing-cpp is not installed')
def test_a_scan_says_what_it_read_and_what_it_cost(tmp_path):
    from utils.qr import scan

    path = tmp_path / 'receipt.jpg'
    _photographed_qr(RECEIPT_URL).convert('RGB').save(path, 'JPEG', quality=70)

    report = scan(str(path))
    assert report['outcome'] == 'decoded'
    assert report['url'] == RECEIPT_URL
    assert report['pass']
    assert report['attempts'] >= 1
    # The dimensions actually decoded, which is how an upload starved by the client's
    # own downscale gets spotted from the admin page rather than from a hunch.
    assert report['image'] == '480x720'


@pytest.mark.skipif(not __import__('utils.qr', fromlist=['qr']).is_available(),
                    reason='zxing-cpp is not installed')
def test_a_code_that_is_not_a_receipt_is_reported_rather_than_dropped(tmp_path):
    """
    'A code was read and it was a menu' and 'no code was read' need different retakes,
    so the report distinguishes them even though the pipeline treats both as no URL.
    """
    from utils.qr import scan

    path = tmp_path / 'poster.jpg'
    _photographed_qr('https://example.com/menu').convert('RGB').save(path, 'JPEG')

    report = scan(str(path))
    assert report['outcome'] == 'not_a_receipt'
    assert report['url'] is None
    assert report['ignored'] == ['https://example.com/menu']


def test_an_unopenable_upload_is_reported_as_that_and_not_as_a_bare_photo(tmp_path):
    """A truncated upload is an infrastructure problem wearing a photography problem."""
    from utils.qr import scan, summarise

    junk = tmp_path / 'junk.jpg'
    junk.write_bytes(b'not an image')

    report = scan(str(junk))
    assert report['outcome'] == 'unreadable'
    assert summarise(report)['tone'] == 'bad'


def test_a_missing_decoder_is_reported_as_an_install_not_a_bad_photograph(
        tmp_path, monkeypatch):
    """
    The failure that is nobody's fault on the page it appears on.

    Without a wheel every photographed receipt silently becomes a transcription, and the
    submissions look exactly like ones whose codes were unreadable - so the one place it
    can be noticed says which it is, and points at the thing that fixes it.
    """
    from utils import qr

    monkeypatch.setattr(qr, 'zxingcpp', None)
    monkeypatch.setattr(qr, '_IMPORT_ERROR', ImportError('No module named zxingcpp'))

    report = qr.scan(str(tmp_path / 'anything.jpg'))
    assert report['outcome'] == 'unavailable'
    assert 'zxing-cpp' in report['detail']
    assert qr.summarise(report)['tone'] == 'bad'


def test_the_decoder_records_what_it_saw_on_the_submission(
        app, configured, photo, portal, vision, judgment, monkeypatch):
    """
    An admin asking 'did the server even try?' is asking about a specific submission.
    """
    import json
    import main

    qr_finds(monkeypatch, RECEIPT_URL)
    portal()
    vision()

    submission = photo()
    main.process_submission(submission)

    assert json.loads(submission.qr_scan)['url'] == RECEIPT_URL
    assert main._stored_qr_scan(submission)['outcome'] == 'decoded'


def test_a_photo_the_decoder_missed_still_says_so_on_the_submission(
        app, configured, photo, portal, vision, judgment, monkeypatch):
    """The case that prompted all of this: the receipt was read, but not by the QR."""
    import main

    qr_finds(monkeypatch, None)
    portal(error=TraReceiptNotUploaded('not uploaded'))
    vision()

    submission = photo()
    submission.retry_count = 99
    db.session.commit()
    main.process_submission(submission)

    scan = main._stored_qr_scan(submission)
    assert scan['outcome'] == 'no_code'
    assert scan['attempts'] == 11
    assert Receipt.query.one().extraction_source == 'llm_vision'


@pytest.fixture
def admin(app, configured):
    client = app.test_client()
    with client.session_transaction() as session:
        session['admin_logged_in'] = True
    return client


def test_the_pages_show_what_the_decoder_did(
        app, configured, photo, portal, vision, judgment, monkeypatch, admin):
    """
    The whole reason the report is stored rather than printed to a log nobody reading
    these pages can see.

    Asserted on the receipt page as well as the submission page because a photograph
    the model read still produces a receipt, and 'why is this one a transcription and
    not a verified receipt' is a question asked in front of the receipt.
    """
    import main

    qr_finds(monkeypatch, None)
    portal(error=TraReceiptNotUploaded('not uploaded'))
    vision()

    submission = photo()
    submission.retry_count = 99
    db.session.commit()
    main.process_submission(submission)

    # A read photo redirects to its receipt; both pages carry the same panel.
    body = admin.get(f'/submissions/{submission.id}',
                     follow_redirects=True).get_data(as_text=True)

    assert 'Server-side QR scan' in body
    assert 'No QR code could be read' in body
    assert '1500x2000' in body          # what the server was actually handed
    assert '11' in body                 # passes spent proving it


def test_a_failed_photo_says_the_decoder_was_never_installed(
        app, configured, photo, monkeypatch, admin):
    """
    The one outcome nobody photographing a receipt can do anything about, on the page
    where it would otherwise read as an unreadable code.
    """
    import json

    submission = photo()
    submission.status = 'failed'
    submission.qr_scan = json.dumps({
        'outcome': 'unavailable', 'url': None, 'texts': [], 'ignored': [],
        'pass': None, 'attempts': 0, 'ms': 0, 'image': None,
        'detail': 'zxing-cpp is not installed (No module named zxingcpp).',
    })
    db.session.commit()

    body = admin.get(f'/submissions/{submission.id}').get_data(as_text=True)

    assert 'decoder is not installed' in body
    assert 'No module named zxingcpp' in body


# --- Asking the decoder again -----------------------------------------------
#
# A photo is scanned once, on its way through the queue, and the verdict it got then is
# the verdict it keeps. That is the wrong number of times whenever the decoder improves
# or the capture that fed it was at fault - which is precisely the pair of things that
# has just happened - so the scan has to be re-runnable against a photograph already on
# disk, and a code found the second time has to be worth as much as one found the first.

def test_a_rescan_replaces_the_stored_verdict_with_a_fresh_one(
        app, configured, photo, portal, vision, judgment, monkeypatch, admin):
    """
    The decoder improved; the submission is still carrying what the old one concluded.
    """
    import json
    import main

    qr_finds(monkeypatch, None)
    portal(error=TraReceiptNotUploaded('not uploaded'))
    vision()

    submission = photo()
    submission.retry_count = 99
    db.session.commit()
    main.process_submission(submission)
    assert main._stored_qr_scan(submission)['outcome'] == 'no_code'

    # The same photograph, through a decoder that can now read it.
    qr_finds(monkeypatch, RECEIPT_URL)
    portal()

    response = admin.post(f'/submissions/{submission.id}/rescan')

    assert response.status_code == 200
    assert response.get_json()['scan']['url'] == RECEIPT_URL
    assert json.loads(submission.qr_scan)['outcome'] == 'decoded'


def test_a_code_found_by_a_rescan_is_taken_to_the_portal_not_just_displayed(
        app, configured, photo, portal, vision, judgment, monkeypatch, admin):
    """
    The whole value of reading the code is the verified receipt on the other side of it.

    A rescan that only refreshed a diagnostic panel would leave an admin looking at
    'the code was read' beside a receipt still marked as a transcription, with no way to
    act on it - which is the state this button exists to get out of.
    """
    import main

    qr_finds(monkeypatch, None)
    portal(error=TraReceiptNotUploaded('not uploaded'))
    vision()

    submission = photo()
    submission.retry_count = 99
    db.session.commit()
    main.process_submission(submission)
    assert Receipt.query.one().extraction_source == 'llm_vision'

    qr_finds(monkeypatch, RECEIPT_URL)
    fetched = portal()

    payload = admin.post(f'/submissions/{submission.id}/rescan').get_json()

    assert payload['verified'] is True
    assert fetched, 'the decoded code was never put to TRA'
    assert Receipt.query.one().extraction_source == 'tra_html'


def test_a_rescan_of_something_with_no_photograph_is_refused_not_crashed(
        app, configured, device, photo, admin):
    """
    Two ways to ask for a scan of nothing, both reachable and neither an error worth a
    stack trace: a URL submission was never a photograph, and a photograph whose file
    has gone is the ordinary state of an instance whose uploads volume was remounted.
    """
    import main
    import os

    typed = Submission(device_id=device.id, input_type='url', input_data=RECEIPT_URL)
    db.session.add(typed)
    db.session.commit()

    assert admin.post(f'/submissions/{typed.id}/rescan').status_code == 409

    orphan = photo()
    os.remove(main.submission_photo_path(orphan))
    assert admin.post(f'/submissions/{orphan.id}/rescan').status_code == 409

    assert admin.post('/submissions/99999/rescan').status_code == 404


def test_the_rescan_button_is_offered_wherever_the_scan_is_shown(
        app, configured, photo, portal, vision, judgment, monkeypatch, admin):
    """
    The panel is included by two pages with two different Alpine scopes, and the button
    is only useful on the page an admin happens to be looking at the photograph from.
    """
    import main

    qr_finds(monkeypatch, None)
    portal(error=TraReceiptNotUploaded('not uploaded'))
    vision()

    submission = photo()
    submission.retry_count = 99
    db.session.commit()
    main.process_submission(submission)

    body = admin.get(f'/submissions/{submission.id}',
                     follow_redirects=True).get_data(as_text=True)

    assert 'Scan again' in body
    assert '/rescan' in body
    assert f'qrRescan({submission.id})' in body


# --- What gets kept off an upload ---------------------------------------------
#
# The app takes a large photo and stores a small one. Those are two different numbers
# answering two different questions, and both were previously unset: there was no body
# limit at all, so the effective one belonged to whatever proxy sat in front of gunicorn
# (a bare 413 the phone could not explain and would not stop retrying), and photo.save()
# wrote whatever arrived, forever, at whatever resolution a sensor felt like.

def _jpeg_bytes(size, colour='white', exif_orientation=None):
    """A real JPEG, because every assertion here is about what Pillow reads back."""
    import io

    image = Image.new('RGB', size, colour)
    # Some texture, so `optimize=True` has something to do and the encoded size is not
    # a degenerate flat-field case.
    for x in range(0, size[0], 7):
        for y in range(0, size[1], 11):
            image.putpixel((x, y), ((x * 3) % 256, (y * 5) % 256, 90))

    buffer = io.BytesIO()
    if exif_orientation is not None:
        exif = image.getexif()
        exif[0x0112] = exif_orientation
        image.save(buffer, 'JPEG', quality=95, exif=exif)
    else:
        image.save(buffer, 'JPEG', quality=95)
    return buffer.getvalue()


def _upload(app, device, data, filename='receipt.jpg'):
    """Posts to the device API and returns the stored file's path."""
    import io
    import os

    import main
    from models.user import Submission

    client = app.test_client()
    response = client.post(
        '/receipt',
        data={'receiptphoto': (io.BytesIO(data), filename)},
        headers={'Authorization': f'Bearer {device.api_key}'},
        content_type='multipart/form-data',
    )
    assert response.status_code == 202, response.get_data(as_text=True)
    submission = Submission.query.order_by(Submission.id.desc()).first()
    return os.path.join(main.app.config['UPLOAD_FOLDER'], submission.input_data)


def test_a_sensor_sized_photograph_is_stored_at_a_size_something_can_use(app, device):
    """
    A 12MP frame off a phone is not 12MP worth of receipt.

    utils.qr bounds every photograph it opens to MAX_EDGE before it looks at it, so
    pixels above that line are read off disk and discarded on every pass of the decode
    ladder; the vision model is billed for base64-encoding them; and the persistence
    volume carries them for the life of the instance. Nothing anywhere can use them.
    """
    from utils.images import STORED_MAX_EDGE

    original = _jpeg_bytes((4032, 3024))
    path = _upload(app, device, original)

    with Image.open(path) as stored:
        assert max(stored.size) == STORED_MAX_EDGE
        # Bounded, not reshaped: a receipt squeezed to a different aspect ratio is a
        # receipt the decoder's geometry assumptions no longer hold for.
        assert abs(stored.width / stored.height - 4032 / 3024) < 0.01
        assert stored.format == 'JPEG'

    import os
    assert os.path.getsize(path) < len(original)


def test_a_photo_the_scanner_already_bounded_is_stored_byte_for_byte(app, device):
    """
    The common path must not be re-encoded.

    JPEG loses something on every generation, and the scanner already caps its own
    uploads - 2000px, or 3000px for a frame whose code it could not read. Putting those
    through an encoder again would spend real QR modules to save nothing, and the
    modules it would spend are exactly the marginal ones utils.qr exists to rescue.
    """
    original = _jpeg_bytes((3000, 2250))
    path = _upload(app, device, original)

    with open(path, 'rb') as handle:
        assert handle.read() == original, 'an already-small photo was re-encoded'


def test_a_photograph_recorded_on_its_side_is_stored_upright(app, device):
    """
    Phones write orientation into EXIF instead of rotating the pixels.

    The QR decoder calls exif_transpose itself, but the vision model does not: it is
    handed the file base64-encoded into a data URL exactly as stored. So a sideways
    photograph was being read sideways by the one consumer that has to recognise words
    on paper. Storing upright pixels fixes that for good, and for everything downstream.
    """
    # 6 is 'rotate 90° clockwise to display', the usual portrait value.
    path = _upload(app, device, _jpeg_bytes((1200, 800), exif_orientation=6))

    with Image.open(path) as stored:
        assert stored.size == (800, 1200), 'the photo is still on its side'
        # And the tag is gone, so nothing turns it a second time.
        assert stored.getexif().get(0x0112) in (None, 0, 1)


def test_a_file_that_is_not_an_image_is_still_accepted_and_still_stored(app, device):
    """
    Refusing here would drop a receipt at the one moment nobody is watching.

    The device has already been told the upload worked; there is no second copy. The
    pipeline has a failure path with a reason on it, which is a far better place for
    'that was not a photograph' than a 400 the outbox would have to interpret.
    """
    path = _upload(app, device, b'this is not a JPEG', filename='receipt.jpg')

    with open(path, 'rb') as handle:
        assert handle.read() == b'this is not a JPEG'


def test_an_upload_past_the_limit_is_refused_in_words(app, device):
    """
    There was no MAX_CONTENT_LENGTH, so the real limit was whichever proxy happened to
    be in front of gunicorn - and it answered with an HTML page the phone's outbox could
    do nothing with except retry it, forever, byte for byte identical.
    """
    import io

    limit = app.config['MAX_CONTENT_LENGTH']
    assert limit == 20 * 1024 * 1024

    client = app.test_client()
    response = client.post(
        '/receipt',
        data={'receiptphoto': (io.BytesIO(b'x' * (limit + 1024)), 'huge.jpg')},
        headers={'Authorization': f'Bearer {device.api_key}'},
        content_type='multipart/form-data',
    )

    assert response.status_code == 413
    body = response.get_json()
    assert body['max_bytes'] == limit
    assert '20MB' in body['error']


@pytest.mark.skipif(not __import__('utils.qr', fromlist=['qr']).is_available(),
                    reason='zxing-cpp is not installed')
def test_bounding_an_upload_costs_no_code_the_decoder_would_have_read(app, device):
    """
    The one real risk in storing less than arrived.

    A verified receipt and a transcribed one are not the same receipt, and the whole
    difference is whether this code decodes - so a storage optimisation that quietly
    resampled a module away would be paid for in the ledger, invisibly, for as long as
    it took anyone to notice. The cap is utils.qr.MAX_EDGE for exactly this reason: it
    is the size that decoder bounds every photograph to anyway, so what reaches the
    ladder is the same image either way.

    Written against a code small in a large frame - the case with the least margin, and
    the one the scanner's own 3000px note was measured on.
    """
    import io
    from utils.qr import find_receipt_url

    # A 4800px frame, i.e. past the cap, with the code occupying a small part of it.
    page = _photographed_qr(RECEIPT_URL, size=600).convert('RGB')
    page = page.resize((page.width * 4, page.height * 4), Image.LANCZOS)
    assert max(page.size) > 4000, 'the fixture must exceed the cap to test anything'

    buffer = io.BytesIO()
    page.save(buffer, 'JPEG', quality=85)
    original = buffer.getvalue()

    # What the decoder gets today, straight off the wire...
    import os
    raw_path = os.path.join(app.config['UPLOAD_FOLDER'], 'unbounded.jpg')
    with open(raw_path, 'wb') as handle:
        handle.write(original)
    assert find_receipt_url(raw_path) == RECEIPT_URL, 'the fixture is not readable at all'

    # ...and what it gets after ingest bounds it. Same answer, or the cap is wrong.
    assert find_receipt_url(_upload(app, device, original)) == RECEIPT_URL


def test_a_re_encoded_upload_is_stored_under_a_name_that_matches_its_bytes(app, device):
    """
    A PNG comes out of the encoder as JPEG, and leaving those bytes under a .png name
    would make /uploads/<filename> serve them as image/png - send_from_directory types
    the response off the extension. Browsers sniff their way past that; there is no
    reason to depend on it, and llm_processor labels the data URL image/jpeg regardless.
    """
    import io
    import os

    from models.user import Submission

    buffer = io.BytesIO()
    Image.new('RGB', (900, 700), 'white').save(buffer, 'PNG')

    path = _upload(app, device, buffer.getvalue(), filename='receipt.png')
    assert path.endswith('.jpg')

    submission = Submission.query.order_by(Submission.id.desc()).first()
    assert submission.input_data.endswith('.jpg'), 'the database still names a .png'
    assert os.path.exists(path)
    with Image.open(path) as stored:
        assert stored.format == 'JPEG'
