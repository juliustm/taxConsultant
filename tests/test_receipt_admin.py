# tests/test_receipt_admin.py
"""
The three things an admin can do to a receipt that is already in the ledger, and the
guard rails on each.

  * Delete it. The only irreversible act on the page, and the only defensible answer to
    a purchase that should never have been in the book at all - the same receipt caught
    twice by two people, a personal lunch, a photograph of a wall the model gamely turned
    into an expense. Everything below about it is about not doing it by accident.

  * Read the document again. The figures on a photographed receipt are a model's reading
    of a crumpled thermal print, and some of those readings are simply wrong. A second
    look costs one call and fixes most of them, so it is offered - but it replaces every
    figure on the page, which is why it is refused outright on a receipt TRA verified and
    confirmed on the page before it runs.

  * Re-analyse it. Judgment only, on any receipt, and it never touches a figure.

And the setting that decides how much of all this is on screen: three densities, chosen
in Settings -> Business or from the receipt itself, where nothing is ever *removed* by
choosing a lower one.
"""
import json
import os
from datetime import date, time

import pytest

from models.user import (
    db, Device, InstanceConfig, Receipt, ReceiptItem, ReceiptTaxLine, Submission, Vendor,
)


@pytest.fixture
def configured(app):
    config = InstanceConfig(
        admin_email='admin@example.com', totp_secret='SECRET',
        llm_provider='groq', llm_api_key='test-key', business_tin='108537108',
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
def photo_receipt(app, device):
    """
    A receipt read off a photograph, with the photograph actually on disk.

    A real file because both features under test look for one: re-reading opens it, and
    deleting is supposed to take it with the row.
    """
    def _build(**overrides):
        filename = f'receipt-{os.urandom(4).hex()}.jpg'
        import main
        path = os.path.join(main.app.config['UPLOAD_FOLDER'], filename)
        with open(path, 'wb') as image:
            image.write(b'not really a jpeg, but a file that exists')

        submission = Submission(
            device_id=device.id, input_type='photo', input_data=filename,
            status='completed', user_note='Diesel for the generator',
        )
        db.session.add(submission)
        db.session.flush()

        fields = dict(
            device_id=device.id, submission_id=submission.id,
            extraction_source='llm_vision', document_type='tra_efd_receipt',
            vendor_name='EFRA1M M0TORS', vendor_tin='10347036',
            receipt_date=date(2025, 5, 28), receipt_time=time(9, 20),
            total_incl_tax_cents=76_000, llm_status='ok', category='other',
            raw_llm_response=json.dumps({'category': 'other',
                                         'llm_tax_analysis': 'Probably deductible.'}),
        )
        fields.update(overrides)
        receipt = Receipt(**fields)
        receipt.vendor = Vendor.upsert(tin=fields['vendor_tin'], name=fields['vendor_name'])
        receipt.items.append(ReceiptItem(line_number=1, description='DIESEL', amount_cents=76_000))
        db.session.add(receipt)
        db.session.commit()
        return receipt, submission, path
    return _build


@pytest.fixture
def verified_receipt(app, device, receipt_html):
    """A receipt whose numbers came from TRA's own page."""
    submission = Submission(
        device_id=device.id, input_type='url', status='completed',
        input_data='https://verify.tra.go.tz/58E41A514_092022',
    )
    db.session.add(submission)
    db.session.flush()

    receipt = Receipt(
        device_id=device.id, submission_id=submission.id, extraction_source='tra_html',
        vendor_name='PLASCO LIMITED', vendor_tin='100147181', vrn='10007206H',
        receipt_verification_code='58E41A514', customer_id_type='TIN',
        customer_id='108537108', receipt_date=date.today(), receipt_time=time(10, 30),
        total_incl_tax_cents=118_00, total_excl_tax_cents=100_00, total_tax_cents=18_00,
        source_html=receipt_html, llm_status='ok',
    )
    receipt.items.append(ReceiptItem(line_number=1, description='DIESEL AGO',
                                     amount_cents=118_00, tax_code='A'))
    receipt.tax_lines.append(ReceiptTaxLine(code='A', rate=18, amount_cents=18_00))
    db.session.add(receipt)
    db.session.commit()
    return receipt


@pytest.fixture
def vision(monkeypatch):
    """The model, stubbed, recording what it was handed."""
    import main

    calls = []

    def _stub(**fields):
        data = {
            'vendor_name': 'EFRAIM MOTORS', 'vendor_tin': '103470362',
            'receipt_date': '2025-05-28', 'receipt_time': '09:20:00',
            'total_amount': 760.00, 'document_type': 'tra_efd_receipt',
            'category': 'fuel', 'llm_extracted_description': 'Diesel.',
            'llm_tax_analysis': 'Deductible as a running cost.',
            'items': [{'description': 'DIESEL AGO', 'amount': 760.00}],
        }
        data.update(fields)

        def _extract(content, is_image, config, user_note=None, catalogue=None):
            calls.append({'content': content, 'is_image': is_image, 'note': user_note})
            return data
        monkeypatch.setattr(main, 'extract_receipt_details', _extract)
        return calls
    return _stub


# --- Deleting ---------------------------------------------------------------

def test_deleting_takes_the_submission_and_the_photograph_with_it(app, admin, photo_receipt):
    """
    A receipt and the submission behind it are one thing, so they go together.

    Leaving the submission would leave a row marked 'completed' with nothing behind it -
    a state every list in this app would then have to learn to draw - and would leave the
    content hash that stops the same photograph being submitted again, which is exactly
    what somebody who has just deleted a receipt by mistake wants to be able to do.
    """
    receipt, submission, path = photo_receipt()

    response = admin.post(f'/receipts/{receipt.id}/delete', data={'confirm': str(receipt.id)})

    assert response.status_code == 200
    assert db.session.get(Receipt, receipt.id) is None
    assert db.session.get(Submission, submission.id) is None
    assert ReceiptItem.query.count() == 0
    assert not os.path.exists(path)


def test_a_receipt_is_not_deleted_without_its_own_number_typed_back(app, admin, photo_receipt):
    """
    The second step is checked here and not only in the browser.

    A confirmation the server does not enforce is a dialog, and a dialog is something a
    tired person dismisses. Typing the number is the one thing that cannot be done by
    reflex on the wrong page.
    """
    receipt, submission, path = photo_receipt()

    for attempt in ('', 'yes', str(receipt.id + 1)):
        response = admin.post(f'/receipts/{receipt.id}/delete', data={'confirm': attempt})
        assert response.status_code == 400
        assert str(receipt.id) in response.get_json()['error']

    assert db.session.get(Receipt, receipt.id) is not None
    assert db.session.get(Submission, submission.id) is not None
    assert os.path.exists(path)


def test_deleting_one_receipt_leaves_the_supplier_and_their_other_receipts(app, admin,
                                                                          photo_receipt):
    """A vendor is a row other receipts share; only this purchase goes."""
    first, _, _ = photo_receipt()
    second, _, _ = photo_receipt()
    vendor_id = first.vendor_id

    admin.post(f'/receipts/{first.id}/delete', data={'confirm': str(first.id)})

    assert db.session.get(Receipt, second.id) is not None
    assert db.session.get(Vendor, vendor_id) is not None


def test_the_page_carries_the_two_step_confirmation(app, admin, photo_receipt):
    """The button is on the page, and it does not delete anything on its own."""
    receipt, _, _ = photo_receipt()

    body = admin.get(f'/receipts/{receipt.id}').get_data(as_text=True)

    assert f'Delete receipt #{receipt.id}' in body
    assert 'This cannot be undone' in body
    assert 'to confirm' in body
    assert f'/receipts/{receipt.id}/delete' in body


def test_deleting_something_that_is_not_there_is_a_404(admin):
    assert admin.post('/receipts/999999/delete', data={'confirm': '999999'}).status_code == 404


def test_deleting_requires_a_login(app, photo_receipt):
    receipt, _, _ = photo_receipt()

    response = app.test_client().post(f'/receipts/{receipt.id}/delete',
                                      data={'confirm': str(receipt.id)})

    assert response.status_code == 302
    assert db.session.get(Receipt, receipt.id) is not None


# --- Reading the document again ---------------------------------------------

def test_re_reading_a_photograph_replaces_the_figures_it_had(app, admin, photo_receipt, vision):
    """
    The repair for a reading that was wrong rather than a field that was.

    A misread TIN files the receipt under a supplier of its own and a misread total is
    simply the wrong number in every report. Both are one bad reading of one photograph,
    and reading it again fixes the lot in one press - including the supplier it is filed
    under, because the new reading goes through the same builder the pipeline uses.
    """
    receipt, submission, path = photo_receipt()
    calls = vision()

    response = admin.post(f'/receipts/{receipt.id}/reread')

    assert response.status_code == 200
    fresh_id = response.get_json()['receipt_id']
    fresh = db.session.get(Receipt, fresh_id)

    assert fresh.vendor_tin == '103470362'
    assert fresh.vendor_name == 'EFRAIM MOTORS'
    assert fresh.total_incl_tax_cents == 76_000
    assert fresh.extraction_source == 'llm_vision'
    assert fresh.submission_id == submission.id
    # The photograph itself was what the model was handed, along with the sender's note.
    assert calls[0]['content'] == path and calls[0]['is_image'] is True
    assert calls[0]['note'] == 'Diesel for the generator'
    # One receipt on this submission, not two.
    assert Receipt.query.count() == 1


def test_re_reading_keeps_a_category_somebody_set_by_hand(app, admin, photo_receipt, vision):
    """
    The same exception re-analysis makes, for the same reason.

    'Diesel for the generator, not the van' is not on the paper and never will be, so a
    fresh reading of the paper is not a reason to replace it with a guess.
    """
    from datetime import datetime

    receipt, _, _ = photo_receipt(category='utilities')
    receipt.category_corrected_at = datetime.utcnow()
    db.session.commit()
    vision(category='fuel')

    payload = admin.post(f'/receipts/{receipt.id}/reread').get_json()

    fresh = db.session.get(Receipt, payload['receipt_id'])
    assert payload['category_kept'] is True
    assert fresh.category == 'utilities'
    assert fresh.category_corrected_at is not None


def test_a_verified_receipt_is_never_re_read(app, admin, verified_receipt, vision):
    """
    TRA's own record of the sale is not replaced by a model's reading of the paper.

    The same refusal correcting one by hand gets, and for the same reason: a verified
    receipt that looks wrong is a parser problem, not a transcription problem.
    """
    calls = vision()

    response = admin.post(f'/receipts/{verified_receipt.id}/reread')

    assert response.status_code == 409
    assert 'verified page' in response.get_json()['error']
    assert calls == []
    assert db.session.get(Receipt, verified_receipt.id).vendor_tin == '100147181'


def test_a_model_that_cannot_read_it_leaves_the_receipt_exactly_as_it_was(app, admin,
                                                                         photo_receipt,
                                                                         monkeypatch):
    """Nothing is deleted until there is something to put in its place."""
    import main

    receipt, _, _ = photo_receipt()

    def _fail(*args, **kwargs):
        raise RuntimeError('provider is down')
    monkeypatch.setattr(main, 'extract_receipt_details', _fail)

    response = admin.post(f'/receipts/{receipt.id}/reread')

    assert response.status_code == 503
    assert 'provider is down' in response.get_json()['error']
    kept = db.session.get(Receipt, receipt.id)
    assert kept is not None and kept.vendor_tin == '10347036'


def test_a_re_read_that_lands_on_another_receipt_deletes_nothing(app, admin, photo_receipt,
                                                                 vision):
    """
    Read again, this document turns out to be one we already hold.

    The pipeline's answer to that is to file the submission as a duplicate and store
    nothing - which here would mean destroying the row an admin is looking at in order to
    record it as a copy of somebody else's. So it is reported instead, and deleting is
    left to the admin who now knows there are two.
    """
    held, _, _ = photo_receipt(receipt_verification_code='DUPLICATE1')
    receipt, _, _ = photo_receipt()
    vision(receipt_verification_code='DUPLICATE1')

    response = admin.post(f'/receipts/{receipt.id}/reread')

    assert response.status_code == 409
    assert response.get_json()['receipt_id'] == held.id
    assert db.session.get(Receipt, receipt.id) is not None
    assert Receipt.query.count() == 2


def test_a_pasted_record_is_re_read_from_the_text_it_came_from(app, admin, device, vision):
    """The SMS is still on the submission, so the text model reads it rather than a photo."""
    submission = Submission(
        device_id=device.id, input_type='text', status='completed',
        input_data='LUKU 12345678901 TZS 20,000 uniti 55.5',
    )
    db.session.add(submission)
    db.session.flush()
    receipt = Receipt(
        device_id=device.id, submission_id=submission.id, extraction_source='llm_text',
        document_type='other_receipt', vendor_name='TANESCO', total_incl_tax_cents=20_000,
        llm_status='ok',
    )
    db.session.add(receipt)
    db.session.commit()

    calls = vision(vendor_name='TANESCO', document_type='other_receipt',
                   total_amount=200.00, items=[])

    payload = admin.post(f'/receipts/{receipt.id}/reread').get_json()

    assert calls[0]['is_image'] is False
    assert calls[0]['content'].startswith('LUKU')
    fresh = db.session.get(Receipt, payload['receipt_id'])
    assert fresh.extraction_source == 'llm_text'
    assert fresh.document_type == 'other_receipt'


def test_a_receipt_with_no_document_left_cannot_be_re_read(app, admin, device, vision):
    """A photograph that is no longer on the volume is not something to offer."""
    submission = Submission(device_id=device.id, input_type='photo', status='completed',
                            input_data='long-since-deleted.jpg')
    db.session.add(submission)
    db.session.flush()
    receipt = Receipt(device_id=device.id, submission_id=submission.id,
                      extraction_source='llm_vision', vendor_name='SOMEBODY',
                      total_incl_tax_cents=1_000, llm_status='ok')
    db.session.add(receipt)
    db.session.commit()
    calls = vision()

    response = admin.post(f'/receipts/{receipt.id}/reread')
    body = admin.get(f'/receipts/{receipt.id}').get_data(as_text=True)

    assert response.status_code == 409
    assert 'nothing left to re-read' in response.get_json()['error']
    assert calls == []
    # And the page does not offer a button that would only ever be refused.
    assert 'Read it again' not in body


def test_the_page_offers_re_reading_where_there_is_something_to_read(app, admin, photo_receipt):
    receipt, _, _ = photo_receipt()

    body = admin.get(f'/receipts/{receipt.id}').get_data(as_text=True)

    assert 'Read it again' in body
    assert f'/receipts/{receipt.id}/reread' in body
    # And says what it will cost before it is pressed.
    assert 'Every figure on this panel is replaced' in body


def test_a_verified_receipt_is_not_offered_a_re_read(app, admin, verified_receipt):
    body = admin.get(f'/receipts/{verified_receipt.id}').get_data(as_text=True)

    assert 'Read it again' not in body
    # The judgment can still be redone; only the figures are the portal's.
    assert 'Re-analyse' in body


# --- How much of the page to show -------------------------------------------

def _levels_body(admin, receipt_id, level, config):
    config.receipt_detail_level = level
    db.session.commit()
    return admin.get(f'/receipts/{receipt_id}').get_data(as_text=True)


def test_the_default_shows_the_checks_that_passed(app, admin, configured, photo_receipt):
    """Standard is what every instance has been reading, and it is the default."""
    receipt, _, _ = photo_receipt()

    assert configured.receipt_detail() == 'standard'
    body = admin.get(f'/receipts/{receipt.id}').get_data(as_text=True)

    assert 'Receipt status' in body                     # a check that passed
    assert 'do not apply to this receipt' in body       # the na group, folded under one line


def test_compact_folds_corroboration_away_and_keeps_what_costs_money(app, admin, configured,
                                                                     photo_receipt):
    """
    What compact drops is corroboration, never a finding.

    The failed checks, the money and every control stay exactly where they were; what
    goes is the second copy of the photograph, where it was collected, and the checks
    that passed - all of which are one press or one level away.
    """
    receipt, _, _ = photo_receipt(tax_office='ILALA TAX OFFICE')
    receipt.submission.location = '-6.7924,39.2083 - Kariakoo'
    db.session.commit()

    compact = _levels_body(admin, receipt.id, 'compact', configured)
    standard = _levels_body(admin, receipt.id, 'standard', configured)

    # The reason this receipt cannot be claimed is on both.
    assert 'Supplier TIN' in compact and 'Supplier TIN' in standard
    # The corroboration is not.
    assert '(approximate)' in standard and '(approximate)' not in compact
    assert 'that passed or do not apply' in compact
    # A field left off is named rather than silently missing, with the level that has it.
    assert 'ILALA TAX OFFICE' in standard and 'ILALA TAX OFFICE' not in compact
    assert 'Tax office is on this receipt and shown at Standard detail' in compact
    # And nothing an admin can do to this receipt went with it.
    for ability in ('Correct by hand', 'Read it again', 'Re-analyse',
                    f'Delete receipt #{receipt.id}', 'startCategorising()',
                    'data-peek="vendor:'):
        assert ability in compact, ability


def test_everything_shows_where_each_figure_came_from(app, admin, configured, photo_receipt):
    """
    The deepest level answers the question that arrives eighteen months later.

    All of it was already stored; it was simply never shown, so answering 'where did this
    number come from, and is this the same document as that one' meant opening the
    database.
    """
    receipt, _, _ = photo_receipt(receipt_number='RN-9001')

    body = _levels_body(admin, receipt.id, 'full', configured)

    assert 'Provenance' in body
    assert 'the vision model reading the photograph' in body
    # The two keys a duplicate is caught on, which is the only way to answer 'why was
    # this not flagged as a copy of that' from outside the database.
    assert 'Identity key' in body and 'Near key' in body
    assert 'what the model actually replied' in body
    # Nothing is folded here: the checks that do not apply are open.
    assert 'do not apply to this receipt' not in body


def test_the_level_is_saved_from_the_business_tab(app, admin, configured):
    response = admin.post('/admin/configure', data={
        'active_tab': 'business', 'business_tin': '108537108',
        'receipt_detail_level': 'full',
    })

    assert response.status_code == 302
    assert db.session.get(InstanceConfig, configured.id).receipt_detail() == 'full'


def test_the_level_can_be_changed_from_the_receipt_itself(app, admin, configured,
                                                          photo_receipt):
    """
    The same setting, offered where the wish for it occurs.

    Somebody notices the page is telling them too much while looking at one, not while
    reading the settings page - and a switch flicked here is the instance's setting from
    then on, which is the only behaviour that does not surprise somebody who set it in
    Settings yesterday.
    """
    receipt, _, _ = photo_receipt()

    response = admin.post('/settings/receipt-detail', data={
        'level': 'compact', 'next': f'/receipts/{receipt.id}',
    })

    assert response.status_code == 302
    assert response.headers['Location'].endswith(f'/receipts/{receipt.id}')
    assert configured.receipt_detail() == 'compact'


def test_an_unknown_level_changes_nothing(app, admin, configured):
    configured.receipt_detail_level = 'full'
    db.session.commit()

    admin.post('/settings/receipt-detail', data={'level': 'forensic'})

    assert configured.receipt_detail() == 'full'


def test_the_switcher_cannot_be_used_to_send_somebody_elsewhere(app, admin, configured):
    """`next` is a path on this instance or it is ignored - never an absolute URL."""
    response = admin.post('/settings/receipt-detail', data={
        'level': 'compact', 'next': 'https://example.com/phish',
    })

    assert 'example.com' not in response.headers['Location']
