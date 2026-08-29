# tests/test_dashboard_routes.py
"""
The pages and endpoints the dashboard is made of.

Rendered through the real templates against a real database, because the failure
these guard against is not a wrong number - it is a page that does not render at all,
or a table that quietly loses rows once there are more of them than fit on a page.
"""
from datetime import date, time, timedelta

import pytest

from models.user import db, Receipt, ReceiptItem, ReceiptTaxLine, Submission


@pytest.fixture
def client(app, config):
    """
    A logged-in browser, on an instance that knows its own TIN.

    Without the TIN the buyer-match check reports itself as unconfigured instead of
    judging the receipt, so an instance that has one is the case worth covering here.
    """
    config.business_tin = '108537108'
    db.session.commit()

    client = app.test_client()
    with client.session_transaction() as session:
        session['admin_logged_in'] = True
    return client


def store(device, status='completed', vendor='PLASCO LIMITED', tin='100147181',
          total=118_00, tax=18_00, when=None, description=None, error=None,
          location=None, **overrides):
    """A submission and, unless it failed, the receipt behind it."""
    submission = Submission(
        device_id=device.id, input_type='url', input_data='https://verify.tra.go.tz/X_000000',
        status=status, description=description, error_message=error, location=location,
    )
    db.session.add(submission)
    db.session.flush()

    if status in ('failed', 'queued', 'processing'):
        db.session.commit()
        return submission, None

    fields = dict(
        vendor_name=vendor, vendor_tin=tin, vrn='10007206H',
        receipt_verification_code=f'CODE{submission.id}', extraction_source='tra_html',
        customer_id_type='TIN', customer_id='108537108',
        receipt_date=when or date.today(), receipt_time=time(10, 30),
        total_incl_tax_cents=total, total_excl_tax_cents=total - tax, total_tax_cents=tax,
        device_id=device.id, submission_id=submission.id,
    )
    fields.update(overrides)
    receipt = Receipt(**fields)
    receipt.items.append(ReceiptItem(line_number=1, description='DIESEL AGO',
                                     amount_cents=total, tax_code='A'))
    receipt.tax_lines.append(ReceiptTaxLine(code='A', rate=18, amount_cents=tax))
    db.session.add(receipt)
    db.session.commit()
    return submission, receipt


# --- The dashboard ----------------------------------------------------------

def test_dashboard_renders_only_the_first_page(client, device):
    """
    The whole table used to be serialised into the document on every load.

    With 60 receipts and a page size of 50, the shell must carry 50 - not 60, and not
    a promise to fetch them all at once.
    """
    for index in range(60):
        store(device, vendor=f'VENDOR {index:02d}')

    response = client.get('/')

    assert response.status_code == 200
    body = response.get_data(as_text=True)
    assert body.count('"receipt_id"') == 50


def test_dashboard_survives_an_empty_instance(client, device):
    """A fresh install has no receipts, and must still render its own shell."""
    response = client.get('/')
    assert response.status_code == 200
    assert b'Receipts Dashboard' in response.data


# --- The API ----------------------------------------------------------------

def test_api_pages_through_the_whole_table_without_losing_a_row(client, device):
    for index in range(25):
        store(device, total=(index + 1) * 100)

    seen = set()
    for page in (1, 2, 3):
        payload = client.get(f'/api/submissions?page={page}&per_page=10').get_json()
        seen.update(row['id'] for row in payload['submissions'])

    assert len(seen) == 25
    assert payload['total'] == 25 and payload['pages'] == 3


def test_api_sorting_is_stable_across_pages(client, device):
    """
    Rows with equal sort values must not swap between pages.

    Ten receipts sharing one date, paged two at a time: without a tie-break on the
    primary key, the same row can be served on two pages and another on none.
    """
    for _ in range(10):
        store(device, when=date(2026, 3, 1))

    seen = []
    for page in (1, 2, 3, 4, 5):
        payload = client.get(f'/api/submissions?sort=receipt_date&page={page}&per_page=2').get_json()
        seen.extend(row['id'] for row in payload['submissions'])

    assert len(seen) == len(set(seen)) == 10


def test_api_search_matches_vendor_tin_and_code(client, device):
    store(device, vendor='PLASCO LIMITED', tin='100147181')
    store(device, vendor='NMB BANK', tin='100200300')

    by_name = client.get('/api/submissions?search=plasco').get_json()
    by_tin = client.get('/api/submissions?search=100200300').get_json()

    assert [row['receipt']['vendor_name'] for row in by_name['submissions']] == ['PLASCO LIMITED']
    assert [row['receipt']['vendor_name'] for row in by_tin['submissions']] == ['NMB BANK']


def test_api_date_filter_reads_the_receipt_date(client, device):
    store(device, when=date(2026, 1, 15))
    store(device, when=date(2026, 6, 15))

    payload = client.get('/api/submissions?start_date=2026-06-01&end_date=2026-06-30').get_json()

    assert payload['total'] == 1
    assert payload['submissions'][0]['receipt']['receipt_date'] == '2026-06-15'


def test_api_ignores_an_unparseable_date_instead_of_failing(client, device):
    """A hand-edited URL must not take the dashboard down."""
    store(device)
    assert client.get('/api/submissions?start_date=not-a-date').status_code == 200


def test_api_rejects_an_unknown_sort_column(client, device):
    """Sort keys are looked up, never interpolated."""
    store(device)
    payload = client.get('/api/submissions?sort=vendor_name);DROP TABLE receipt;--').get_json()
    assert payload['filters']['sort'] == 'received_at'
    assert Receipt.query.count() == 1


def test_api_caps_the_page_size(client, device):
    store(device)
    payload = client.get('/api/submissions?per_page=100000').get_json()
    assert payload['per_page'] <= 200


def test_insights_cover_every_match_not_just_the_page(client, device):
    """
    The totals panel must not change when you turn the page.

    Twelve receipts of 100.00, ten to a page: the total is 1,200.00 on both pages.
    """
    for _ in range(12):
        store(device, total=100_00, tax=0)

    first = client.get('/api/submissions?page=1&per_page=10').get_json()
    second = client.get('/api/submissions?page=2&per_page=10').get_json()

    assert first['insights']['total_cents'] == second['insights']['total_cents'] == 1_200_00
    assert first['insights']['count'] == 12


def test_insights_exclude_cancelled_and_test_receipts(client, device):
    store(device, total=500_00)
    store(device, total=900_00, is_cancelled=True)
    store(device, total=700_00, is_test=True)

    insights = client.get('/api/submissions').get_json()['insights']

    assert insights['count'] == 1
    assert insights['total_cents'] == 500_00


def test_tab_counts_are_reported_for_every_tab(client, device):
    store(device, status='completed')
    store(device, status='failed', error='TraReceiptNotUploaded: not there yet')
    store(device, status='queued')
    store(device, status='duplicate')

    counts = client.get('/api/submissions').get_json()['tab_counts']

    assert counts['processed'] == 1
    assert counts['failed'] == 1
    assert counts['queued'] == 1
    assert counts['duplicates'] == 1
    assert counts['all'] == 4


def test_tab_counts_respect_the_active_search(client, device):
    """The counts describe the filtered table, not the whole database."""
    store(device, vendor='PLASCO LIMITED')
    store(device, vendor='NMB BANK')

    counts = client.get('/api/submissions?search=plasco').get_json()['tab_counts']

    assert counts['processed'] == 1


# --- Filtering by more than one thing at a time -----------------------------
#
# The table used to hold one category, one search and a date range. That answers 'what
# is fuel', but not 'what did the two vans spend on fuel and repairs last quarter' -
# which is the question an admin actually has, and which used to be six separate looks
# and a total they had to add up in their head. These cover the filter holding several
# values at once, the pickers still offering the rest, and the export meaning exactly
# what the screen it was started from means.


@pytest.fixture
def second_device(app):
    """A second phone, so 'which device' is a question with more than one answer."""
    from models.user import Device

    other = Device(name='Van two', api_key='van-key')
    db.session.add(other)
    db.session.commit()
    return other


def with_vendor(receipt, name=None, tin=None):
    """Attaches the Vendor row the processing pipeline would have created."""
    from models.user import Vendor

    vendor = Vendor.upsert(tin=tin or receipt.vendor_tin, name=name or receipt.vendor_name)
    db.session.flush()
    receipt.vendor_id = vendor.id
    db.session.commit()
    return vendor


def ids_in(payload):
    return {row['id'] for row in payload['submissions']}


def test_the_table_holds_more_than_one_category_at_a_time(client, device):
    fuel, _ = store(device, category='fuel')
    meals, _ = store(device, category='meals')
    store(device, category='rent')

    payload = client.get('/api/submissions?category=fuel&category=meals').get_json()

    assert ids_in(payload) == {fuel.id, meals.id}


def test_a_comma_separated_selection_means_the_same_as_a_repeated_one(client, device):
    """Both forms exist in the wild: the page emits one, a person types the other."""
    fuel, _ = store(device, category='fuel')
    meals, _ = store(device, category='meals')
    store(device, category='rent')

    payload = client.get('/api/submissions?category=fuel,meals').get_json()

    assert ids_in(payload) == {fuel.id, meals.id}


def test_a_single_category_link_still_means_what_it_did(client, device):
    """Every link ever shared, and every card's footer, names exactly one category."""
    fuel, _ = store(device, category='fuel')
    store(device, category='meals')

    payload = client.get('/api/submissions?category=fuel').get_json()

    assert ids_in(payload) == {fuel.id}
    assert payload['filters']['category'] == ['fuel']


def test_the_table_narrows_to_the_devices_selected(client, device, second_device):
    mine, _ = store(device)
    theirs, _ = store(second_device)

    payload = client.get(f'/api/submissions?device={second_device.id}').get_json()

    assert ids_in(payload) == {theirs.id}
    assert mine.id not in ids_in(payload)


def test_selecting_a_supplier_matches_every_spelling_of_their_name(client, device):
    """
    The vendor filter is keyed on the TIN TRA issued, not on the printed name.

    'PLASCO LIMITED' and 'Plasco Ltd' are one supplier, and a filter that split them
    would report half of what was spent with them - silently.
    """
    first, one = store(device, vendor='PLASCO LIMITED', tin='100147181')
    second, two = store(device, vendor='Plasco Ltd', tin='100147181')
    store(device, vendor='NMB BANK', tin='109876543')
    with_vendor(one)
    with_vendor(two)

    payload = client.get('/api/submissions?vendor=tin:100147181').get_json()

    assert ids_in(payload) == {first.id, second.id}


def test_a_supplier_with_no_vendor_row_still_answers_to_their_tin(client, device):
    """A receipt read off a photograph can carry a TIN and never have been linked."""
    unlinked, _ = store(device, tin='100147181')

    payload = client.get('/api/submissions?vendor=tin:100147181').get_json()

    assert ids_in(payload) == {unlinked.id}


def test_an_unreadable_device_id_is_ignored_rather_than_fatal(client, device):
    """A mangled link shows a table, exactly as an unparseable date already does."""
    kept, _ = store(device)

    payload = client.get('/api/submissions?device=not-a-number').get_json()

    assert ids_in(payload) == {kept.id}
    assert payload['filters']['device'] == []


def test_the_selections_combine_rather_than_replace_one_another(client, device, second_device):
    """Every filter narrows what the ones before it left. That is the whole point."""
    wanted, _ = store(device, category='fuel', when=date(2026, 5, 10))
    store(device, category='meals', when=date(2026, 5, 10))
    store(second_device, category='fuel', when=date(2026, 5, 10))
    store(device, category='fuel', when=date(2025, 1, 1))

    payload = client.get(
        f'/api/submissions?category=fuel&category=rent&device={device.id}'
        '&start_date=2026-01-01&end_date=2026-12-31'
    ).get_json()

    assert ids_in(payload) == {wanted.id}


# --- What the selection adds up to ------------------------------------------

def test_insights_break_the_selection_down_by_category_and_device(client, device, second_device):
    store(device, category='fuel', total=300_00, tax=0)
    store(device, category='meals', total=100_00, tax=0)
    store(second_device, category='fuel', total=100_00, tax=0)

    insights = client.get('/api/submissions').get_json()['insights']

    by_category = {row['key']: row for row in insights['by_category']}
    assert by_category['fuel']['cents'] == 400_00
    assert by_category['fuel']['share_pct'] == 80.0
    assert by_category['meals']['count'] == 1

    by_device = {row['label']: row['cents'] for row in insights['by_device']}
    assert by_device['Test device'] == 400_00
    assert by_device['Van two'] == 100_00


def test_insights_report_the_span_the_figures_actually_cover(client, device):
    """
    The dates asked for are not the dates that answered.

    A total read without knowing the stretch of calendar behind it is a number the
    reader has to trust rather than judge, so the panel is told what it is summing.
    """
    store(device, when=date(2026, 2, 3))
    store(device, when=date(2026, 6, 20))

    insights = client.get('/api/submissions?start_date=2026-01-01&end_date=2026-12-31').get_json()['insights']

    assert insights['first_date'] == '2026-02-03'
    assert insights['last_date'] == '2026-06-20'


def test_insights_average_and_largest_describe_the_same_rows_as_the_total(client, device):
    store(device, total=100_00, tax=0)
    store(device, total=300_00, tax=0)

    insights = client.get('/api/submissions').get_json()['insights']

    assert insights['total_cents'] == 400_00
    assert insights['average_cents'] == 200_00
    assert insights['largest_cents'] == 300_00


def test_an_uncategorised_receipt_is_named_rather_than_dropped(client, device):
    """A blank slice that silently vanishes makes the bars stop adding up to the total."""
    store(device, category=None, total=500_00, tax=0)

    insights = client.get('/api/submissions').get_json()['insights']

    assert [row['label'] for row in insights['by_category']] == ['uncategorised']
    assert insights['by_category'][0]['cents'] == 500_00


# --- The pickers ------------------------------------------------------------

def test_the_pickers_keep_their_other_options_while_one_is_selected(client, device):
    """
    Counted under the current selection, choosing 'fuel' would empty the category list
    of every other category and there would be no way to add 'meals' to it. The whole
    multi-select would be a series of single choices wearing its clothes.
    """
    store(device, category='fuel')
    store(device, category='meals')

    facets = client.get('/api/submissions?category=fuel').get_json()['facets']

    assert {option['key'] for option in facets['categories']} == {'fuel', 'meals'}


def test_the_pickers_are_still_narrowed_by_the_dates_and_the_search(client, device):
    """What the pickers do not narrow is themselves. Everything else still applies."""
    store(device, category='fuel', when=date(2026, 5, 10))
    store(device, category='meals', when=date(2020, 1, 1))

    facets = client.get('/api/submissions?start_date=2026-01-01').get_json()['facets']

    assert {option['key'] for option in facets['categories']} == {'fuel'}


def test_every_device_is_offered_even_with_nothing_in_range(client, device, second_device):
    """
    'What did that phone collect?' is a question whose answer can be none.

    A picker that quietly omits a device cannot say that; it just looks as though the
    device never existed.
    """
    store(device)

    devices = {option['label']: option['count']
               for option in client.get('/api/submissions').get_json()['facets']['devices']}

    assert devices == {'Test device': 1, 'Van two': 0}


def test_the_device_picker_says_whether_each_one_can_still_submit(client, device):
    facets = client.get('/api/submissions').get_json()['facets']
    assert facets['devices'][0]['status'] == device.status


def test_a_supplier_selected_out_of_range_stays_listed(client, device):
    """
    Otherwise the only way to remove a filter is to guess that it is still applied.

    Narrow the dates past a supplier's last receipt and their chip would vanish while
    still filtering the table down to nothing.
    """
    _, receipt = store(device, when=date(2020, 1, 1))
    with_vendor(receipt)

    facets = client.get(
        '/api/submissions?vendor=tin:100147181&start_date=2026-01-01'
    ).get_json()['facets']

    listed = {option['key']: option for option in facets['vendors']}
    assert listed['tin:100147181']['label'] == 'PLASCO LIMITED'
    assert listed['tin:100147181']['count'] == 0


def test_the_pickers_survive_a_tab_with_no_receipts_behind_it(client, device):
    """Failed submissions have no receipt, but they were still collected by a device."""
    store(device, status='failed', error='TraReceiptNotUploaded: not there yet')

    facets = client.get('/api/submissions?tab=failed').get_json()['facets']

    assert facets['categories'] == []
    assert [option['count'] for option in facets['devices'] if option['label'] == 'Test device'] == [1]


def test_every_row_carries_its_assessment(client, device):
    store(device)
    row = client.get('/api/submissions').get_json()['submissions'][0]

    assessment = row['receipt']['assessment']
    assert assessment['score'] == 100
    assert assessment['recoverable_vat_cents'] == 18_00


# --- The receipt page -------------------------------------------------------

def test_receipt_page_renders_the_receipt_and_its_checks(client, device):
    _, receipt = store(device)

    response = client.get(f'/receipts/{receipt.id}')
    body = response.get_data(as_text=True)

    assert response.status_code == 200
    assert 'PLASCO LIMITED' in body
    assert 'Compliance 100/100' in body
    assert 'Made out to your TIN' in body        # the buyer-TIN check, in words
    assert 'DIESEL AGO' in body                  # the line items


def test_receipt_page_shows_why_a_claim_is_blocked(client, device):
    _, receipt = store(device, customer_id='100999888')

    body = client.get(f'/receipts/{receipt.id}').get_data(as_text=True)

    assert 'not to yours' in body
    assert 'Expired' not in body


def test_receipt_page_links_to_other_receipts_from_the_same_vendor(client, device):
    from models.user import Vendor

    vendor = Vendor.upsert(tin='100147181', name='PLASCO LIMITED')
    db.session.flush()
    _, first = store(device, vendor_id=vendor.id)
    _, second = store(device, vendor_id=vendor.id)

    body = client.get(f'/receipts/{first.id}').get_data(as_text=True)

    assert f'/receipts/{second.id}' in body


def test_receipt_page_404s_gracefully(client):
    response = client.get('/receipts/999999')
    assert response.status_code == 302  # back to the dashboard with a flash


def test_receipt_page_requires_a_login(app, device):
    _, receipt = store(device)
    response = app.test_client().get(f'/receipts/{receipt.id}')
    assert response.status_code == 302
    assert '/admin/login' in response.headers['Location']


# --- Retry ------------------------------------------------------------------

def test_retrying_a_failed_submission_puts_it_back_on_the_queue(client, device, monkeypatch):
    import main
    monkeypatch.setattr(main.gevent, 'spawn', lambda *a, **k: None)

    submission, _ = store(device, status='failed', error='TraReceiptNotUploaded: not there yet')
    submission.retry_count = 6
    db.session.commit()

    response = client.post(f'/submissions/{submission.id}/retry')

    assert response.status_code == 202
    refreshed = db.session.get(Submission, submission.id)
    assert refreshed.status == 'queued'
    assert refreshed.error_message is None
    # A fresh attempt, not the next tick of an exhausted schedule.
    assert refreshed.retry_count == 0
    assert refreshed.next_attempt_at is None


def test_a_completed_submission_cannot_be_retried(client, device):
    submission, _ = store(device, status='completed')
    assert client.post(f'/submissions/{submission.id}/retry').status_code == 409


def test_retrying_something_that_does_not_exist_is_a_404(client):
    assert client.post('/submissions/999999/retry').status_code == 404


# --- Re-analysis ------------------------------------------------------------

def test_reanalysis_replaces_the_judgment_but_never_the_facts(client, device, config,
                                                              receipt_html, monkeypatch):
    """
    The model may revise what it thinks of a receipt. It may not revise the receipt.

    Totals, TIN and date came from the verified page and stay exactly as parsed; only
    the category and the narrative are replaced.
    """
    import main

    _, receipt = store(device, source_html=receipt_html, category='other')
    original = (receipt.total_incl_tax_cents, receipt.vendor_tin, receipt.receipt_date)

    monkeypatch.setattr(config.__class__, 'is_configured', lambda self: True)
    monkeypatch.setattr(main, 'analyse_receipt', lambda *a, **k: {
        'category': 'utilities',
        'llm_extracted_description': 'Water supply.',
        'llm_tax_analysis': 'Deductible under section 11.',
    })

    response = client.post(f'/receipts/{receipt.id}/reanalyse')

    assert response.status_code == 200
    refreshed = db.session.get(Receipt, receipt.id)
    assert refreshed.category == 'utilities'
    assert refreshed.llm_status == 'ok'
    assert (refreshed.total_incl_tax_cents, refreshed.vendor_tin, refreshed.receipt_date) == original


def test_reanalysis_needs_a_stored_page_to_re_read(client, device, config, monkeypatch):
    """Without the source HTML there is nothing to re-read, and TRA is not asked again."""
    monkeypatch.setattr(config.__class__, 'is_configured', lambda self: True)
    _, receipt = store(device, source_html=None)

    response = client.post(f'/receipts/{receipt.id}/reanalyse')

    assert response.status_code == 409
    assert 'no stored TRA page' in response.get_json()['error']


def test_reanalysis_reports_an_unavailable_model_rather_than_failing_silently(
        client, device, config, receipt_html, monkeypatch):
    import main
    from utils.llm_processor import LlmUnavailable

    _, receipt = store(device, source_html=receipt_html, category='other')
    monkeypatch.setattr(config.__class__, 'is_configured', lambda self: True)

    def unavailable(*args, **kwargs):
        raise LlmUnavailable('provider is down')
    monkeypatch.setattr(main, 'analyse_receipt', unavailable)

    response = client.post(f'/receipts/{receipt.id}/reanalyse')

    assert response.status_code == 503
    # The receipt keeps the judgment it already had rather than losing it.
    assert db.session.get(Receipt, receipt.id).category == 'other'


# --- Export -----------------------------------------------------------------

def test_csv_export_carries_the_computed_columns(client, device):
    store(device)

    body = client.get('/export/csv').get_data(as_text=True)

    assert 'Input VAT Recoverable' in body
    assert 'Compliance Score' in body
    assert '18.00' in body


def test_csv_export_states_why_a_claim_is_blocked(client, device):
    store(device, customer_id='100999888')

    body = client.get('/export/csv').get_data(as_text=True)

    assert 'it is not issued to your TIN' in body


def test_the_export_carries_exactly_what_the_filter_matches(client, device, second_device):
    """
    The download and the screen it was started from have to mean the same thing.

    They did not: the export read its own dates and its own search out of the query
    string and ignored everything else, so a table narrowed to one category on one
    device exported the whole ledger. That difference only surfaces after somebody has
    filed the number.
    """
    store(device, category='fuel', vendor='WANTED LTD')
    store(device, category='meals', vendor='WRONG CATEGORY LTD')
    store(second_device, category='fuel', vendor='WRONG DEVICE LTD')

    body = client.get(f'/export/csv?category=fuel&device={device.id}').get_data(as_text=True)

    assert 'WANTED LTD' in body
    assert 'WRONG CATEGORY LTD' not in body
    assert 'WRONG DEVICE LTD' not in body


def test_the_export_names_the_device_behind_each_row(client, device):
    store(device)

    body = client.get('/export/csv').get_data(as_text=True)

    assert 'Device' in body.splitlines()[0]
    assert 'Test device' in body.splitlines()[1]


def test_the_export_still_reports_a_row_that_never_produced_a_receipt(client, device):
    """
    Filtering to Failed and downloading an empty file would be a straight lie.

    The receipt columns are blank because there is no receipt, and the row is padded to
    the header's width so it still lines up under the columns it has nothing to say about.
    """
    store(device, status='failed', description='blurred photo', error='TraReceiptNotUploaded')

    lines = client.get('/export/csv?tab=failed').get_data(as_text=True).splitlines()

    assert len(lines) == 2
    assert lines[1].split(',')[1] == 'failed'
    assert 'blurred photo' in lines[1]
    assert len(lines[1].split(',')) == len(lines[0].split(','))


# --- Vendors and the VAT ledger ---------------------------------------------

def test_vendor_page_groups_one_supplier_spelled_two_ways(client, device):
    """
    'PLASCO LIMITED' and 'Plasco Ltd' under one TIN are one supplier.

    Grouping on the printed name splits a vendor into several and makes the spend
    analysis useless, which is why receipts hang off a Vendor keyed on TIN.
    """
    from models.user import Vendor

    vendor = Vendor.upsert(tin='100147181', name='PLASCO LIMITED')
    db.session.flush()
    store(device, vendor='PLASCO LIMITED', vendor_id=vendor.id, total=100_00)
    store(device, vendor='Plasco Ltd', vendor_id=vendor.id, total=300_00)

    body = client.get('/vendors').get_data(as_text=True)

    assert body.count('<tr>') == 2          # one header row, one vendor row
    assert '400.00' in body                 # the two receipts summed
    assert '200.00' in body                 # and their average ticket


def test_vendor_page_excludes_cancelled_receipts_from_the_spend(client, device):
    from models.user import Vendor

    vendor = Vendor.upsert(tin='100147181', name='PLASCO LIMITED')
    db.session.flush()
    store(device, vendor_id=vendor.id, total=500_00)
    store(device, vendor_id=vendor.id, total=900_00, is_cancelled=True)

    assert '500.00' in client.get('/vendors').get_data(as_text=True)


def test_vat_ledger_totals_only_what_is_actually_recoverable(client, device):
    """
    Two receipts of 18.00 VAT each, one issued to another TIN.

    The ledger must show 36.00 charged and 18.00 recoverable - filing 36.00 is the
    mistake this whole view exists to prevent.
    """
    store(device, when=date(2026, 5, 4), total=118_00, tax=18_00)
    store(device, when=date(2026, 5, 9), total=118_00, tax=18_00, customer_id='100999888')

    body = client.get('/vat-ledger?period=2026-05').get_data(as_text=True)

    assert 'of 36.00 charged' in body
    assert 'of VAT you cannot claim' in body
    assert 'it is not issued to your TIN' in body


def test_vat_ledger_covers_exactly_one_calendar_month(client, device):
    store(device, when=date(2026, 4, 30), total=118_00, tax=18_00)
    store(device, when=date(2026, 5, 1), total=118_00, tax=18_00)
    store(device, when=date(2026, 5, 31), total=118_00, tax=18_00)
    store(device, when=date(2026, 6, 1), total=118_00, tax=18_00)

    body = client.get('/vat-ledger?period=2026-05').get_data(as_text=True)

    assert '2 receipt(s)' in body


def test_vat_ledger_falls_back_to_this_month_on_a_bad_period(client, device):
    assert client.get('/vat-ledger?period=nonsense').status_code == 200


def test_vat_ledger_warns_when_the_business_tin_is_unset(app, device):
    """Without it, every figure on the page rests on an assumption worth stating."""
    from models.user import InstanceConfig

    db.session.add(InstanceConfig(admin_email='a@b.c', totp_secret='S'))
    db.session.commit()

    client = app.test_client()
    with client.session_transaction() as session:
        session['admin_logged_in'] = True

    assert b'Your own TIN is not set' in client.get('/vat-ledger').data


# --- Fuzzy duplicates -------------------------------------------------------

def test_the_same_purchase_submitted_twice_is_flagged(client, device):
    """
    Verification-code dedup cannot catch a photo and a URL of one purchase.

    The photo has no code to match on, so both are stored and the expense is counted
    twice. Same vendor, same date, same total is what that looks like.
    """
    from models.user import Vendor

    vendor = Vendor.upsert(tin='100147181', name='PLASCO LIMITED')
    db.session.flush()
    _, first = store(device, vendor_id=vendor.id, when=date(2026, 5, 4), total=118_00)
    _, second = store(device, vendor_id=vendor.id, when=date(2026, 5, 4), total=118_00)

    body = client.get(f'/receipts/{first.id}').get_data(as_text=True)

    assert 'Possibly the same purchase as' in body
    assert f'receipt #{second.id}' in body


def test_different_amounts_from_one_vendor_are_not_duplicates(client, device):
    from models.user import Vendor

    vendor = Vendor.upsert(tin='100147181', name='PLASCO LIMITED')
    db.session.flush()
    _, first = store(device, vendor_id=vendor.id, when=date(2026, 5, 4), total=118_00)
    store(device, vendor_id=vendor.id, when=date(2026, 5, 4), total=236_00)

    assert 'Possibly the same purchase' not in client.get(f'/receipts/{first.id}').get_data(as_text=True)


def test_matching_amounts_from_different_vendors_are_not_duplicates(client, device):
    from models.user import Vendor

    one = Vendor.upsert(tin='100147181', name='PLASCO LIMITED')
    two = Vendor.upsert(tin='100200300', name='NMB BANK')
    db.session.flush()
    # Each receipt prints the TIN of the supplier it is filed under, which is the only
    # state the pipeline can produce: Vendor.upsert is handed the receipt's own printed
    # fields, so the row and the print never disagree. See Receipt.vendor_key.
    _, first = store(device, vendor_id=one.id, tin='100147181', when=date(2026, 5, 4), total=118_00)
    store(device, vendor_id=two.id, tin='100200300', vendor='NMB BANK',
          when=date(2026, 5, 4), total=118_00)

    assert 'Possibly the same purchase' not in client.get(f'/receipts/{first.id}').get_data(as_text=True)


# --- Hostile receipt content ------------------------------------------------

def test_a_vendor_name_cannot_break_out_of_the_bootstrap_script(client, device):
    """
    Receipt fields are text lifted off a page somebody else served.

    The dashboard embeds a page of them inside a <script> block. Serialised with a
    plain json.dumps, a vendor name containing '</script>' closes that block early and
    everything after it is parsed as markup - stored XSS in the admin's own dashboard.
    """
    store(device, vendor='</script><script>window.__pwned = 1;</script>')

    body = client.get('/').get_data(as_text=True)

    assert '</script><script>window.__pwned' not in body
    assert '\\u003c/script\\u003e' in body


def test_a_search_term_cannot_break_out_of_the_bootstrap_script(client, device):
    """The filter state is echoed back into the same block."""
    body = client.get('/?search=%3C/script%3E%3Cscript%3Ealert(1)%3C/script%3E').get_data(as_text=True)

    assert '</script><script>alert(1)' not in body


def test_hostile_receipt_text_is_escaped_on_the_receipt_page(client, device):
    """The detail page renders through Jinja, which escapes as it goes."""
    _, receipt = store(device, vendor='<img src=x onerror=alert(1)>')

    body = client.get(f'/receipts/{receipt.id}').get_data(as_text=True)

    assert '<img src=x onerror=alert(1)>' not in body
    assert '&lt;img src=x onerror=alert(1)&gt;' in body


def test_the_list_payload_omits_the_prose_behind_each_check(client, device):
    """
    The table renders a score and the ids of what failed, nothing more.

    Shipping fifty copies of every check's wording is most of the page weight for
    none of the page; the sentences live on /receipts/<id>, rendered server-side.
    """
    store(device)

    listed = client.get('/api/submissions').get_json()['submissions'][0]['receipt']['assessment']

    assert 'checks' not in listed
    assert listed['score'] == 100 and listed['failed'] == []


def test_a_single_receipt_still_carries_the_full_wording(client, device):
    """A webhook consumer gets one receipt at a time and wants the reasons with it."""
    import main

    _, receipt = store(device, customer_id='100999888')
    with main.app.app_context():
        payload = main.receipt_to_dict(db.session.get(Receipt, receipt.id))

    details = {check['id']: check['detail'] for check in payload['assessment']['checks']}
    assert 'not to yours' in details['buyer_tin']


# --- Insights ---------------------------------------------------------------

def test_insights_leads_with_the_vat_that_cannot_be_claimed(client, device):
    """The first thing the page must answer is what this period costs if ignored."""
    store(device, total=118_00, tax=18_00)
    store(device, total=118_00, tax=18_00, customer_id='100999888')

    body = client.get('/insights').get_data(as_text=True)

    assert 'Input VAT recoverable' in body
    assert 'VAT you cannot claim' in body
    assert 'Why VAT is being lost' in body
    assert 'it is not issued to your TIN' in body


def test_insights_renders_on_an_empty_instance(client, device):
    """A page of figures must not fail when there are no figures."""
    response = client.get('/insights')

    assert response.status_code == 200
    assert b'No receipts dated in this period yet' in response.data


def test_insights_respects_its_window(client, device):
    store(device, when=date.today())
    store(device, when=date.today() - timedelta(days=200))

    assert '2 receipt(s) dated' in client.get('/insights?window=365').get_data(as_text=True)
    assert '1 receipt(s) dated' in client.get('/insights?window=30').get_data(as_text=True)


def test_insights_falls_back_on_a_nonsense_window(client, device):
    store(device)
    assert client.get('/insights?window=; DROP TABLE receipt').status_code == 200
    assert Receipt.query.count() == 1


def test_insights_narrows_to_the_same_filter_the_dashboard_speaks(client, device, second_device):
    """
    The pickers moved off the dashboard and onto this page, so the filter has to mean
    the same thing here. Every key the table reads is read here too, and by the same
    function - two filter languages that agree until they don't is how a figure quoted
    off one page ends up disagreeing with the table it was read from.
    """
    store(device, total=100_00, tax=0, category='fuel')
    store(second_device, total=700_00, tax=0, category='fuel')
    store(second_device, total=300_00, tax=0, category='travel')

    everything = client.get('/insights').get_data(as_text=True)
    assert '3 receipt(s) dated' in everything

    one_device = client.get(f'/insights?device={second_device.id}').get_data(as_text=True)
    assert '2 receipt(s) dated' in one_device

    # Compound, as on the dashboard: this device, and only what it spent on fuel.
    both = client.get(f'/insights?device={second_device.id}&category=fuel').get_data(as_text=True)
    assert '1 receipt(s) dated' in both


def test_insights_says_what_it_is_narrowed_by_and_can_undo_it(client, device):
    """
    No filter may be invisible. A page of figures narrowed by something the reader
    cannot see is a page answering a question nobody asked, and the only symptom is a
    number that looks a little small.
    """
    store(device, category='fuel')

    body = client.get('/insights?category=fuel&search=plasco').get_data(as_text=True)

    assert 'Showing only' in body
    assert 'plasco' in body
    # Each chip is a link back to the same page without that one value, so removing a
    # filter never means retyping the rest of it.
    assert 'search=plasco' in body and 'category=fuel' in body


def test_insights_carries_its_filter_to_the_ledger_and_the_csv(client, device):
    """
    A filter has to be able to leave with you. Both links carry it: one to the rows
    behind these figures, one to a spreadsheet of exactly those rows.
    """
    store(device, category='fuel')

    body = client.get('/insights?category=fuel&start_date=2020-01-01').get_data(as_text=True)

    assert '/export/csv?' in body
    assert 'Open in ledger' in body
    for link in ('/?', '/export/csv?'):
        assert any(
            line.count('category=fuel') and line.count('start_date=2020-01-01')
            for line in body.splitlines() if link in line
        ), link


def test_insights_offers_the_filter_it_is_standing_in_rather_than_a_dead_end(client, device):
    """A filter that matches nothing must still be visible, and still be removable."""
    store(device, category='fuel')

    body = client.get('/insights?category=travel').get_data(as_text=True)

    assert 'No receipts dated in this period yet' in body
    assert 'Nothing matches this filter' in body
    # The pickers are still drawn, so widening is a click rather than a URL edit.
    assert 'Showing only' in body


def test_insights_breaks_spending_down_by_region(client, device):
    """Regions come from the device's coordinates, worked out offline."""
    store(device, location='-6.7924,39.2083 - Kariakoo', total=100_00)
    store(device, location='-2.5164,32.9175', total=300_00)

    body = client.get('/insights').get_data(as_text=True)

    assert 'Dar es Salaam' in body
    assert 'Mwanza' in body


def test_insights_reports_a_price_rise_with_its_evidence(client, device):
    from models.user import Vendor

    vendor = Vendor.upsert(tin='100147181', name='PLASCO LIMITED')
    db.session.flush()
    for day, amount in ((60, 30_000_00), (30, 30_000_00), (1, 36_000_00)):
        _, receipt = store(device, vendor_id=vendor.id, total=amount, tax=0,
                           when=date.today() - timedelta(days=day))
        receipt.items[0].quantity = 10
    db.session.commit()

    body = client.get('/insights').get_data(as_text=True)

    assert 'Prices that have moved' in body
    assert '+20.0%' in body


def test_receipt_page_names_the_approximate_region(client, device):
    _, receipt = store(device, location='-6.7924,39.2083 - Kariakoo')

    body = client.get(f'/receipts/{receipt.id}').get_data(as_text=True)

    assert 'Dar es Salaam' in body
    # Never presented as something the receipt itself states.
    assert '(approximate)' in body


def test_a_vendor_without_a_vrn_is_not_badged_vat_registered(client, device):
    """
    The badge answers 'may I recover input VAT from this supplier?', so it may only
    appear against a real VRN. It was previously driven by the VRN field being
    non-empty, and TRA fills that field with the text 'NOT REGISTERED' - which put
    the green badge on every unregistered supplier in the list.
    """
    from models.user import Vendor

    registered = Vendor.upsert(tin='100127423', name='BONITE BOTTLERS LTD', vrn='10007206H')
    unregistered = Vendor.upsert(tin='114685836', name='MOHAMED JUMA MUSSA')
    db.session.flush()
    store(device, vendor='BONITE BOTTLERS LTD', tin='100127423', vendor_id=registered.id)
    store(device, vendor='MOHAMED JUMA MUSSA', tin='114685836', vendor_id=unregistered.id,
          vrn=None)
    db.session.commit()

    body = client.get('/vendors').get_data(as_text=True)

    assert body.count('VAT registered') == 1
    assert 'no VRN' in body

    detail = client.get(f'/vendors/{unregistered.lookup_key}').get_data(as_text=True)
    assert 'VAT registered' not in detail
    assert 'no VRN on file' in detail
