# tests/test_receipt_pipeline.py
"""
End-to-end behaviour of a submission, with the portal and the LLM stubbed out.

These cover the things the receipt page itself cannot: that money survives storage as
an exact number, that vendors group by TIN, that cancelled and test receipts stay out
of the spending totals, and that a receipt is still recorded in full when the LLM is
unreachable.
"""
from datetime import date, timedelta
from decimal import Decimal

import pytest

from models.user import db, Receipt, Submission, Vendor
from utils.llm_processor import LlmUnavailable
from utils.money import to_cents


@pytest.fixture
def submit(app, device):
    """Queues a TRA URL submission and returns it."""
    def _submit(url='https://verify.tra.go.tz/58E41A514_092022', description=None):
        submission = Submission(
            device_id=device.id, input_type='url', input_data=url, description=description,
        )
        db.session.add(submission)
        db.session.commit()
        return submission
    return _submit


@pytest.fixture
def portal(monkeypatch, receipt_html):
    """Serves saved HTML in place of the live portal."""
    import main

    def _serve(html=receipt_html):
        monkeypatch.setattr(main, 'fetch_receipt_html', lambda url: html)
    return _serve


def store_receipt(app, device, **overrides):
    """A stored receipt, for the queries that do not need the whole pipeline."""
    submission = Submission(device_id=device.id, input_type='url', input_data='x')
    db.session.add(submission)
    db.session.flush()

    fields = {
        'receipt_date': date.today(), 'total_incl_tax_cents': 100_00,
        'device_id': device.id, 'submission_id': submission.id,
    }
    fields.update(overrides)
    receipt = Receipt(**fields)
    db.session.add(receipt)
    db.session.commit()
    return receipt


# --- Facts come from the page, not the model --------------------------------

def test_url_submission_stores_parsed_facts(app, device, config, submit, portal, monkeypatch):
    import main

    portal()
    monkeypatch.setattr(main, 'analyse_receipt', lambda *a, **k: {
        'category': 'utilities',
        'llm_extracted_description': 'Water supply purchase.',
        'llm_tax_analysis': 'Deductible under Section 11.',
    })
    monkeypatch.setattr(config.__class__, 'is_configured', lambda self: True)

    submission = submit()
    main.process_submission(submission)

    receipt = Receipt.query.one()
    assert submission.status == 'completed'
    assert receipt.extraction_source == 'tra_html'
    assert receipt.vendor_name == 'PLASCO LIMITED'
    assert receipt.vendor_tin == '100147181'
    assert receipt.efd_serial == '03TZ343001520'
    assert receipt.z_number == '169'
    assert receipt.tax_office == 'LARGE TAXPAYERS DEPARTMENT'
    assert receipt.receipt_date == date(2022, 3, 8)
    assert receipt.receipt_time.isoformat() == '09:20:22'
    assert receipt.total_amount == Decimal('24273000.00')
    assert receipt.source_html.startswith('<!DOCTYPE html>')
    # The judgment, and only the judgment, came from the model.
    assert receipt.category == 'utilities'
    assert receipt.llm_status == 'ok'


def test_line_items_and_tax_lines_are_kept(app, device, config, submit, portal, monkeypatch):
    import main

    portal()
    monkeypatch.setattr(main, 'analyse_receipt', lambda *a, **k: {'category': 'other'})

    main.process_submission(submit())

    receipt = Receipt.query.one()
    assert [(i.description, i.amount, i.tax_code) for i in receipt.items] == [
        ('SUMMARIZED SALE - E', Decimal('24273000.00'), 'EX'),
    ]
    assert [(t.code, t.amount) for t in receipt.tax_lines] == [('EX', Decimal('0.00'))]


def test_the_llm_is_never_asked_for_facts(app, device, config, submit, portal, monkeypatch):
    """The judgment call may only see the structured facts, never the page."""
    import main

    portal()
    seen = {}

    def _capture(facts, cfg, user_note=None):
        seen['facts'] = facts
        return {'category': 'fuel', 'llm_tax_analysis': 'ok'}

    monkeypatch.setattr(main, 'analyse_receipt', _capture)
    monkeypatch.setattr(config.__class__, 'is_configured', lambda self: True)

    main.process_submission(submit())

    assert 'html' not in repr(seen['facts']).lower()
    assert seen['facts']['total_incl_tax'] == '24273000.00'


# --- Money ------------------------------------------------------------------

def test_amounts_are_stored_as_exact_cents(app, device, config, submit, portal, monkeypatch):
    import main

    portal()
    monkeypatch.setattr(main, 'analyse_receipt', lambda *a, **k: {})

    main.process_submission(submit())

    receipt = Receipt.query.one()
    assert receipt.total_incl_tax_cents == 2_427_300_000
    assert isinstance(receipt.total_incl_tax_cents, int)


def test_summing_receipts_does_not_drift(app, device):
    """
    Ten receipts of 0.10 and 0.20 are exactly 3.00. Summed as floats they are not,
    which is the whole reason the column is an integer.
    """
    for _ in range(10):
        store_receipt(app, device, total_incl_tax_cents=to_cents('0.10'))
        store_receipt(app, device, total_incl_tax_cents=to_cents('0.20'))

    total = db.session.query(db.func.sum(Receipt.total_incl_tax_cents)).scalar()

    assert total == 300
    assert Receipt.query.first().total_amount == Decimal('0.10')


# --- Cancelled and test receipts --------------------------------------------

def test_cancelled_receipt_is_stored_but_not_counted(app, device, config, submit, portal,
                                                     receipt_html, monkeypatch):
    import main

    portal(receipt_html.replace(
        '<!-- Cancelled Watermark -->',
        '<!-- Cancelled Watermark --><div class="cancelled-watermark"></div>',
    ))

    called = []
    monkeypatch.setattr(main, 'analyse_receipt', lambda *a, **k: called.append(1) or {})

    submission = submit()
    main.process_submission(submission)

    receipt = Receipt.query.one()
    assert submission.status == 'completed'
    assert receipt.is_cancelled is True
    assert receipt.is_expense is False
    # Nothing to judge on a voided receipt, so no tokens are spent on one.
    assert called == []
    assert 'not an expense' in receipt.submission.description

    # And it stays out of the spending figures.
    receipt.receipt_date = date.today()
    db.session.commit()
    assert main.calculate_dashboard_stats()['today'] == {'count': 0, 'total_cents': 0, 'total': 0.0}


def test_test_receipts_are_excluded_from_totals(app, device):
    import main

    store_receipt(app, device, total_incl_tax_cents=500_00)
    store_receipt(app, device, total_incl_tax_cents=900_00, is_test=True)
    store_receipt(app, device, total_incl_tax_cents=700_00, is_cancelled=True)

    stats = main.calculate_dashboard_stats()['today']

    assert stats['count'] == 1
    assert stats['total_cents'] == 500_00


# --- Dashboard axis ---------------------------------------------------------

def test_stats_count_when_money_was_spent_not_when_it_was_scanned(app, device):
    """
    A receipt from last year that was scanned today is last year's expense. Keying the
    dashboard off processed_at put it in 'today'.
    """
    import main

    old = store_receipt(app, device, receipt_date=date.today() - timedelta(days=200),
                        total_incl_tax_cents=1_000_00)
    store_receipt(app, device, receipt_date=date.today(), total_incl_tax_cents=25_00)

    stats = main.calculate_dashboard_stats()

    # Both were processed just now; only one was spent today.
    assert old.processed_at.date() == date.today()
    assert stats['today'] == {'count': 1, 'total_cents': 25_00, 'total': 25.0}
    assert stats['1y']['count'] == 2
    assert stats['1y']['total_cents'] == 1_025_00


def test_stats_ignore_receipts_dated_beyond_the_window(app, device):
    import main

    store_receipt(app, device, receipt_date=date.today() - timedelta(days=8), total_incl_tax_cents=1_00)

    assert main.calculate_dashboard_stats()['7d']['count'] == 0
    assert main.calculate_dashboard_stats()['4w']['count'] == 1


# --- Vendors ----------------------------------------------------------------

def test_vendors_group_by_tin_not_by_name(app, device):
    """The same taxpayer spelled three ways is one vendor."""
    for name in ('PLASCO LIMITED', 'Plasco Ltd', 'plasco  limited.'):
        vendor = Vendor.upsert(tin='100147181', name=name, vrn='10007206H')
        db.session.commit()

    assert Vendor.query.count() == 1
    assert vendor.tin == '100147181'
    # The newest spelling wins for display, but the grouping key never moved.
    assert vendor.name == 'plasco  limited.'
    assert vendor.lookup_key == 'tin:100147181'


def test_vendors_without_a_tin_fall_back_to_their_name(app, device):
    Vendor.upsert(name='Corner Duka')
    Vendor.upsert(name='CORNER DUKA')
    Vendor.upsert(name='Other Duka')
    db.session.commit()

    assert Vendor.query.count() == 2


def test_receipt_is_attached_to_its_vendor(app, device, config, submit, portal, monkeypatch):
    import main

    portal()
    monkeypatch.setattr(main, 'analyse_receipt', lambda *a, **k: {})

    main.process_submission(submit())

    receipt = Receipt.query.one()
    assert receipt.vendor.tin == '100147181'
    assert receipt.vendor.is_vat_registered is True
    assert receipt.vendor.receipts == [receipt]


# --- Working without the LLM ------------------------------------------------

def test_receipt_is_recorded_in_full_when_the_llm_is_down(app, device, config, submit, portal, monkeypatch):
    import main

    portal()

    def _explode(*args, **kwargs):
        raise LlmUnavailable('connection refused')

    monkeypatch.setattr(main, 'analyse_receipt', _explode)
    monkeypatch.setattr(config.__class__, 'is_configured', lambda self: True)

    submission = submit()
    main.process_submission(submission)

    receipt = Receipt.query.one()
    assert submission.status == 'completed'
    assert receipt.llm_status == 'unavailable'
    assert receipt.total_amount == Decimal('24273000.00')
    assert receipt.vendor_tin == '100147181'
    assert len(receipt.items) == 1
    # The description falls back to the receipt itself rather than going blank.
    assert 'SUMMARIZED SALE - E' in submission.description
    assert 'PLASCO LIMITED' in submission.description


def test_unconfigured_instance_still_processes_a_url_receipt(app, device, config, submit, portal):
    import main

    portal()
    submission = submit()

    main.process_submission(submission)

    receipt = Receipt.query.one()
    assert submission.status == 'completed'
    assert receipt.llm_status == 'skipped'
    assert receipt.category is None


# --- Duplicates and failures ------------------------------------------------

def test_duplicate_is_detected_before_the_llm_is_called(app, device, config, submit, portal, monkeypatch):
    import main

    portal()
    calls = []
    monkeypatch.setattr(main, 'analyse_receipt', lambda *a, **k: calls.append(1) or {})
    monkeypatch.setattr(config.__class__, 'is_configured', lambda self: True)

    main.process_submission(submit())
    second = submit()
    main.process_submission(second)

    assert Receipt.query.count() == 1
    assert second.status == 'duplicate'
    assert calls == [1]  # only the first submission cost a call


def test_dashboard_and_csv_render_a_stored_receipt(app, device, config, submit, portal, monkeypatch):
    """The two pages that read a receipt back out, over the real templates."""
    import json
    import main

    portal()
    monkeypatch.setattr(main, 'analyse_receipt', lambda *a, **k: {
        'category': 'utilities', 'llm_extracted_description': 'Water supply.',
        'llm_tax_analysis': 'Deductible.',
    })
    monkeypatch.setattr(config.__class__, 'is_configured', lambda self: True)
    main.process_submission(submit())

    client = app.test_client()
    with client.session_transaction() as session:
        session['admin_logged_in'] = True

    page = client.get('/')
    assert page.status_code == 200
    assert b'PLASCO LIMITED' in page.data
    # The bootstrap payload carries cents, so the browser never sums floats.
    bootstrap = page.data.decode()
    assert '"total_amount_cents": 2427300000' in bootstrap.replace('&#34;', '"')

    export = client.get('/export/csv')
    assert export.status_code == 200
    body = export.data.decode()
    assert '24273000.00' in body
    assert '03TZ343001520' in body           # EFD serial
    assert 'SUMMARIZED SALE - E' in body     # line items
    assert json.loads(main.prepare_submissions_for_frontend([]))  == []


def test_unparsable_page_fails_the_submission_instead_of_guessing(app, device, config, submit, monkeypatch):
    import main

    monkeypatch.setattr(main, 'fetch_receipt_html',
                        lambda url: '<html>RECEIPT VERIFICATION CODE</html>')
    monkeypatch.setattr(main, 'analyse_receipt', lambda *a, **k: pytest.fail('LLM must not be a fallback'))
    monkeypatch.setattr(main, 'extract_receipt_details', lambda *a, **k: pytest.fail('LLM must not be a fallback'))

    submission = submit()
    main.process_submission(submission)

    assert Receipt.query.count() == 0
    assert submission.status == 'failed'
    assert 'Could not parse the TRA receipt page' in submission.error_message
