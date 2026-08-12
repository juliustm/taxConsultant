# tests/test_dashboard_javascript.py
"""
The dashboard's own JavaScript, executed rather than merely rendered.

Every other test in this suite asserts on the HTML the server produced. That is not
enough: a dashboard whose bootstrap block never runs serves byte-perfect HTML and
displays nothing at all, which is exactly what shipped. The page is driven entirely
by Alpine reading three constants out of a <script> block, so those constants have to
be checked the way a browser sees them - after HTML parsing, and then evaluated.

The failure this was written for: a comment inside the bootstrap block contained a
literal closing script tag. The HTML parser honours that even inside a comment, so
the block ended early, the three constants were never defined, and every x-for on the
page rendered nothing.
"""
import json
import shutil
import subprocess
from datetime import date, time
from html.parser import HTMLParser

import pytest

from models.user import db, Receipt, ReceiptItem, ReceiptTaxLine, Submission


class ScriptCollector(HTMLParser):
    """
    Collects script contents the way a browser does.

    HTMLParser terminates a script element at the first closing tag in the raw text,
    comment or no comment, which is precisely the behaviour under test.
    """

    def __init__(self):
        super().__init__()
        self.scripts = []
        self._in_script = False

    def handle_starttag(self, tag, attrs):
        if tag == 'script' and not dict(attrs).get('src'):
            self._in_script = True
            self.scripts.append('')

    def handle_endtag(self, tag):
        if tag == 'script':
            self._in_script = False

    def handle_data(self, data):
        if self._in_script:
            self.scripts[-1] += data


@pytest.fixture
def dashboard_scripts(app, config):
    """Every inline script on the dashboard, as the browser would receive them."""
    config.business_tin = '108537108'

    submission = Submission(device_id=_device(app).id, input_type='url',
                            input_data='https://verify.tra.go.tz/X_000000', status='completed')
    db.session.add(submission)
    db.session.flush()

    receipt = Receipt(
        vendor_name='PLASCO LIMITED', vendor_tin='100147181', vrn='10007206H',
        receipt_verification_code='58E41A514', extraction_source='tra_html',
        customer_id_type='TIN', customer_id='108537108',
        receipt_date=date.today(), receipt_time=time(10, 30),
        total_incl_tax_cents=118_00, total_excl_tax_cents=100_00, total_tax_cents=18_00,
        device_id=submission.device_id, submission_id=submission.id,
    )
    receipt.items.append(ReceiptItem(line_number=1, description='DIESEL AGO',
                                     amount_cents=118_00, tax_code='A'))
    receipt.tax_lines.append(ReceiptTaxLine(code='A', rate=18, amount_cents=18_00))
    db.session.add(receipt)
    db.session.commit()

    client = app.test_client()
    with client.session_transaction() as session:
        session['admin_logged_in'] = True

    collector = ScriptCollector()
    collector.feed(client.get('/').get_data(as_text=True))
    return collector.scripts


def _device(app):
    from models.user import Device

    device = Device.query.first()
    if device is None:
        device = Device(name='Test device', api_key='test-key')
        db.session.add(device)
        db.session.commit()
    return device


def test_the_bootstrap_block_survives_html_parsing(dashboard_scripts):
    """
    All three constants must still be inside the first script after parsing.

    If anything in that block closes the script element early, the assignments fall
    out of it and land in the document as text - the page then renders its shell and
    nothing else.
    """
    bootstrap = dashboard_scripts[0]

    assert 'const initialPage =' in bootstrap
    assert 'const initialStats =' in bootstrap
    assert 'const initialFilters =' in bootstrap


def test_the_bootstrapped_page_carries_the_receipt(dashboard_scripts):
    """The one stored receipt has to be in the payload the browser is handed."""
    bootstrap = dashboard_scripts[0]
    payload = json.loads(bootstrap.split('const initialPage =', 1)[1].split(';', 1)[0])

    assert payload['total'] == 1
    assert payload['submissions'][0]['receipt']['vendor_name'] == 'PLASCO LIMITED'


@pytest.mark.skipif(not shutil.which('node'), reason='node is not installed')
def test_the_dashboard_component_initialises(dashboard_scripts, tmp_path):
    """
    dashboard() is built and read, in a real JavaScript engine.

    A syntax error anywhere in the page's script, or a constant that never got
    defined, stops the component from being constructed at all - and Alpine reports
    that only to a browser console nobody is watching.
    """
    driver = """
    const component = dashboard();
    console.log(JSON.stringify({
        rows: component.page.submissions.length,
        total: component.page.total,
        vendor: component.page.submissions[0].receipt.vendor_name,
        score: component.page.submissions[0].receipt.assessment.score,
        cards: component.statCards.length,
        cardAmount: component.statCards[0].amount,
        cardExact: component.statCards[0].exact,
        cardClass: component.statCards[0].amountClass,
        tabCount: component.page.tab_counts.processed,
        activeTab: component.filters.tab,
    }));
    """
    bundle = tmp_path / 'dashboard.js'
    bundle.write_text('\n'.join(dashboard_scripts) + driver)

    result = subprocess.run(['node', str(bundle)], capture_output=True, text=True, timeout=30)
    assert result.returncode == 0, result.stderr

    state = json.loads(result.stdout)
    assert state['rows'] == 1
    assert state['total'] == 1
    assert state['vendor'] == 'PLASCO LIMITED'
    assert state['score'] == 100
    # Four period cards, each formatted rather than left as raw cents. The headline
    # figure and the exact one are separate fields: the card shows whole shillings at a
    # size chosen from their length, and keeps the full amount for the hover title.
    assert state['cards'] == 4
    assert ',' in state['cardAmount'] or state['cardAmount'].isdigit()
    assert 'TZS' in state['cardExact']
    # The size class is what the font-size fix turns on; an undefined one is how the
    # figure silently loses its styling.
    assert state['cardClass']
    assert state['tabCount'] == 1
    assert state['activeTab'] == 'processed'


LIVE_DRIVER = """
// The live feed, driven. EventSource does not exist in node, so it is stubbed with
// something that records what the page attached to it and lets the test fire the
// events a browser would.
const opened = [];
global.document = { hidden: false };
global.EventSource = class {
    constructor(url) { this.url = url; this.readyState = 0; this.listeners = {}; opened.push(this); }
    addEventListener(name, fn) { this.listeners[name] = fn; }
    close() { this.readyState = 2; }
};

const component = dashboard();
const reloads = [];
component.reload = (page, options) => { reloads.push({ page, options }); };
component.addNotification = () => {};
component.showBrowserNotification = () => {};
component.soundGenerator = {
    playSuccess() {}, playFailed() {}, playDuplicate() {}, playQueued() {},
};

component.openStream();
const stream = opened[0];

// Opening is itself a catch-up: whatever happened while the page had no connection
// has to be fetched, not waited for.
stream.onopen();
const reloadsAfterOpen = reloads.length;
const liveAfterOpen = component.live;

// A burst - one runner tick finishing several receipts - is one refetch, not several.
stream.onmessage({ data: JSON.stringify({ event_type: 'submission.processed', data: { submission_id: 1, stats: {} } }) });
stream.onmessage({ data: JSON.stringify({ event_type: 'submission.processed', data: { submission_id: 2, stats: {} } }) });
stream.onmessage({ data: JSON.stringify({ event_type: 'submission.processing', data: { submission_id: 3 } }) });

setTimeout(() => {
    const burst = reloads.length - reloadsAfterOpen;

    // A dropped connection is visible, and the watchdog opens a new one.
    stream.onerror();
    const liveAfterError = component.live;
    component.lastBeat = Date.now() - 120000;   // Long past the server's heartbeat.
    component.checkStreamHealth();

    console.log(JSON.stringify({
        url: stream.url,
        liveAfterOpen,
        reloadsAfterOpen,
        burst,
        liveAfterError,
        streams: opened.length,
        closedFirst: stream.readyState === 2,
        allQuiet: reloads.every((r) => r.options && r.options.quiet),
        heartbeatListener: typeof stream.listeners.ping === 'function',
    }));
}, 500);
"""


@pytest.mark.skipif(not shutil.which('node'), reason='node is not installed')
def test_the_live_feed_catches_up_coalesces_and_reconnects(dashboard_scripts, tmp_path):
    """
    The behaviour the dashboard was missing, executed rather than read.

    What shipped was one EventSource with an onmessage handler and nothing else: no
    catch-up on connect, no way to notice a connection that had died, and a full page
    request per event. A receipt processed while the connection was down never appeared
    at all until somebody reloaded the page by hand.
    """
    bundle = tmp_path / 'live.js'
    bundle.write_text('\n'.join(dashboard_scripts) + LIVE_DRIVER)

    result = subprocess.run(['node', str(bundle)], capture_output=True, text=True, timeout=30)
    assert result.returncode == 0, result.stderr
    state = json.loads(result.stdout)

    assert state['url'] == '/stream'
    assert state['liveAfterOpen'] is True
    # Connecting is a catch-up, not just a subscription.
    assert state['reloadsAfterOpen'] == 1
    # Three events, one refetch.
    assert state['burst'] == 1
    # Every automatic refetch is quiet: no spinner over a table someone is reading.
    assert state['allQuiet'] is True
    # The heartbeat has somewhere to land, which is what makes silence detectable.
    assert state['heartbeatListener'] is True

    # A failed connection says so, and is replaced rather than left to sit there.
    assert state['liveAfterError'] is False
    assert state['streams'] == 2
    assert state['closedFirst'] is True
