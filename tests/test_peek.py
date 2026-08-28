# tests/test_peek.py
"""
The cards behind every linked value, and the pages they lead to.

Two failures are worth guarding against here and they are not the same one.

The first is a card that renders but says nothing true: a vendor total that quietly
includes the cancelled receipts, or a "not made out to your TIN" note that appears
when the receipt is in fact made out to it. Those are wrong answers delivered
confidently, which is worse than no card at all.

The second is a link that goes nowhere. The whole design rests on every peekable
value being a real anchor to a real page, so the addresses those anchors point at have
to exist - including for a supplier who never got a Vendor row of their own.
"""
import json
import shutil
import subprocess
from datetime import date, datetime, time, timedelta

import pytest

from models.user import db, Device, Receipt, ReceiptItem, ReceiptTaxLine, Submission, Vendor


@pytest.fixture
def client(app, config):
    """A logged-in browser, on an instance that knows its own TIN."""
    config.business_tin = '108537108'
    db.session.commit()

    client = app.test_client()
    with client.session_transaction() as session:
        session['admin_logged_in'] = True
    return client


def store(device, vendor='PLASCO LIMITED', tin='100147181', total=118_00, tax=18_00,
          when=None, item='DIESEL AGO', quantity=None, category='fuel', **overrides):
    """A completed submission and the receipt behind it."""
    submission = Submission(
        device_id=device.id, input_type='url', input_data='https://verify.tra.go.tz/X_000000',
        status='completed',
    )
    db.session.add(submission)
    db.session.flush()

    fields = dict(
        vendor_name=vendor, vendor_tin=tin, vrn='10007206H', tax_office='MEDIUM TAXPAYERS DIVISION',
        efd_serial='10TZ144450', receipt_number='60344', z_number='20251008',
        receipt_verification_code=f'CODE{submission.id}', extraction_source='tra_html',
        customer_id_type='TIN', customer_id='108537108', category=category,
        receipt_date=when or date.today(), receipt_time=time(10, 30),
        total_incl_tax_cents=total, total_excl_tax_cents=total - tax, total_tax_cents=tax,
        device_id=device.id, submission_id=submission.id,
    )
    fields.update(overrides)
    receipt = Receipt(**fields)
    receipt.items.append(ReceiptItem(line_number=1, description=item, amount_cents=total,
                                     quantity=quantity, tax_code='A'))
    receipt.tax_lines.append(ReceiptTaxLine(code='A', rate=18, amount_cents=tax))
    db.session.add(receipt)
    db.session.commit()
    return receipt


def with_vendor(receipt, name=None, tin=None):
    """Attaches the Vendor row the processing pipeline would have created."""
    vendor = Vendor.upsert(tin=tin or receipt.vendor_tin, name=name or receipt.vendor_name,
                           vrn=receipt.vrn, tax_office=receipt.tax_office)
    db.session.flush()
    receipt.vendor_id = vendor.id
    db.session.commit()
    return vendor


def card(client, kind, key):
    response = client.get(f'/api/peek/{kind}/{key}')
    assert response.status_code == 200, response.get_data(as_text=True)
    return response.get_json()


def text_of(payload):
    """Every string on a card, so a test can assert on what it says without its layout."""
    # ensure_ascii off: the cards are full of '·' and '→', and escaping them here would
    # mean asserting on · rather than on what the reader sees.
    return json.dumps(payload, ensure_ascii=False)


# --- The endpoint itself ----------------------------------------------------

def test_a_peek_needs_a_login(app, device):
    """The cards carry spending figures, so they are behind the same door as the rest."""
    response = app.test_client().get('/api/peek/vendor/tin:100147181')

    assert response.status_code == 302
    assert '/admin/login' in response.headers['Location']


def test_an_unknown_kind_is_a_404_rather_than_an_empty_card(client, device):
    store(device)
    assert client.get('/api/peek/nonsense/whatever').status_code == 404


def test_a_key_with_nothing_behind_it_is_a_404(client, device):
    """
    A miss has to be visibly a miss.

    The key came out of our own markup, so nothing behind it means the receipt is gone
    or the link is stale. A card reading 'Unnamed vendor, 0 receipts' would hide that.
    """
    store(device)
    assert client.get('/api/peek/vendor/tin:999999999').status_code == 404
    assert client.get('/api/peek/compliance/98765').status_code == 404
    assert client.get('/api/peek/code/NOTACODE').status_code == 404


# --- Vendor -----------------------------------------------------------------

def test_the_vendor_card_totals_what_was_actually_spent(client, device):
    """
    Three receipts from one supplier add up, at every level of the card.

    Only the last of them is linked to the Vendor row here, which is the half-migrated
    state a backfill can leave behind. The two carrying the same printed TIN belong to
    the same supplier and have to be counted with it - dropping them would take money
    off a supplier's total without saying so.
    """
    for amount in (100_00, 200_00, 300_00):
        receipt = store(device, total=amount, tax=0)
    with_vendor(receipt)

    payload = card(client, 'vendor', 'tin:100147181')

    assert payload['title'] == 'PLASCO LIMITED'
    stats = {stat['label']: stat['value'] for stat in payload['stats']}
    assert stats['Total spend'] == '600.00'
    assert stats['Receipts'] == '3'
    assert stats['Average'] == '200.00'
    assert '3 receipt(s)' in payload['evidence']


def test_the_vendor_card_leaves_out_cancelled_and_test_receipts(client, device):
    """
    A cancelled receipt is not money and a test receipt never was.

    Both are stored, so the submission has a visible outcome; neither may reach a
    spending total, here or anywhere else.
    """
    store(device, total=100_00)
    store(device, total=500_00, is_cancelled=True)
    store(device, total=900_00, is_test=True)

    stats = {stat['label']: stat['value'] for stat in card(client, 'vendor', 'tin:100147181')['stats']}

    assert stats['Total spend'] == '100.00'
    assert stats['Receipts'] == '1'


def test_the_vendor_card_names_the_habit_that_costs_money(client, device):
    """
    One receipt made out to a walk-in customer is an annoyance. Most of them is a
    pattern, and the card has to say so - that is the entire reason it exists.
    """
    for _ in range(3):
        store(device, customer_id=None, customer_id_type=None)

    payload = card(client, 'vendor', 'tin:100147181')

    assert 'not made out to your TIN' in text_of(payload)
    assert any(note['tone'] in ('bad', 'warn') for note in payload['notes'])


def test_the_vendor_card_says_when_there_is_nothing_to_compare_against(client, device):
    """A single receipt supports no judgment about a supplier, and must not imply one."""
    store(device)

    payload = card(client, 'vendor', 'tin:100147181')

    assert 'First and only receipt' in text_of(payload)


def test_a_vendor_resolves_without_a_vendor_row(client, device):
    """
    A photographed receipt may never have produced a Vendor row.

    Its printed TIN still identifies the supplier, and the card - and the profile page
    it links to - must work from that alone.
    """
    store(device)

    payload = card(client, 'vendor', 'tin:100147181')

    assert payload['title'] == 'PLASCO LIMITED'
    assert payload['href'] == '/vendors/tin:100147181'
    assert client.get(payload['href']).status_code == 200


def test_an_unregistered_supplier_charging_vat_is_called_out(client, device):
    """Tax charged by a supplier with no VRN is not input tax, whatever the receipt says."""
    store(device, vrn=None, tax=18_00)

    payload = card(client, 'vendor', 'tin:100147181')

    assert 'cannot be claimed' in text_of(payload)
    assert {'label': 'no VRN', 'tone': 'warn'} in payload['badges']


# --- The receipt's own verdict ----------------------------------------------

def test_the_compliance_card_carries_the_wording_the_table_has_no_room_for(client, device):
    """
    The table shows a score and two words. The card has to show the sentence, or it is
    just the same summary again in a smaller box.
    """
    receipt = store(device, customer_id='999999999')

    payload = card(client, 'compliance', str(receipt.id))

    labels = {check['label']: check for check in payload['checks']}
    assert labels['Issued to you']['status'] == 'fail'
    assert 'not to yours (108537108)' in labels['Issued to you']['detail']
    assert payload['href'] == f'/receipts/{receipt.id}'


def test_the_vat_card_separates_charged_from_recoverable(client, device):
    """The two figures differ for a reason, and the reason is the point of the card."""
    receipt = store(device, customer_id=None, customer_id_type=None, tax=18_00)

    payload = card(client, 'vat', str(receipt.id))

    stats = {stat['label']: stat['value'] for stat in payload['stats']}
    assert stats['Charged'] == '18.00'
    assert stats['Recoverable'] == '0.00'
    assert payload['notes']
    assert payload['href'].startswith('/vat-ledger?period=')


def test_the_vat_card_links_to_the_period_the_receipt_belongs_on(client, device):
    receipt = store(device, when=date(2026, 3, 14))

    assert card(client, 'vat', str(receipt.id))['href'] == '/vat-ledger?period=2026-03'


def test_the_customer_card_points_at_configuration_when_the_tin_is_missing(app, device, config):
    """
    Without our own TIN the check cannot be made, and the card says so rather than
    reporting a pass it did not earn.
    """
    client = app.test_client()
    with client.session_transaction() as session:
        session['admin_logged_in'] = True
    receipt = store(device)

    payload = card(client, 'customer', str(receipt.id))

    assert payload['href'] == '/admin/configure'
    assert 'not configured' in text_of(payload)


def test_the_code_card_reports_a_possible_duplicate(client, device):
    """
    The same purchase submitted as a photo and as a link is stored twice, and only the
    shape of it - same supplier, same day, same total - gives it away.
    """
    first = store(device, total=118_00)
    second = store(device, total=118_00)
    with_vendor(first)
    with_vendor(second)

    payload = card(client, 'code', first.receipt_verification_code)

    assert 'counted twice' in text_of(payload)
    assert payload['href'] == f'/receipts/{first.id}'


def test_the_till_card_says_how_many_tills_the_supplier_has(client, device):
    """Two EFD serials against one TIN is a supplier with branches, which is worth seeing."""
    first = store(device, efd_serial='10TZ144450')
    second = store(device, efd_serial='10TZ999999')
    with_vendor(first)
    with_vendor(second)

    payload = card(client, 'till', '10TZ999999')

    assert payload['title'] == 'Till 10TZ999999'
    assert '2 separate tills' in text_of(payload)


# --- Slices of the ledger ---------------------------------------------------

def test_the_category_card_reports_its_share_of_everything(client, device):
    store(device, category='fuel', total=750_00)
    store(device, category='meals_entertainment', total=250_00)

    payload = card(client, 'category', 'fuel')

    stats = {stat['label']: stat['value'] for stat in payload['stats']}
    assert stats['Spend'] == '750.00'
    assert stats['Share'] == '75.0%'
    assert payload['href'] == '/?tab=processed&category=fuel'


def test_the_date_card_counts_the_day_and_names_the_return_it_falls_on(client, device):
    when = date(2026, 3, 14)
    store(device, when=when, total=100_00)
    store(device, when=when, total=200_00)
    store(device, when=date(2026, 3, 20), total=400_00)

    payload = card(client, 'date', '2026-03-14')

    stats = {stat['label']: stat['value'] for stat in payload['stats']}
    assert stats['Spent that day'] == '300.00'
    assert stats['Receipts'] == '2'
    # The whole month, not just the day, and the 20th of the month after it.
    assert '700.00 · 3 receipts' in text_of(payload)
    assert '20 Apr 2026' in text_of(payload)


# --- Device -----------------------------------------------------------------

def test_the_device_card_reports_what_one_phone_collected(client, device):
    """
    A device is the closest thing here to a person, so it is the unit an admin manages.

    Share is the figure that is invisible receipt by receipt: a phone quietly being
    most of the spending is a fact about how the business runs.
    """
    other = Device(name='Van two', api_key='van-key')
    db.session.add(other)
    db.session.commit()

    store(device, total=300_00, tax=0)
    store(device, total=100_00, tax=0)
    store(other, total=100_00, tax=0)

    payload = card(client, 'device', device.id)

    assert payload['title'] == 'Test device'
    stats = {stat['label']: stat['value'] for stat in payload['stats']}
    assert stats['Collected'] == '400.00'
    assert stats['Receipts'] == '2'
    assert stats['Share'] == '80.0%'
    assert payload['href'] == f'/?tab=processed&device={device.id}'


def test_the_device_card_counts_the_submissions_no_receipt_came_out_of(client, device):
    """
    A device's failures are exactly the attempts that never became a receipt, so a
    count of receipts is structurally unable to see them.
    """
    store(device)
    for _ in range(4):
        db.session.add(Submission(device_id=device.id, input_type='photo',
                                  input_data='blurred.jpg', status='failed'))
    db.session.commit()

    payload = card(client, 'device', device.id)

    assert '4 of 5 submissions from this device never verified' in text_of(payload)


def test_the_device_card_notices_one_that_has_gone_quiet(client, device):
    """Receipts not scanned are input VAT not claimed, and nobody is told."""
    receipt = store(device)
    receipt.submission.received_at = datetime.utcnow() - timedelta(days=40)
    db.session.commit()

    assert 'Nothing collected for 40 days' in text_of(card(client, 'device', device.id))


def test_a_revoked_device_says_so_rather_than_looking_idle(client, device):
    store(device)
    device.revoked_at = datetime.utcnow()
    db.session.commit()

    payload = card(client, 'device', device.id)

    assert {'label': 'revoked', 'tone': 'bad'} in payload['badges']
    assert 'its history stays here' in text_of(payload)


def test_a_device_that_does_not_exist_is_a_404(client, device):
    assert client.get('/api/peek/device/9999').status_code == 404
    assert client.get('/api/peek/device/the-red-one').status_code == 404


# --- Photograph -------------------------------------------------------------

def test_the_photo_card_carries_the_picture_itself(client, device):
    """
    Confirming figures against the paper is the commonest reason to open a submission
    at all, and it was a page load, a look and a click back for something the eye
    settles in under a second.
    """
    receipt = store(device)
    receipt.submission.photo_filename = 'receipt_20260510.jpg'
    db.session.commit()

    payload = card(client, 'photo', receipt.submission_id)

    assert payload['image'] == '/uploads/receipt_20260510.jpg'
    assert payload['href'] == '/uploads/receipt_20260510.jpg'
    assert payload['title'] == 'PLASCO LIMITED'
    assert {'label': 'Collected by', 'value': 'Test device', 'tone': 'muted'} in payload['rows']


def test_the_photo_card_resolves_a_path_left_by_an_older_volume(client, device):
    """Rows written before the persistence volume moved hold an absolute path."""
    receipt = store(device)
    receipt.submission.photo_filename = '/var/old/uploads/receipt.jpg'
    db.session.commit()

    assert card(client, 'photo', receipt.submission_id)['image'] == '/uploads/receipt.jpg'


def test_a_photo_submission_keeps_its_image_where_it_always_did(client, device):
    """A photo submission has no photo_filename - the image is its input_data."""
    submission = Submission(device_id=device.id, input_type='photo',
                            input_data='snap.jpg', status='queued')
    db.session.add(submission)
    db.session.commit()

    assert card(client, 'photo', submission.id)['image'] == '/uploads/snap.jpg'


def test_a_row_with_no_photograph_has_no_card(client, device):
    """The chip is only rendered where there is an image, so this is a stale link."""
    receipt = store(device)

    assert client.get(f'/api/peek/photo/{receipt.submission_id}').status_code == 404


def test_the_photo_card_says_when_the_figures_were_read_off_the_image(client, device):
    """
    The difference between TRA's record of a sale and a model's reading of a crumpled
    print is the whole reason to look at the paper, so the card names which one it is.
    """
    receipt = store(device, extraction_source='llm_vision')
    receipt.submission.photo_filename = 'snap.jpg'
    db.session.commit()

    payload = card(client, 'photo', receipt.submission_id)

    assert {'label': 'read from this image', 'tone': 'warn'} in payload['badges']
    assert 'not from TRA' in text_of(payload)


def test_a_verified_photo_is_badged_as_verified(client, device):
    receipt = store(device)
    receipt.submission.photo_filename = 'snap.jpg'
    db.session.commit()

    payload = card(client, 'photo', receipt.submission_id)

    assert {'label': 'verified by TRA', 'tone': 'good'} in payload['badges']
    assert 'not from TRA' not in text_of(payload)


def test_the_date_card_refuses_a_key_that_is_not_a_date(client, device):
    store(device)
    assert client.get('/api/peek/date/last-tuesday').status_code == 404


def test_the_item_card_compares_a_price_against_what_it_used_to_be(client, device):
    """
    Against the mean of every earlier purchase from the same supplier, so a single odd
    receipt does not become the baseline everything after it is judged against.
    """
    store(device, item='DIESEL AGO', quantity=10, total=1_000_00, when=date.today() - timedelta(days=30))
    store(device, item='DIESEL AGO', quantity=10, total=1_000_00, when=date.today() - timedelta(days=20))
    latest = store(device, item='diesel  (ago)', quantity=10, total=1_500_00, when=date.today())

    payload = card(client, 'item', f'{latest.id}:1')

    # 1,000.00 over ten units against 1,500.00 over ten: 100.00 a unit becoming 150.00.
    rows = {row['label']: row['value'] for row in payload['rows']}
    assert rows['Usual here'] == '100.00'
    assert '50% above' in text_of(payload)


def test_the_item_card_finds_the_same_thing_cheaper_elsewhere(client, device):
    dear = store(device, vendor='PLASCO LIMITED', tin='100147181', item='CEMENT 50KG',
                 quantity=10, total=200_00)
    store(device, vendor='OTHER SUPPLIER', tin='100999999', item='CEMENT 50KG',
          quantity=10, total=100_00)

    payload = card(client, 'item', f'{dear.id}:1')

    assert 'OTHER SUPPLIER charged 10.00 a unit, 50% less' in text_of(payload)


def test_the_item_card_links_to_the_purchase_it_compared_against(client, device):
    """
    Not to the receipt the line is printed on - this card is only ever opened from
    there, so that link leads back to the page already on screen. The useful
    destination is the evidence: the last time the same thing was bought here.
    """
    store(device, item='CEMENT 50KG', quantity=10, total=100_00, when=date.today() - timedelta(days=40))
    previous = store(device, item='CEMENT 50KG', quantity=10, total=100_00,
                     when=date.today() - timedelta(days=20))
    latest = store(device, item='CEMENT 50KG', quantity=10, total=300_00)

    payload = card(client, 'item', f'{latest.id}:1')

    # The most recent earlier purchase, not the oldest and not this one.
    assert payload['href'] == f'/receipts/{previous.id}'
    assert 'earlier purchase(s)' in payload['evidence']


def test_an_item_with_no_history_offers_no_link(client, device):
    """A footer link that goes back where you came from is worse than no footer."""
    receipt = store(device, item='ONE OFF THING', quantity=1, total=500_00)

    assert card(client, 'item', f'{receipt.id}:1')['href'] is None


def test_the_item_card_reads_what_the_purchase_implies(client, device):
    """Line item text drives withholding tax and capital treatment. Same rules as the receipt page."""
    receipt = store(device, item='CONSULTANCY SERVICES', total=1_000_00)

    payload = card(client, 'item', f'{receipt.id}:1')

    assert 'withholding tax' in text_of(payload)


def test_an_item_card_survives_a_receipt_with_no_quantity_printed(client, device):
    """
    A great many Tanzanian EFDs print 'SUMMARIZED SALE' and no quantity at all. There
    is nothing to divide by, so the price half of the card is silent rather than wrong.
    """
    receipt = store(device, item='SUMMARIZED SALE', quantity=None, total=118_00)

    payload = card(client, 'item', f'{receipt.id}:1')

    assert not any(stat['label'] == 'Unit price' for stat in payload['stats'])


# --- The vendor profile page ------------------------------------------------

def test_the_vendor_page_counts_repeated_failures(client, device):
    """
    The page exists to show what one receipt cannot: the same check failing over and
    over is how this supplier works, and the count is what makes that visible.
    """
    for _ in range(3):
        receipt = store(device, customer_id=None, customer_id_type=None)
    with_vendor(receipt)

    response = client.get('/vendors/tin:100147181')
    body = response.get_data(as_text=True)

    assert response.status_code == 200
    assert 'PLASCO LIMITED' in body
    assert 'Compliance record' in body
    assert 'failed on' in body


def test_the_vendor_page_reports_a_clean_supplier_as_clean(client, device):
    store(device)

    body = client.get('/vendors/tin:100147181').get_data(as_text=True)

    assert 'Every check passes on every receipt' in body


def test_an_unknown_vendor_page_goes_back_to_the_list(client, device):
    response = client.get('/vendors/tin:000000000')

    assert response.status_code == 302
    assert response.headers['Location'].endswith('/vendors')


def test_the_vendors_list_links_each_supplier_to_their_profile(client, device):
    receipt = store(device)
    with_vendor(receipt)

    body = client.get('/vendors').get_data(as_text=True)

    assert 'href="/vendors/tin:100147181"' in body
    assert 'data-peek="vendor:tin:100147181"' in body


# --- The filter a category chip sets ----------------------------------------

def test_clicking_a_category_narrows_the_table(client, device):
    store(device, category='fuel', vendor='FUEL CO', tin='100000001')
    store(device, category='meals_entertainment', vendor='CAFE CO', tin='100000002')

    payload = client.get('/api/submissions?tab=processed&category=fuel').get_json()

    assert payload['total'] == 1
    assert payload['submissions'][0]['receipt']['vendor_name'] == 'FUEL CO'
    assert payload['insights']['count'] == 1


def test_the_category_filter_is_carried_back_to_the_page(client, device):
    """
    A filter the table cannot see it is under is a table that lies about how much
    there is. The dashboard renders a chip from this, so it has to survive the trip.
    """
    store(device, category='fuel')

    body = client.get('/?category=fuel').get_data(as_text=True)

    assert '"category": "fuel"' in body or '"category":"fuel"' in body


# --- The markup the browser needs -------------------------------------------

def test_the_receipt_page_marks_up_every_key_it_prints(client, device):
    """
    The peekable values are the point of the page. A refactor that drops the attribute
    leaves a page that still renders and has silently lost the feature.
    """
    receipt = store(device)
    with_vendor(receipt)

    body = client.get(f'/receipts/{receipt.id}').get_data(as_text=True)

    for spec in [
        'data-peek="vendor:tin:100147181"',
        'data-peek="till:10TZ144450"',
        f'data-peek="code:{receipt.receipt_verification_code}"',
        f'data-peek="customer:{receipt.id}"',
        f'data-peek="item:{receipt.id}:1"',
        'data-peek="tax_office:MEDIUM TAXPAYERS DIVISION"',
        'data-peek="category:fuel"',
        f'data-peek="device:{device.id}"',
    ]:
        assert spec in body, spec


def test_the_dashboard_ships_the_key_its_rows_link_by(client, device):
    """
    The table is rendered in the browser, so the vendor key has to be in the payload -
    a row cannot look one up.
    """
    receipt = store(device)
    with_vendor(receipt)

    payload = client.get('/api/submissions').get_json()

    assert payload['submissions'][0]['receipt']['vendor_key'] == 'tin:100147181'


def test_the_dashboard_ships_the_device_behind_each_row(client, device):
    """
    Who collected a receipt is a fact the table had but never showed. The row needs the
    id and not only the name: the chip is a filter link and a hover card, both keyed on
    the id, and two phones can be called 'Front desk' a year apart.
    """
    store(device)

    row = client.get('/api/submissions').get_json()['submissions'][0]

    assert row['device_id'] == device.id
    assert row['device_name'] == 'Test device'


def test_the_dashboard_marks_up_its_device_and_photograph_chips(client, device):
    """
    Rendered by Alpine, so what is asserted here is the template's binding rather than
    a finished attribute - which is still the thing a refactor silently drops.
    """
    body = client.get('/').get_data(as_text=True)

    for spec in [
        'data-peek="`device:${sub.device_id}`"',
        'data-peek="`photo:${sub.id}`"',
        'data-peek-warm',
    ]:
        assert spec in body, spec


def test_the_dashboard_loads_the_peek_script(client, device):
    body = client.get('/').get_data(as_text=True)
    assert 'peek.js' in body


@pytest.mark.skipif(shutil.which('node') is None, reason='node is not installed')
def test_the_peek_script_parses():
    """
    Executed rather than merely served.

    Every other test here asserts on what the server produced. A syntax error in the
    client would leave all of that byte-perfect and every card dead, which is exactly
    the failure this file cannot otherwise see.
    """
    result = subprocess.run(
        ['node', '--check', 'static/peek.js'], capture_output=True, text=True,
    )
    assert result.returncode == 0, result.stderr
