# tests/test_pwa_assets.py
"""
The scanner's static contract with itself.

None of this can fail loudly in production. A service worker that precaches a URL
which 404s still installs, still activates, and still reports success - the app simply
does not open the next time the phone is offline, in a shop, with no way to reach an
admin. The same is true of a vendored library that was never committed, and of the
receipt-code pattern drifting apart from the server's.

So the shell list, the vendored files and the shared regex are checked here rather
than trusted to review. This follows tests/test_dashboard_javascript.py, which exists
for the same reason: front-end mistakes in this project are silent by default.
"""
import json
import pathlib
import re
import shutil
import subprocess

import pytest

ROOT = pathlib.Path(__file__).resolve().parent.parent
STATIC = ROOT / 'static'
TEMPLATES = ROOT / 'templates'
SERVICE_WORKER = STATIC / 'js' / 'service-worker.js'


def app_shell():
    """The APP_SHELL list, read out of the worker rather than restated here."""
    source = SERVICE_WORKER.read_text()
    block = re.search(r'const APP_SHELL\s*=\s*\[(.*?)\];', source, re.S)
    assert block, 'APP_SHELL is no longer a literal array; the diagnostics page parses it too.'
    return re.findall(r"'([^']+)'", block.group(1))


@pytest.fixture
def client(app):
    return app.test_client()


def test_every_precached_url_actually_resolves(client):
    """
    The failure this exists for: renaming a template or a vendored file, and finding
    out weeks later from a phone that will not open on a market day.
    """
    missing = []
    for url in app_shell():
        response = client.get(url)
        if response.status_code != 200:
            missing.append(f'{url} -> {response.status_code}')

    assert not missing, 'Precached URLs that do not resolve: ' + ', '.join(missing)


def test_the_shell_covers_every_screen_and_engine():
    """A gap here is an app that installs cleanly and then cannot open offline."""
    shell = set(app_shell())
    source = SERVICE_WORKER.read_text()

    # One document, not three. All three /scan/ routes render identical HTML and the
    # view is chosen in the browser, so caching them separately only created copies
    # that drift: an app always entered at /scan/ never revalidated the other two, and
    # a deep link to History could be served a shell from an old deploy.
    assert '/scan/' in shell
    assert '/scan/history' not in shell
    assert '/scan/diagnostics' not in shell

    # ...which only holds together if every navigation resolves to that one document,
    # and the router that reads the path out of the URL is cached alongside it.
    assert "await cache.match(SHELL_DOCUMENT)" in source
    assert {'/scan/', '/scan/history', '/scan/diagnostics'} <= set(
        re.findall(r"'([^']+)'", re.search(
            r'const CACHEABLE_NAVIGATIONS = new Set\(\[(.*?)\]\)', source, re.S).group(1))
    )
    assert '/static/js/router.js' in shell

    assert '/static/js/pwa.js' in shell
    assert '/static/js/scanner.js' in shell
    assert '/static/css/scan.css' in shell
    # The decoder is useless without its WASM payload, and the payload is the one
    # entry big enough that a flaky first install is likely to drop it.
    assert '/static/js/vendor/zxing-reader.js' in shell
    assert '/static/js/vendor/zxing_reader.wasm' in shell


def test_the_vendored_libraries_are_committed():
    """
    Nothing may be fetched from a CDN at runtime.

    An offline app whose first paint depends on the public internet is not offline,
    and the deployments this serves are exactly the ones where it is not reachable.
    """
    for name in ('tailwind.min.js', 'alpine.min.js', 'alpine-collapse.min.js',
                 'idb.umd.min.js', 'zxing-reader.js', 'zxing_reader.wasm'):
        path = STATIC / 'js' / 'vendor' / name
        assert path.exists(), f'{name} has not been vendored'
        assert path.stat().st_size > 1000, f'{name} looks truncated'


def test_no_template_loads_a_resource_from_a_cdn():
    """
    Scripts and stylesheets only. An <a href> to the project's own repository is a
    link a human may click, not something the page needs in order to render.
    """
    offenders = []
    for template in (ROOT / 'templates').rglob('*.html'):
        source = template.read_text()
        for match in re.finditer(r'<script[^>]+src="(https?://[^"]+)"', source):
            offenders.append(f'{template.relative_to(ROOT)}: {match.group(1)}')
        for match in re.finditer(r'<link[^>]+rel="stylesheet"[^>]*href="(https?://[^"]+)"', source):
            offenders.append(f'{template.relative_to(ROOT)}: {match.group(1)}')

    assert not offenders, 'Runtime CDN dependencies: ' + ', '.join(offenders)


def test_the_icons_the_manifest_promises_exist(client):
    manifest = client.get('/scan/manifest.json').get_json()

    assert manifest['start_url'] == '/scan/'
    assert manifest['display'] == 'standalone'
    # Android needs a maskable icon or it draws a white box behind the artwork.
    assert any(icon['purpose'] == 'maskable' for icon in manifest['icons'])

    for icon in manifest['icons']:
        assert client.get(icon['src']).status_code == 200


def test_the_installed_app_carries_the_business_name_and_not_the_products(app, client):
    """
    An installed PWA is an icon and a caption on somebody's home screen, next to their
    bank and their WhatsApp. "Receipts" is the caption of an app that could belong to
    anyone - and this one belongs to the business whose drivers and shop staff were
    handed it, which is the only reason they trust it with a photograph of a receipt.

    Three places have to agree, because a name that changes between them reads as a
    different app: the manifest (Android), the apple-mobile-web-app-title meta (iOS),
    and the document title.
    """
    from models.user import db, InstanceConfig

    db.session.add(InstanceConfig(admin_email='admin@example.com', totp_secret='S',
                                  business_name="Another J's Bar & Restaurant"))
    db.session.commit()

    manifest = client.get('/scan/manifest.json').get_json()
    assert manifest['name'] == "Another J's Bar & Restaurant Receipts"
    assert "Another J's Bar & Restaurant" in manifest['description']
    # Cut at a word, because both platforms truncate the caption under an icon at about
    # twelve characters and an ellipsis mid-word is what that looks like otherwise.
    assert manifest['short_name'] == "Another J's"

    page = client.get('/scan/').get_data(as_text=True)
    assert '<title>Another J&#39;s Bar &amp; Restaurant Receipts</title>' in page
    assert 'name="apple-mobile-web-app-title" content="Another J&#39;s"' in page

    # And the identity of the installed app is unchanged by any of it: a rename must
    # not turn one installed app into a second one on the same phone.
    assert manifest['id'] == '/scan/'
    assert manifest['scope'] == '/scan/'


def test_an_instance_with_no_name_yet_still_has_an_installable_app(client):
    """
    Branding is configured after setup, and often not at all. The manifest is fetched
    long before that and must never come back with an empty name - which is an app that
    installs as a blank caption, or does not install.
    """
    manifest = client.get('/scan/manifest.json').get_json()

    assert manifest['name'] == 'Karani Receipts'
    assert manifest['short_name'] == 'Karani'


def test_the_service_worker_is_served_uncacheable(client):
    """
    A worker pinned in an HTTP cache is an app that can never be fixed - the version
    bump inside it is the only upgrade mechanism there is.
    """
    response = client.get('/scan/sw.js')

    assert response.status_code == 200
    assert 'javascript' in response.headers['Content-Type']
    assert 'no-store' in response.headers['Cache-Control']


def test_the_worker_stays_out_of_the_admin_app():
    """
    Scope is the boundary. The dashboard must never be served from a cache, and the
    event stream must never be held open by one.
    """
    source = SERVICE_WORKER.read_text()

    assert "scope: '/scan/'" in (STATIC / 'js' / 'pwa.js').read_text()
    # Navigations outside /scan/ are handed back to the network untouched.
    assert "if (!url.pathname.startsWith('/scan/')) return;" in source
    # And the API is left to the client, which owns the outbox and the retries.
    assert "url.pathname.startsWith('/scan/api/')" in source

    for path in ('/admin', '/stream', '/api/submissions'):
        assert path not in app_shell()


DECODE_HARNESS = r"""
// Loads the vendored bundle and decodes a real QR image through the same entry point
// and the same options object scanner.js uses.
const fs = require('fs'), vm = require('vm'), path = require('path');
const VENDOR = process.argv[2], RAW = process.argv[3], META = process.argv[4];

const sandbox = { console, setTimeout, clearTimeout, fetch, URL, TextDecoder, TextEncoder, performance, crypto };
sandbox.window = sandbox; sandbox.self = sandbox; sandbox.globalThis = sandbox;
vm.createContext(sandbox);
vm.runInContext(fs.readFileSync(path.join(VENDOR, 'zxing-reader.js'), 'utf8'), sandbox);

const Z = sandbox.ZXingWASM;
const scanner = fs.readFileSync(process.argv[5], 'utf8');

// The entry point and options are taken from scanner.js itself, not restated, so this
// fails if the source drifts to a name the bundle does not export.
const entry = scanner.match(/zxing\.(readBarcodes\w*)\(/)[1];
const optionsSrc = scanner.match(/var ZXING_OPTIONS = (\{[\s\S]*?\n    \});/)[1];
const options = vm.runInNewContext('(' + optionsSrc + ')');

(async () => {
    if (typeof Z[entry] !== 'function') {
        console.log(JSON.stringify({ error: 'scanner.js calls ' + entry + ', which the bundle does not export' }));
        return;
    }
    Z.setZXingModuleOverrides({ wasmBinary: fs.readFileSync(path.join(VENDOR, 'zxing_reader.wasm')) });
    await Z.getZXingModule();
    const { width, height } = JSON.parse(fs.readFileSync(META, 'utf8'));
    const data = new Uint8ClampedArray(fs.readFileSync(RAW));
    const results = await Z[entry]({ data, width, height }, options);
    console.log(JSON.stringify({ entry, text: results.length ? results[0].text : null }));
})().catch((e) => console.log(JSON.stringify({ error: e.message })));
"""


@pytest.mark.skipif(not shutil.which('node'), reason='node is not installed')
def test_the_vendored_decoder_actually_decodes_a_receipt_qr(tmp_path):
    """
    Runs the real decoder against a real QR code.

    Every other check here reads source. That was not enough: scanner.js called
    `readBarcodes`, which the bundle does not export - it exports
    `readBarcodesFromImageData` - so every frame threw a TypeError. A failed frame is
    indistinguishable from a frame with no code in it, so the scanner looked alive and
    silently never decoded anything, ever.

    Grepping for the name was what gave false confidence, because the string is a
    substring of the real export. Only calling it proves it.
    """
    qrcode = pytest.importorskip('qrcode')
    from PIL import Image

    expected = 'https://verify.tra.go.tz/58E41A514_092022'
    image = qrcode.make(expected).convert('RGBA').resize((400, 400), Image.NEAREST)

    raw = tmp_path / 'qr.raw'
    raw.write_bytes(image.tobytes())
    meta = tmp_path / 'qr.json'
    meta.write_text(json.dumps({'width': image.width, 'height': image.height}))
    harness = tmp_path / 'decode.js'
    harness.write_text(DECODE_HARNESS)

    result = subprocess.run(
        ['node', str(harness), str(STATIC / 'js' / 'vendor'), str(raw), str(meta),
         str(STATIC / 'js' / 'scanner.js')],
        capture_output=True, text=True, timeout=120,
    )
    assert result.returncode == 0, result.stderr
    payload = json.loads(result.stdout.strip().splitlines()[-1])

    assert 'error' not in payload, payload['error']
    assert payload['text'] == expected


def test_a_decoder_that_cannot_run_is_never_mistaken_for_an_empty_frame():
    """
    The silent failure is the real defect here.

    A decoder throwing on every frame produced exactly the same user experience as a
    camera pointed at a blank wall, which is why it went unnoticed. It has to announce
    itself.
    """
    scanner = (STATIC / 'js' / 'scanner.js').read_text()

    # The engine is validated once, at load, rather than discovered per frame.
    assert "typeof zxing.readBarcodesFromImageData !== 'function'" in scanner
    assert "report('onEngineFailure'" in scanner

    # And the UI acts on it instead of swallowing it.
    page = (TEMPLATES / 'scan' / '_scanner.html').read_text()
    assert 'onEngineFailure' in page
    assert 'engineError' in page


def test_the_viewfinder_shows_what_will_actually_be_captured():
    """
    A QR box drawn over a full-frame photo tells the user to fit their receipt inside a
    crop that is never applied. The two modes capture different things and so must be
    framed differently.
    """
    css = (STATIC / 'css' / 'scan.css').read_text()
    page = (TEMPLATES / 'scan' / '_scanner.html').read_text()

    assert '.photo-frame' in css
    # The small box is bound to QR mode only.
    assert '''class="reticle" x-show="mode === 'qr'"''' in page
    assert '''class="photo-frame" x-show="mode === 'photo'"''' in page
    # The shutter keeps the preview, whole. It briefly cropped to the brackets instead,
    # which is the more confusing way to get this wrong: a receipt that was entirely
    # visible on screen came back with its top line or its total cut off, and nothing on
    # screen said which strip had gone.
    assert 'capture()' in page and 'capture(this.$refs.photoFrame)' not in page
    # So the brackets are pulled out to the edge of the glass rather than sitting a
    # thumb's width inside it and standing for a boundary that is no longer there.
    frame = re.search(r'\.photo-frame \{(.*?)\}', css, re.S)
    assert frame, '.photo-frame has moved; this asserts where its edges are'
    assert '3.5rem' not in frame.group(1) and '8.5rem' not in frame.group(1), \
        'the brackets are inset from the screen again, which is not where the crop is'
    # And the shutter only exists where a manual capture makes sense.
    assert '''class="shutter" x-show="mode === 'photo'"''' in page


def test_the_scanner_and_the_server_agree_on_what_a_receipt_code_looks_like():
    """
    scanner.js rejects a QR code at the camera; utils/tra parses it hours later.

    If the two drift, either the scanner refuses receipts the server would have
    accepted, or it queues rubbish that fails silently long after the person who
    scanned it has walked away.
    """
    from utils.tra import _RECEIPT_URL_RE

    js = (STATIC / 'js' / 'scanner.js').read_text()
    declared = re.search(r'var RECEIPT_URL_RE = /(.+?)/;', js)
    assert declared, 'RECEIPT_URL_RE is no longer a literal in scanner.js'

    # JavaScript requires the forward slash escaped inside a literal; Python does not.
    assert declared.group(1).replace(r'\/', '/') == _RECEIPT_URL_RE.pattern


@pytest.mark.parametrize('text, code', [
    ('https://verify.tra.go.tz/58E41A514_092022', '58E41A514'),
    ('https://verify.tra.go.tz/58E41A514_092022/', '58E41A514'),
    ('58E41A514_092022', '58E41A514'),
])
def test_the_shared_pattern_reads_real_receipt_urls(text, code):
    from utils.tra import _RECEIPT_URL_RE
    assert _RECEIPT_URL_RE.search(text).group(1) == code


@pytest.mark.parametrize('text', [
    'https://example.com/promo',
    'WIFI:S=Cafe;T=WPA;P=hunter2;;',
    'https://verify.tra.go.tz/58E41A514',      # no time component
])
def test_the_shared_pattern_rejects_everything_else(text):
    from utils.tra import _RECEIPT_URL_RE
    assert _RECEIPT_URL_RE.search(text) is None


def test_the_app_shell_is_not_downloaded_before_activation():
    """
    The precache must not compete with the activation request.

    Installing the worker pulls the whole shell - well over a megabyte across a dozen
    requests. On mobile data that saturates the connection, and an unactivated phone
    has exactly one request that matters. It queued behind the precache and hit the
    fetch deadline, which surfaced as "could not reach the server" on a phone with
    perfectly good signal.
    """
    pwa = (STATIC / 'js' / 'pwa.js').read_text()

    # Registration in start() is conditional on already holding a session. Asserted on
    # the shape rather than on one exact line, so reformatting that block does not read
    # as the guard having been removed.
    start_body = pwa.split('function start()')[1].split('global.addEventListener')[0]
    guard = start_body.split('if (sessionToken()) {')[1]
    assert 'registerServiceWorker()' in guard
    # ...and activate() is what installs it, once there is something to install it for.
    activate_body = pwa.split('async function activate(token)')[1].split('\n    }')[0]
    assert 'registerServiceWorker()' in activate_body

    # The activation page must not pre-empt that.
    activate_html = (ROOT / 'templates' / 'scan' / 'activate.html').read_text()
    assert 'PWA.registerServiceWorker()' not in activate_html


def test_activation_gets_a_longer_deadline_than_an_interactive_read():
    """
    Activation is one-shot, happens on a cold cache, and is the only thing between a
    field user and a working app. The 8s budget meant for a re-triable read is not it.
    """
    pwa = (STATIC / 'js' / 'pwa.js').read_text()

    net = int(re.search(r'var NET_TIMEOUT_MS = (\d+)', pwa).group(1))
    activate = int(re.search(r'var ACTIVATE_TIMEOUT_MS = (\d+)', pwa).group(1))

    assert activate > net
    assert 'timeout: ACTIVATE_TIMEOUT_MS' in pwa


def test_activation_never_throws_so_failures_cannot_be_mislabelled():
    """
    Every failure comes back in the return value.

    A blanket try/catch around this is what turned a timeout into "check your
    connection" - advice for a problem the user did not have, and undiagnosable
    because a storage error and a dead network produced the same sentence.
    """
    pwa = (STATIC / 'js' / 'pwa.js').read_text()
    activate_body = pwa.split('async function activate(token)')[1].split('\n    }')[0]
    assert "return { ok: false, reason: e.kind || 'network', error: e.message };" in activate_body

    for template in ('activate.html', '_scanner.html'):
        source = (TEMPLATES / 'scan' / template).read_text()
        assert 'Could not reach the server. Check your connection' not in source
        assert 'PWA.activate' in source


def test_a_timeout_is_reported_as_a_timeout():
    pwa = (STATIC / 'js' / 'pwa.js').read_text()

    assert "throw netError('timeout'" in pwa
    assert "throw netError('network'" in pwa
    # The two must not be collapsed back into one message.
    assert 'took too long' in pwa


def test_a_spent_token_is_never_reported_as_a_failure():
    """
    Once the server has answered, the token is gone.

    Anything that fails after that - IndexedDB unavailable, storage denied - must not
    surface as failure, or the user retries a link that is legitimately dead and needs
    an admin to issue another. The session is already in localStorage by then.
    """
    pwa = (STATIC / 'js' / 'pwa.js').read_text()
    activate_body = pwa.split('async function activate(token)')[1].split('\n    }')[0]
    after_save = activate_body[activate_body.index('saveSession('):]

    # Nothing at all is awaited past that point. Anything awaited here is something
    # that can reject - or, worse, never settle - between a successful activation and
    # the caller being told about it.
    assert 'await ' not in after_save
    assert 'return { ok: true' in after_save


def test_opening_the_database_can_never_hang_forever():
    """
    indexedDB.open() is not reliably a promise that settles.

    WebKit can fire no event at all - not success, not error, not blocked - usually on
    the first access after a page load. An `await` on that never returns and never
    throws, so try/catch is no protection; the caller just stops. That is what left
    activation on "Activating…" indefinitely, with the token already spent.
    """
    pwa = (STATIC / 'js' / 'pwa.js').read_text()
    opener = pwa.split('function openWithDeadline(version)')[1].split('\n    /*')[0]

    assert 'DB_OPEN_TIMEOUT_MS' in opener        # raced against a deadline
    assert 'blocked:' in opener                  # the other silent-stall path

    # Every open goes through that deadline. A bare idb.openDB() anywhere else in the
    # file is the hang coming back by another door.
    assert pwa.count('idb.openDB(') == 1

    # A failure backs off briefly rather than latching for the life of the page: the
    # stalls this guards against are transient, and the old permanent latch turned one
    # unlucky open into a phone that could not save a receipt again until it was
    # reloaded - with the only cure buried on the diagnostics screen.
    getdb = pwa.split('function getDB()')[1].split('\n    /* Lets the diagnostics')[0]
    assert 'dbRetryAfter = Date.now() + DB_RETRY_AFTER_MS' in getdb


def test_a_database_missing_its_stores_is_rebuilt_rather_than_used():
    """
    An open at the current version does not run `upgrade`, so a database that exists at
    that version with no object stores in it opens perfectly happily and then fails
    every read and write with NotFoundError - for good, because nothing will ever
    trigger the upgrade that would fix it.

    Phones reached that state: the service worker used to create this database with no
    upgrade callback (see openOutboxDB), and whichever of the two got there first after
    an eviction decided the schema. Bumping the version is the only thing that runs an
    upgrade, so getDB has to notice and do exactly that.
    """
    pwa = (STATIC / 'js' / 'pwa.js').read_text()
    worker = (STATIC / 'js' / 'service-worker.js').read_text()

    repair = pwa.split('async function repairSchema(db)')[1].split('\n    /*')[0]
    assert 'db.version + 1' in repair
    assert 'openWithDeadline(version)' in repair

    getdb = pwa.split('function getDB()')[1].split('\n    /* Lets the diagnostics')[0]
    assert 'missingStores(db).length ? repairSchema(db)' in getdb

    # The worker must never be the thing that decides the schema. No version pinned, so
    # it can only ever attach to what the page built...
    assert 'idb.openDB(OUTBOX_DB_NAME, undefined, {' in worker
    # ...and it creates the real stores in the one case where it does create the
    # database, rather than an empty shell that strands the app forever.
    for store in ('outbox', 'submissions', 'meta'):
        assert f"createObjectStore('{store}'" in worker
    # A store-less database is handed back for the page to repair, not used.
    assert "throw new Error('schema-incomplete')" in worker


def test_the_scan_screen_never_waits_on_storage_or_the_network_to_show_a_viewfinder():
    """
    The camera is the screen. Awaiting a database open (up to DB_OPEN_TIMEOUT_MS) and
    then an activation POST (up to ACTIVATE_TIMEOUT_MS, 45s) before so much as
    constructing the scanner meant a launch from a home-screen icon carrying a token
    could sit on a black screen for most of a minute - while the camera was already
    open and streaming, started by the head script, with nobody collecting it.
    """
    scanner = (TEMPLATES / 'scan' / '_scanner.html').read_text()
    boot = scanner.split('async boot() {')[1].split('\n        },')[0]

    # Registering with the router is what opens the camera: it calls back immediately
    # with this view's state, and setActive() starts it. Nothing may be awaited first.
    started = boot.index("ScanRouter.watch('scan'")
    assert 'await ' not in boot[:started]
    # Storage and activation are awaited only after the camera is on its way.
    assert boot.index('await this.refresh()') > started
    assert boot.index('await this.claimTokenFromUrl()') > started

    set_active = scanner.split('setActive(active) {')[1].split('\n        },')[0]
    assert 'this.startCamera()' in set_active


def test_activation_does_not_wait_on_local_storage():
    """
    Being activated means holding a session, and the session is in localStorage.

    Awaiting IndexedDB after the server has already answered put a database open on the
    critical path of the one operation that must not stall - and left the token spent
    with the UI still saying "Activating…".
    """
    pwa = (STATIC / 'js' / 'pwa.js').read_text()
    activate_body = pwa.split('async function activate(token)')[1].split('\n    }')[0]

    saved = activate_body.index('saveSession(')
    assert 'await metaSet' not in activate_body[saved:]
    assert "metaSet('signOutReason', null).catch(" in activate_body


def test_feature_detection_does_not_trust_the_in_operator():
    """
    `'serviceWorker' in navigator` is true while `navigator.serviceWorker` is undefined
    in Firefox private browsing and in any insecure context. The feature check passes
    and the call after it throws synchronously - right after a successful activation,
    which strands the caller with the token already spent.
    """
    # Comments discuss the broken idiom by name, so only code is inspected.
    code = '\n'.join(
        line for line in (STATIC / 'js' / 'pwa.js').read_text().splitlines()
        if not line.lstrip().startswith(('*', '//', '/*'))
    )

    assert "'serviceWorker' in" not in code
    assert 'if (!global.navigator.serviceWorker)' in code


def test_nothing_after_a_successful_activation_can_reach_the_caller():
    """The token cannot be spent twice, so no later failure may look like failure."""
    pwa = (STATIC / 'js' / 'pwa.js').read_text()
    activate_body = pwa.split('async function activate(token)')[1].split('\n    }')[0]
    after_save = activate_body[activate_body.index('saveSession('):]

    assert after_save.index('try {') < after_save.index('registerServiceWorker()')
    assert 'catch (e)' in after_save


def test_a_storage_failure_does_not_break_the_screens_that_report_it():
    """status() feeds every refresh, so it has to degrade rather than throw."""
    pwa = (STATIC / 'js' / 'pwa.js').read_text()
    status_body = pwa.split('async function status()')[1].split('\n    }')[0]

    assert 'storageOk' in status_body
    assert 'catch' in status_body

    diagnostics = (TEMPLATES / 'scan' / '_diagnostics.html').read_text()
    assert 'status.storageOk' in diagnostics
    assert 'PWA.resetStorage()' in diagnostics


def test_adding_to_the_home_screen_carries_the_activation_token(client, app):
    """
    The iOS flow only works if the install carries the token.

    A home-screen app on iOS does not share storage with Safari, so activating in the
    browser spends the single-use token in a jar the installed app cannot read - and
    the app opens signed out with a dead link. Putting the token in start_url means it
    is spent once, in the storage the app actually runs in.
    """
    from models.user import db, Device
    from utils.device_auth import issue_enrolment_token

    device = Device(name='Phone')
    db.session.add(device)
    db.session.flush()
    token = issue_enrolment_token(device)
    db.session.commit()

    manifest = client.get(f'/scan/manifest.json?t={token}').get_json()
    assert manifest['start_url'] == f'/scan/?t={token}'
    # Same installed app regardless of which start_url it was installed from.
    assert manifest['id'] == '/scan/'
    assert manifest['scope'] == '/scan/'
    # start_url must sit inside scope or the browser refuses to install.
    assert manifest['start_url'].startswith(manifest['scope'])

    # And the activation page points the install at exactly that manifest.
    page = client.get(f'/scan/a/{token}').get_data(as_text=True)
    assert f'/scan/manifest.json?t={token}' in page

    # The plain manifest is unaffected, so an already-installed app is not disturbed.
    assert client.get('/scan/manifest.json').get_json()['start_url'] == '/scan/'


def test_the_scanner_claims_a_token_the_launcher_carries(client):
    """
    start_url is fixed at install time, so every launch replays the same token. A
    session must win over it, and a spent token must not surface as an error - it is
    just an old shortcut, not a fault.
    """
    body = client.get('/scan/').get_data(as_text=True)

    assert 'claimTokenFromUrl' in body
    claim = body.split('async claimTokenFromUrl()')[1].split('async refresh()')[0]
    assert 'if (!this.session)' in claim
    assert "result.reason !== 'unknown'" in claim
    # The credential does not stay in the address bar.
    assert 'history.replaceState' in claim


def test_relaunching_an_installed_app_never_re_activates(client):
    """
    iOS bookmarks the *current page URL* on Add to Home Screen; it does not follow the
    manifest's start_url. Since the instructions are to install from the activation
    page, the home-screen shortcut points there permanently - so every launch arrives
    holding a token that was spent on the very first one.

    Unguarded, that reactivated on every launch, failed because the token is single
    use, and told the user "This device is not recognised" while their session sat
    perfectly valid in localStorage. It presented as losing the session on every close.
    """
    activate_html = (ROOT / 'templates' / 'scan' / 'activate.html').read_text()

    head = activate_html.split('{% block head %}')[1].split('{% endblock %}')[0]
    # The check runs before anything can paint, so a relaunch shows no activation UI.
    assert 'localStorage.getItem' in head
    assert 'location.replace' in head
    # It must precede the body, or the activation screen flashes up on every launch.
    assert activate_html.index('{% block head %}') < activate_html.index('{% block body %}')

    # And again in the component, for the case where the inline script did not run.
    detect = activate_html.split('detect() {')[1].split('\n        },')[0]
    assert 'PWA.sessionToken()' in detect
    assert detect.index('PWA.sessionToken()') < detect.index('if (standalone)')


def test_the_inline_guard_reads_the_key_pwa_actually_writes():
    """
    The <head> guard runs before pwa.js loads, so it hardcodes the storage key. If the
    two ever drift, the guard silently never fires and the relaunch bug comes back
    exactly as before.
    """
    pwa = (STATIC / 'js' / 'pwa.js').read_text()
    key = re.search(r"var SESSION_KEY = '([^']+)'", pwa).group(1)

    activate_html = (ROOT / 'templates' / 'scan' / 'activate.html').read_text()
    assert f"getItem('{key}')" in activate_html


def test_a_used_link_says_so_instead_of_blaming_the_phone(client):
    """
    "Not recognised" reads as a fault with the device. A spent link is not a fault at
    all, and retrying it can never work - so the offer is the one thing that can.
    """
    activate_html = (ROOT / 'templates' / 'scan' / 'activate.html').read_text()

    assert 'already been used' in activate_html
    assert 'Scan a new activation code' in activate_html
    # The dead retry button is not offered in that state.
    assert 'x-if="!spent"' in activate_html


def test_a_long_wait_is_narrated_rather_than_frozen():
    """
    Activation is allowed a long deadline because the connection may be poor. A label
    that says "Activating…" and never changes is indistinguishable from a hung app.
    """
    activate_html = (ROOT / 'templates' / 'scan' / 'activate.html').read_text()

    assert 'startTicker' in activate_html
    assert 'Slow connection' in activate_html
    assert 'Still trying' in activate_html


def test_the_pwa_shells_are_reachable_without_a_session(client):
    """
    Deliberately public.

    The worker has to be able to precache these and hand them to a phone that is
    offline, or offline and signed out. They carry no data - everything behind them is
    on /scan/api/*, which is guarded.
    """
    for path in ('/scan/', '/scan/history', '/scan/diagnostics'):
        assert client.get(path).status_code == 200


def test_the_scanner_page_opens_the_camera_before_anything_else(client):
    """
    The permission prompt and sensor warm-up dominate a cold launch, so they are
    started from an inline script in <head>. Moving that below Alpine would add most
    of a second to every launch without any visible symptom in review.
    """
    body = client.get('/scan/').get_data(as_text=True)

    head = body.split('</head>')[0]
    assert 'window.__cameraPromise' in head
    # Compared against the actual tags, not the text: the inline block mentions both
    # scanner.js and Alpine in its own comments.
    assert head.index('window.__cameraPromise') < head.index('src="/static/js/scanner.js"')
    assert 'alpine.min.js' not in head


def test_decoding_happens_off_the_main_thread():
    """
    On a phone with no native BarcodeDetector - every iPhone, and any Android whose
    Play Services barcode module is absent - ZXing is the only decoder and it runs on
    every frame, with the most expensive options this app has, ten times a second.

    On the main thread that is not jank, it is the whole thread. The part that broke
    receipts rather than merely feeling slow: IndexedDB delivers its completion events
    there too, so writes that finished in microseconds could not report it, and the
    app's own 5-second deadline on a local save fired - "Saving to this phone is
    taking too long" - while storage was in fact perfectly healthy.
    """
    scanner = (STATIC / 'js' / 'scanner.js').read_text()
    worker = (STATIC / 'js' / 'decoder-worker.js').read_text()

    assert '/static/js/decoder-worker.js' in app_shell()
    assert "importScripts('/static/js/vendor/zxing-reader.js')" in worker
    assert 'readBarcodesFromImageData' in worker

    decode = scanner.split('async function decodeWithZXing(canvas, options)')[1].split('\n    /*')[0]
    assert 'askWorker(' in decode
    # Transferred, not structured-cloned: a 1920x1080 frame is an eight-megabyte
    # buffer, and copying one per frame is its own performance problem.
    assert '[image.data.buffer]' in decode
    # Slow beats broken: no worker, or a worker that died, still decodes here.
    assert 'await getZXing()' in decode


def test_a_camera_the_platform_took_back_is_reopened():
    """
    A call, the lock screen, or another app wanting the camera ends the video track,
    and nothing tells the page: the viewfinder freezes on its last frame with every
    control still enabled and no error anywhere. Scanning then silently does nothing.

    That is the state that teaches a field user to reload, which on WebKit means
    answering the camera permission prompt again - the "lots of clicks" this app is
    supposed to be free of.
    """
    scanner = (STATIC / 'js' / 'scanner.js').read_text()
    page = (TEMPLATES / 'scan' / '_scanner.html').read_text()

    # The cached pre-warmed stream is only reused while it is actually live.
    acquire = scanner.split('async function acquireStream()')[1].split('\n    async function')[0]
    assert 'streamIsLive(existing)' in acquire

    assert "t.readyState === 'live'" in scanner
    assert 'async revive()' in scanner
    assert "document.addEventListener('visibilitychange'" in page
    assert 'this.reviveCamera()' in page


def test_the_diagnostics_screen_can_actually_pass_its_own_qr_check():
    """
    diagnostics.html loaded scanner.js but never the ZXing bundle it needs, so
    Scanner.getZXing() had no global to find and the check reported "Nothing on this
    phone can read a QR code" on every device without a native detector - while the
    scan screen next door decoded receipts perfectly well.

    A check that cannot pass is worse than no check: it sends a working phone to an
    admin as a broken one, and buries the real failure next to it.
    """
    page = (TEMPLATES / 'scan' / '_diagnostics.html').read_text()
    head = (TEMPLATES / 'scan' / 'shell.html').read_text().split('{% block head %}')[1]

    # One document now, so the bundle is loaded once for all three views and the
    # diagnostics screen cannot be the one that was left without it.
    assert 'zxing-reader.js' in head
    assert 'scanner.js' in head
    # Tests the path the scan screen actually decodes on, which is the worker.
    assert 'Scanner.warmDecoder()' in page


def test_storage_failing_is_never_reported_as_everything_sent():
    """
    status() used to leave `pending` at its initial 0 when the outbox could not be
    read, so every screen rendered a storage failure as "Nothing waiting. Everything
    has been sent." - the most reassuring sentence the app owns, shown at the one
    moment it cannot account for a single receipt.
    """
    pwa = (STATIC / 'js' / 'pwa.js').read_text()
    body = pwa.split('async function status()')[1].split('\n    }')[0]
    assert 'var pending = null;' in body

    diagnostics = (TEMPLATES / 'scan' / '_diagnostics.html').read_text()
    assert 'status.pending === null' in diagnostics

    # And the pill on both live screens says so rather than claiming to be synced.
    for name in ('_scanner.html', '_history.html'):
        page = (TEMPLATES / 'scan' / name).read_text()
        label = page.split('get syncLabel()')[1].split('},')[0]
        assert "if (!this.storageOk) return 'Storage problem';" in label


def test_the_three_scan_urls_serve_one_identical_document(client):
    """
    The field app is one page. Scan, History and Diagnostics share a document so that
    moving between them cannot destroy the camera - on WebKit a getUserMedia grant
    belongs to the document that asked, so every navigation back to the scanner used to
    cost a permission prompt, forever, for anyone who checked their history.

    Serving anything route-specific breaks two things at once: the service worker
    precaches exactly one document and answers every /scan/ navigation with it, and the
    router picks the view from the URL on the client. A per-route difference would show
    up as the wrong screen after an offline deep link, which is the hardest possible
    place to notice it.
    """
    bodies = {path: client.get(path).get_data(as_text=True)
              for path in ('/scan/', '/scan/history', '/scan/diagnostics')}

    for path, body in bodies.items():
        assert bodies['/scan/'] == body, f'{path} does not render the same shell as /scan/'

    # All three views really are present in that one document.
    assert 'scannerApp()' in bodies['/scan/']
    assert 'historyPage()' in bodies['/scan/']
    assert 'diagnosticsPage()' in bodies['/scan/']


def test_moving_between_views_never_reloads_the_page(client):
    """
    A cross-view <a> keeps its href - it is a real route, so middle-click, "open in new
    tab" and a no-JS load all still work - but an in-app tap must be intercepted. One
    link left unhandled is a full navigation, which throws away the document, the
    camera, the compiled ZXing module and the permission with it.
    """
    body = client.get('/scan/').get_data(as_text=True)

    links = re.findall(r'<a href="(/scan/[^"]*)"([^>]*)>', body)
    assert links, 'no cross-view links found; this test is no longer testing anything'

    for href, attrs in links:
        assert '@click.prevent="ScanRouter.go(' in attrs, f'{href} would reload the page'


def test_each_view_boots_only_when_it_is_opened():
    """
    Three components now live in one document, and two of them are expensive: History
    walks IndexedDB and installs an IntersectionObserver that immediately starts paging
    more history, Diagnostics compiles the ZXing WASM, refetches the worker source and
    walks the whole precache.

    On separate pages, navigating there was the gate. In one document the router is the
    only gate, and without it every one of those costs is paid on the cold launch of a
    person who opened the app to scan a receipt.
    """
    for name, component in (('_history.html', 'historyPage'), ('_diagnostics.html', 'diagnosticsPage')):
        page = (TEMPLATES / 'scan' / name).read_text()
        assert 'x-init="init()"' in page, f'{name} still boots from x-init'
        assert "ScanRouter.watch(" in page

    history = (TEMPLATES / 'scan' / '_history.html').read_text()
    set_active = history.split('async setActive(active) {')[1].split('\n        },')[0]
    assert 'if (!active) return;' in set_active
    # And the first-run work happens once, not on every visit back.
    assert 'if (!this.booted)' in set_active

    # Every view is hidden until Alpine has hydrated. Without this the cold launch
    # lays all three out at once - a light history sheet stacked under the camera.
    for name in ('_scanner.html', '_history.html', '_diagnostics.html'):
        root = (TEMPLATES / 'scan' / name).read_text().split('<div', 1)[1].split('>', 1)[0]
        assert 'x-cloak' in root, f'{name} flashes before Alpine boots'
        assert 'x-show="active"' in root


def test_starting_the_pwa_twice_does_not_double_its_timers():
    """
    PWA.start() was called once per page when there were three pages. Two components in
    the shared document call it now, so without a guard a user who opened History would
    run two sync intervals and two sets of online/visibilitychange/pagehide listeners -
    duplicate requests to the server for the rest of the session, from every device.
    """
    pwa = (STATIC / 'js' / 'pwa.js').read_text()
    body = pwa.split('function start() {')[1].split('\n    }')[0]

    assert 'if (started) return;' in body
    assert 'started = true;' in body


# --- The live channel ---------------------------------------------------------
#
# The scanner reads the event stream by hand, with fetch() and a stream reader, because
# device sessions live in an Authorization header and EventSource cannot send one. That
# hand-written parser is the single point where the whole push channel can fail
# silently: a frame it does not understand is not an error, it is just an update that
# never arrives - and the screen then looks exactly like a quiet afternoon.

STREAM_HARNESS = """
const fs = require('fs');
// pwa.js is an IIFE over `window`. Nothing at load time touches the DOM, so a shim
// with the handful of properties it reads is enough to get at its exports.
global.window = {
    navigator: { onLine: true },
    document: { addEventListener() {}, visibilityState: 'visible' },
    addEventListener() {},
    localStorage: { getItem() { return null; }, setItem() {}, removeItem() {} },
    setTimeout: setTimeout,
};
global.indexedDB = undefined;
eval(fs.readFileSync(process.argv[2], 'utf8'));

const parse = global.window.PWA.parseEventFrame;
console.log(JSON.stringify({
    withId: parse('id: 42\\ndata: {"event_type":"submission.processed"}'),
    idLast: parse('data: {"event_type":"submission.failed"}\\nid: 7'),
    ping: parse('event: ping\\ndata: {}'),
    retry: parse('retry: 3000'),
    comment: parse(': keep-alive'),
    blank: parse(''),
    multiline: parse('id: 9\\ndata: {"a":1,\\ndata: "b":2}'),
}));
"""


@pytest.mark.skipif(not shutil.which('node'), reason='node is not installed')
def test_the_scanner_reads_every_field_of_a_frame(tmp_path):
    """
    Parsed, not grepped for.

    The parser used to require `data: ` to be the very first thing in a frame. Adding
    the `id:` line that makes reconnection possible would therefore have turned every
    single event into a frame the phone silently ignored - the app would have looked
    completely normal and never updated again.
    """
    harness = tmp_path / 'frames.js'
    harness.write_text(STREAM_HARNESS)

    result = subprocess.run(['node', str(harness), str(STATIC / 'js' / 'pwa.js')],
                            capture_output=True, text=True, timeout=60)
    assert result.returncode == 0, result.stderr
    frames = json.loads(result.stdout)

    # The id is what a reconnect resumes from, wherever it sits in the frame.
    assert frames['withId']['id'] == '42'
    assert json.loads(frames['withId']['data'])['event_type'] == 'submission.processed'
    assert frames['idLast']['id'] == '7'

    # The heartbeat is recognised as itself, so it can prove the connection is alive
    # without being mistaken for an update.
    assert frames['ping']['event'] == 'ping'

    # Nothing to apply, and nothing that should look like an event.
    assert frames['retry'] is None
    assert frames['comment'] is None
    assert frames['blank'] is None

    # SSE allows a payload to be split across several data lines.
    assert json.loads(frames['multiline']['data']) == {'a': 1, 'b': 2}


def test_the_scanner_reconnects_with_a_cursor_rather_than_from_now():
    """
    Without ?since=, a reconnecting phone is started at the present and everything that
    happened while it was in a pocket is lost - which is the entire failure this
    channel was rebuilt to fix.
    """
    pwa = (STATIC / 'js' / 'pwa.js').read_text()

    assert "'/scan/api/stream' + (lastEventId ? '?since=' + encodeURIComponent(lastEventId) : '')" in pwa
    assert 'if (frame.id) lastEventId = frame.id;' in pwa


def test_a_stream_that_goes_silent_is_treated_as_dead():
    """
    The failure no client can otherwise see: the connection is open, the reader is
    waiting, and nothing is ever going to arrive. Only the absence of the server's
    heartbeat gives it away.
    """
    pwa = (STATIC / 'js' / 'pwa.js').read_text()
    watchdog = pwa.split('function armStreamWatchdog')[1].split('\n    }')[0]

    assert 'STREAM_SILENCE_MS' in watchdog
    assert '.abort()' in watchdog
    # Re-armed by every frame, heartbeats included, or it fires mid-conversation.
    assert pwa.count('armStreamWatchdog(generation)') >= 2


# --- The numbers above the list -----------------------------------------------

def history_component_source():
    """The history screen's own script, as the browser receives it."""
    source = (TEMPLATES / 'scan' / '_history.html').read_text()
    return source.rsplit('<script>', 1)[1].rsplit('</script>', 1)[0]


HERO_DRIVER = """
const page = historyPage();
const populated = {
    month: { receipts: 12, spend_cents: 124050000, vat_cents: 18900000 },
    today: { receipts: 3, spend_cents: 4500000 },
    month_label: 'July', in_flight: 0, needs_attention: 0,
};

function snapshot() {
    return {
        total: page.heroTotal,
        label: page.heroLabel,
        today: page.todayLine,
        stats: page.heroStats.map((s) => [s.label, s.value, s.tone]),
    };
}

const out = {};
page.storageOk = true; page.online = true; page.pending = 0;

page.summary = null;
out.unknown = snapshot();

page.summary = populated;
out.clear = snapshot();

page.pending = 4;
out.unsent = snapshot();

page.pending = null;
out.storageUnknown = snapshot();

page.pending = 0;
page.summary = Object.assign({}, populated, { needs_attention: 2, in_flight: 5 });
out.failing = snapshot();

console.log(JSON.stringify(out));
"""


@pytest.mark.skipif(not shutil.which('node'), reason='node is not installed')
def test_the_dashboard_numbers_say_unknown_rather_than_zero(tmp_path):
    """
    The figures at the top of the field app, evaluated.

    The rule they all follow: not knowing must never render as zero. "TZS 0 captured
    this month" on a phone that simply has not reached the server yet is not a smaller
    truth than the real figure, it is a different and alarming one - and it is exactly
    what a screen shows when null is passed to a formatter without thinking.
    """
    bundle = tmp_path / 'hero.js'
    bundle.write_text(
        "global.PWA = { MAX_ATTEMPTS: 8 };\n"
        "global.navigator = { onLine: true };\n"
        + history_component_source() + HERO_DRIVER
    )

    result = subprocess.run(['node', str(bundle)], capture_output=True, text=True, timeout=30)
    assert result.returncode == 0, result.stderr
    state = json.loads(result.stdout)

    # Nothing known yet.
    assert state['unknown']['total'] == '—'
    assert [value for _, value, _ in state['unknown']['stats'][:2]] == ['—', '—']

    # Known: shortened, because a hero figure that wraps is no longer a glance.
    assert state['clear']['total'] == '1.24M'
    assert state['clear']['label'] == 'Captured · July'
    assert '3 today' in state['clear']['today']
    assert state['clear']['stats'][0] == ['Receipts', '12', '']
    assert state['clear']['stats'][1] == ['VAT', '189K', '']
    # Nothing outstanding is itself worth saying.
    assert state['clear']['stats'][2] == ['All sent', '✓', '']

    # Receipts still on the phone outrank work in progress...
    assert state['unsent']['stats'][2] == ['Sending', '4', 'is-busy']
    # ...an outbox that cannot even be counted outranks that...
    assert state['storageUnknown']['stats'][2] == ['Unsent', '?', 'is-alert']
    # ...and a receipt that failed outright outranks everything.
    assert state['failing']['stats'][2] == ['Need you', '2', 'is-alert']


# --- The receipt that vanished ------------------------------------------------
#
# The worst bug this screen has had, because of what it looked like from outside: a
# receipt was scanned, sat visibly in "on this phone", and then - the moment the server
# accepted it, which is the moment it was safest it had ever been - disappeared. It came
# back on a manual refresh or a relaunch, so nothing was ever actually lost. Nobody
# holding the phone had any way to know that.
#
# It had two halves, and both are pinned here.

REFRESH_DRIVER = """
const cache = [];
global.PWA = {
    MAX_ATTEMPTS: 8,
    status: async () => ({
        pending: 0, syncing: false, online: true, live: true, summary: null,
        session: { device: { name: 'Test Phone' } }, recentEvents: [], storageOk: true,
    }),
    outboxAll: async () => [],
    // The same contract as the real one: newest first, `beforeId` exclusive.
    cachedHistory: async (options = {}) => {
        let rows = cache.slice().sort((a, b) => b.id - a.id);
        if (options.beforeId != null) rows = rows.filter((r) => r.id < options.beforeId);
        return options.limit ? rows.slice(0, options.limit) : rows;
    },
};

const row = (id, status) => ({
    id, status, captured_at: '2026-07-29T17:4' + (id % 10) + ':00', receipt_code: 'CODE' + id,
});

(async () => {
    const out = {};
    const page = historyPage();

    // On screen: one receipt from earlier. In the cache: that one, plus the one just
    // scanned, which the server has taken and given the next id to.
    page.submissions = [row(41, 'completed')];
    cache.push(row(41, 'completed'), row(42, 'queued'));
    await page.refresh();
    out.afterScan = page.submissions.map((s) => s.id);

    // A status that moved while the app was open still lands, which is what refresh()
    // was doing right before and must go on doing.
    cache.find((r) => r.id === 42).status = 'completed';
    await page.refresh();
    out.afterVerdict = page.submissions.map((s) => s.status);

    // A search is its own list. Merging the newest page into it would put receipts on
    // screen that do not match what was typed.
    page.query = 'maina';
    page.submissions = [row(7, 'completed')];
    await page.refresh();
    out.duringSearch = page.submissions.map((s) => s.id);

    console.log(JSON.stringify(out));
})();
"""


@pytest.mark.skipif(not shutil.which('node'), reason='node is not installed')
def test_a_receipt_that_was_just_sent_appears_without_being_asked_for(tmp_path):
    """
    refresh() must re-read the newest receipts, not the ones already on screen.

    It used to ask the cache for `beforeId: submissions[0].id + 1` - a window pinned
    under the top of the list, which by construction cannot contain a receipt newer than
    the top of the list. Every freshly sent receipt is exactly that. So the row left the
    outbox when the server took it and had nowhere to arrive: it was in the cache, it
    was in IndexedDB, and it was not on screen until the refresh button or a relaunch
    re-read the first page from the top.
    """
    bundle = tmp_path / 'refresh.js'
    bundle.write_text(
        "global.navigator = { onLine: true };\n"
        + history_component_source() + REFRESH_DRIVER
    )

    result = subprocess.run(['node', str(bundle)], capture_output=True, text=True, timeout=30)
    assert result.returncode == 0, result.stderr
    state = json.loads(result.stdout)

    assert state['afterScan'] == [42, 41], 'the receipt just sent is still not on screen'
    assert state['afterVerdict'] == ['completed', 'completed']
    assert state['duringSearch'] == [7], 'a refresh dropped non-matching rows into a search'


def test_an_accepted_receipt_reaches_history_before_it_leaves_the_outbox():
    """
    The other half. Even re-reading from the top only helps once something has written
    the sent receipt into the cache, and the only thing that did was the next pull from
    /scan/api/submissions - a round trip, on connections where a round trip is not a
    given. Between discard() and that reply the receipt was in neither list.

    The server already answers the sync call with the submission id it created, so the
    phone can record the row itself, immediately, and let the pull fill in the rest.
    """
    pwa = (STATIC / 'js' / 'pwa.js').read_text()

    for name in ('syncUrlBatch', 'syncPhoto'):
        body = pwa.split('async function %s' % name)[1].split('\n    }')[0]
        assert 'cacheAccepted' in body, f'{name} drops the outbox row with nothing to replace it'
        # rindex: the discard that matters is the one after a successful send. syncPhoto
        # has an earlier one for an entry whose blob is gone, which has nothing to cache.
        assert body.index('cacheAccepted') < body.rindex('await discard('), \
            f'{name} discards before the receipt is anywhere else'

    written = pwa.split('async function cacheAccepted')[1].split('\n    }')[0]
    # Keyed on the server's own id, so the pull that follows replaces this row rather
    # than adding a second one next to it.
    assert 'id: result.submission_id' in written
    # And it never overwrites what the server has already told us.
    assert 'if (existing) return;' in written


ROUTER_DRIVER = """
const listeners = {};
global.window = {
    addEventListener: (name, fn) => { listeners[name] = fn; },
    history: { replaceState() {}, pushState() {} },
    location: { pathname: '/scan/history', href: '/scan/history' },
    document: {
        title: '',
        body: { classList: { add() {}, remove() {} }, scrollTop: 0 },
        documentElement: { scrollTop: 0 },
    },
    scrollTo() { /* the no-op this file exists to work around */ },
    requestAnimationFrame: (fn) => fn(),
};
global.window.window = global.window;
require(process.argv[2]);

const R = window.ScanRouter;
const body = window.document.body;
R.watch('history', () => {});
R.watch('scan', () => {});
R.start();

const out = {};
body.scrollTop = 640;              // deep into a year of receipts
R.go('scan');
out.openedScanner = body.scrollTop;
R.go('history');
out.backOnHistory = body.scrollTop;
R.go('diagnostics');
out.openedDiagnostics = body.scrollTop;
listeners.popstate({ state: { view: 'history' } });
out.wentBack = body.scrollTop;
console.log(JSON.stringify(out));
"""


@pytest.mark.skipif(not shutil.which('node'), reason='node is not installed')
def test_switching_views_lands_where_the_user_left_each_one(tmp_path):
    """
    Three views, one document, one scroller - so a scroll position belongs to a view,
    not to the app, and the router is the only thing that knows which is which.

    It was calling window.scrollTo(0, 0), which does nothing here: scan.css gives html
    and body both a height and their own overflow, which makes the body the scroll
    container and window the wrong object to ask. So Diagnostics opened halfway down
    wherever History had been left, and History came back to the top of a list someone
    was reading the middle of.
    """
    bundle = tmp_path / 'router-driver.js'
    bundle.write_text(ROUTER_DRIVER)

    result = subprocess.run(
        ['node', str(bundle), str(STATIC / 'js' / 'router.js')],
        capture_output=True, text=True, timeout=30,
    )
    assert result.returncode == 0, result.stderr
    state = json.loads(result.stdout)

    # A view being opened for the first time starts at the top of itself.
    assert state['openedScanner'] == 0
    assert state['openedDiagnostics'] == 0
    # And one being returned to picks up where it was - by the tap, and by Back.
    assert state['backOnHistory'] == 640
    assert state['wentBack'] == 640


def test_a_photo_the_phone_could_not_decode_is_uploaded_bigger_than_one_it_could():
    """
    The two capture paths must not flatten this back to a single size.

    A receipt reached the server at 720x1280 with its QR code about sixty pixels across
    - under two pixels a module - and the server's decoder ran every pass it has over it
    and found nothing, because there was nothing left to find. Whether the code survives
    is settled on the phone, by the two numbers below, long before utils/qr.py gets a
    look at it: what the camera was asked for, and how much of the result is kept.

    Asserted on the source because there is no way to prove it otherwise without a
    camera. Both `capture()` and `readImageFile()` must ask for the larger size, and
    must ask for it only when their own decode came back empty - a photo whose code this
    phone already read is evidence filed beside a verified receipt, and does not need
    the bytes.
    """
    scanner = (STATIC / 'js' / 'scanner.js').read_text()

    undecoded = int(re.search(r'var UNDECODED_MAX_EDGE = (\d+);', scanner).group(1))
    default = int(re.search(r'maxEdge = maxEdge \|\| (\d+);', scanner).group(1))
    assert undecoded > default, 'an unread code is the one case that needs the pixels'

    # Two different canvases by name: the shutter uploads the frame cropped to what the
    # viewfinder was showing, the gallery import uploads the file as it was given. The
    # rule is the same for both and is what this asserts.
    conditional = re.findall(
        r'toJpeg\((?:full|framed), text \? null : UNDECODED_MAX_EDGE\)', scanner)
    assert len(conditional) == 2, 'both the shutter and the gallery import must do this'


TUNE_HARNESS = r"""
// Runs Scanner.tuneCamera against a fake camera and reports what it asked the track
// for, in order. What it must never ask for is a format it does not need: changing
// format is what resets a multi-lens phone back to its widest lens.
const fs = require('fs'), vm = require('vm');

const sandbox = {
    console, setTimeout, clearTimeout, Promise,
    requestAnimationFrame: (fn) => setTimeout(() => fn(Date.now()), 0),
    document: { createElement: () => ({ width: 0, height: 0, getContext: () => ({}) }) },
    navigator: {},
};
sandbox.window = sandbox; sandbox.self = sandbox; sandbox.globalThis = sandbox;
vm.createContext(sandbox);
vm.runInContext(fs.readFileSync(process.argv[2], 'utf8'), sandbox);

const settings = JSON.parse(process.argv[3]);
const caps = JSON.parse(process.argv[4]);

const asked = [];
const track = {
    getCapabilities: () => caps,
    getSettings: () => settings,
    async applyConstraints(c) { asked.push(JSON.parse(JSON.stringify(c))); },
};

(async () => {
    await sandbox.Scanner.tuneCamera(track);
    console.log(JSON.stringify({ asked }));
})().catch((e) => console.log(JSON.stringify({ error: e.message, stack: e.stack })));
"""


def _tune(tmp_path, settings, caps):
    harness = tmp_path / 'tune.js'
    harness.write_text(TUNE_HARNESS)
    result = subprocess.run(
        ['node', str(harness), str(STATIC / 'js' / 'scanner.js'),
         json.dumps(settings), json.dumps(caps)],
        capture_output=True, text=True, timeout=30)
    assert result.returncode == 0, result.stderr
    report = json.loads(result.stdout.strip().splitlines()[-1])
    assert 'error' not in report, report
    return report['asked']


@pytest.mark.skipif(not shutil.which('node'), reason='node is not installed')
def test_the_camera_is_configured_once_and_not_again(tmp_path):
    """
    Focus and resolution in one call, because applyConstraints is not a patch.

    Per spec it replaces a track's entire constraint set. The old code made two calls -
    focus hints, then resolution - so the second silently discarded the first, and
    continuous autofocus was requested on every camera open and taken away again a
    moment later, every time. On a phone being held over a small printed code that is
    most of the difference between a scanner that reads and one that hunts.
    """
    asked = _tune(
        tmp_path,
        {'width': 720, 'height': 1280},
        {'width': {'max': 2160}, 'height': {'max': 3840}, 'focusMode': ['continuous', 'manual']},
    )
    assert len(asked) == 1, asked
    # The long edge only, so the camera keeps its own aspect ratio rather than being
    # asked for a mode it does not have - and the focus hint rides along rather than
    # being wiped by it.
    assert asked[0] == {'height': {'ideal': 1920}, 'advanced': [{'focusMode': 'continuous'}]}


@pytest.mark.skipif(not shutil.which('node'), reason='node is not installed')
def test_a_stream_already_near_the_target_is_left_alone(tmp_path):
    """
    The reason the photograph stopped matching the viewfinder.

    A phone's rear camera is one virtual device in front of three lenses, and
    reconfiguring its format resets the zoom factor - which is another way of saying it
    picks a different lens. So a stream that came back at 1600 when 1920 was asked for
    must not be corrected: the extra pixels decide no receipt, and the price of asking
    is a viewfinder that jumps to the ultra-wide.

    Only the focus hint goes, and it carries the current size with it so that the call
    itself cannot trigger the reconfiguration it exists to avoid.
    """
    asked = _tune(
        tmp_path,
        {'width': 1200, 'height': 1600},
        {'width': {'max': 3024}, 'height': {'max': 4032}, 'focusMode': ['continuous']},
    )
    assert asked == [{'height': {'ideal': 1600}, 'advanced': [{'focusMode': 'continuous'}]}]


@pytest.mark.skipif(not shutil.which('node'), reason='node is not installed')
def test_a_camera_that_reports_nothing_is_not_reconfigured(tmp_path):
    """
    Every step is allowed to fail into leaving the stream alone.

    A camera with no capabilities to report, or no focus modes worth asking for and a
    resolution already good enough, still streams at whatever it negotiated - which is
    a working scanner, and is worth more than an error in front of someone holding a
    receipt.
    """
    assert _tune(tmp_path, {'width': 1080, 'height': 1920},
                 {'width': {'max': 1080}, 'height': {'max': 1920}}) == []


def test_the_shutter_does_not_reconfigure_the_camera():
    """
    Why the capture stopped being wider than the preview.

    capture() used to raise the track to the sensor's maximum for the duration of the
    shutter and put it back afterwards. On a phone with three rear lenses that is not a
    resolution change, it is a reconfiguration, and the camera comes back at its default
    zoom - the ultra-wide. The preview showed a receipt filling the frame; the
    photograph came out with the same receipt small in the middle of a table, and the
    crop could not save it because by then the viewfinder was showing something else.

    ImageCapture.takePhoto is gone for the same reason: it takes from the sensor rather
    than from the preview stream, so where it exists at all it has its own field of view
    and its own idea of the framing.
    """
    scanner = (STATIC / 'js' / 'scanner.js').read_text()
    capture = re.search(r'async capture\(\) \{(.*?)\n            \},', scanner, re.S)
    assert capture, 'capture() has moved; this asserts what it does not do'
    body = capture.group(1)

    assert 'applyConstraints' not in body, 'the shutter must not change the camera mode'
    assert 'ImageCapture' not in body, 'a sensor still has its own field of view'
    assert 'drawFull(video' in body, 'the still is the frame the viewfinder was showing'
    # And the frame after the tap, not the one already sitting in the element from
    # before the finger landed and the phone dipped.
    assert body.index('nextFrame(video)') < body.index('drawFull(video')


def test_the_camera_is_asked_for_a_long_edge_rather_than_a_landscape_frame():
    """
    Where the 720x1280 came from.

    `width: 1920, height: 1080` describes a landscape stream. A phone held upright
    produces a portrait one, and a browser resolving that pair by aspect ratio hands
    back the nearest mode it has - routinely 720p - from a camera holding twelve
    megapixels. And because `ideal` obliges nobody, the request has to be made a second
    time once the track's real capabilities can be read.

    Asserted of the <head> script as well as of scanner.js, and that is the half that
    was missing: the stream scanner.js uses is the one <head> opened, so its own
    carefully-worded constraints never run on a normal launch. The landscape pair lived
    on there, unread, doing the damage this test was written to prevent.
    """
    scanner = (STATIC / 'js' / 'scanner.js').read_text()
    constraints = re.search(r'var CAMERA_CONSTRAINTS = \{(.*?)\n    \};', scanner, re.S).group(1)

    assert 'width:' not in constraints, 'a width pins the frame to landscape'
    assert 'aspectRatio' in constraints and 'height:' in constraints

    shell = (TEMPLATES / 'scan' / 'shell.html').read_text()
    head = re.search(r'var constraints = \{(.*?)\n        \};', shell, re.S).group(1)
    assert 'width:' not in head, 'the request that is actually made pins the frame to landscape'
    assert head.count('aspectRatio') == 2 and head.count('height: { ideal: 1920 }') == 2, \
        'both branches - a chosen camera and the default one - make the same request'

    # And asked again with the answer in hand, on every path that starts a camera:
    # start(), revive() after the platform took it back, and switchCamera() when somebody
    # picks a different lens on the diagnostics screen. A path that opens a stream
    # without this runs the whole session at whatever the browser first felt like
    # giving, which is where the 720p uploads came from.
    assert len(re.findall(r'await tuneCamera\(track\)', scanner)) == 3


# --- A deploy reaching a phone that already has the app ----------------------
#
# The failure this covers is the one that produced "the PWA is broken": handleStatic
# was cache-first with no revalidation, so /static/js/pwa.js and /static/js/scanner.js
# were served out of the precache *permanently*, and the precache is only rebuilt when
# CACHE_VERSION changes. Navigations, meanwhile, have always revalidated - so a deploy
# that changed the scripts without bumping the constant gave every installed phone the
# new HTML wrapped around the old JavaScript on the very next launch. That is not a
# degraded app: the component throws while initialising and takes the whole shell down,
# history and all, and reloading re-serves the same stale script.

SERVICE_WORKER_HARNESS = r"""
const fs = require('fs');
const vm = require('vm');

const source = fs.readFileSync(process.argv[2], 'utf8');
const served = new Map(JSON.parse(process.argv[3]));   // url -> what the server has now
const seeded = new Map(JSON.parse(process.argv[4]));   // url -> what the phone cached

class FakeResponse {
    constructor(body, ok = true) { this.body = body; this.ok = ok; }
    clone() { return new FakeResponse(this.body, this.ok); }
}

class FakeCache {
    constructor() { this.store = new Map(); }
    async match(request) { return this.store.get(request.url); }
    async put(request, response) { this.store.set(request.url, response); }
}

const caches_ = new Map();
const fetched = [];

const context = {
    console,
    URL,
    setTimeout,
    clearTimeout,
    AbortController,
    fetched,
    caches_,
    FakeResponse,
    caches: {
        // Creation order matters: the real Cache Storage searches caches in the order
        // they were opened, which is exactly how a stale precache entry beats a fresh
        // runtime one.
        async open(name) {
            if (!caches_.has(name)) caches_.set(name, new FakeCache());
            return caches_.get(name);
        },
        async match(request) {
            for (const cache of caches_.values()) {
                const hit = await cache.match(request);
                if (hit) return hit;
            }
            return undefined;
        },
        async keys() { return [...caches_.keys()]; },
        async delete(name) { return caches_.delete(name); },
    },
    async fetch(request) {
        const url = typeof request === 'string' ? request : request.url;
        fetched.push(url);
        if (!served.has(url)) return new FakeResponse(null, false);
        return new FakeResponse(served.get(url), true);
    },
    self: {
        addEventListener() {},
        location: { origin: 'https://karani.example' },
        clients: { claim: async () => {} },
        skipWaiting: async () => {},
        registration: {},
    },
};
context.globalThis = context;
vm.createContext(context);

vm.runInContext(
    source + '\n;globalThis.__sw = { handleStatic, isOwnAsset, PRECACHE, RUNTIME };',
    context
);

(async () => {
    const sw = context.__sw;
    const precache = await context.caches.open(sw.PRECACHE);
    for (const [url, body] of seeded) {
        await precache.put({ url }, new FakeResponse(body, true));
    }

    const read = async (url) => {
        const waits = [];
        const event = { waitUntil: (p) => waits.push(p) };
        const response = await sw.handleStatic({ url, method: 'GET' }, event);
        // The launch is over; whatever the worker asked to finish, finishes.
        await Promise.all(waits.map((p) => p.catch(() => {})));
        return response ? response.body : null;
    };

    const out = {};
    for (const url of served.keys()) {
        out[url] = { first: await read(url), second: await read(url) };
    }
    out.fetched = fetched;
    console.log(JSON.stringify(out));
})().catch((e) => { console.log(JSON.stringify({ error: String(e && e.stack || e) })); });
"""


@pytest.mark.skipif(not shutil.which('node'), reason='node is not installed')
def test_a_new_script_reaches_a_phone_that_already_has_the_old_one(tmp_path):
    """
    Runs the real worker. Source-level checks are what let this ship in the first
    place: every individual piece read correctly, and the bug was in what the pieces
    did together over two launches.
    """
    app_js = 'https://karani.example/static/js/pwa.js'
    vendor_js = 'https://karani.example/static/js/vendor/alpine.min.js'

    harness = tmp_path / 'sw.js'
    harness.write_text(SERVICE_WORKER_HARNESS)
    result = subprocess.run(
        ['node', str(harness), str(SERVICE_WORKER),
         json.dumps([[app_js, 'NEW'], [vendor_js, 'NEW']]),
         json.dumps([[app_js, 'OLD'], [vendor_js, 'OLD']])],
        capture_output=True, text=True, timeout=60,
    )
    assert result.returncode == 0, result.stderr
    out = json.loads(result.stdout.strip().splitlines()[-1])
    assert 'error' not in out, out['error']

    # The first launch still paints instantly from the cache - that is the whole point
    # of precaching, and slowing it down would trade one complaint for another.
    assert out[app_js]['first'] == 'OLD'
    # The second launch has the deploy. Before this, it was 'OLD' forever.
    assert out[app_js]['second'] == 'NEW'


@pytest.mark.skipif(not shutil.which('node'), reason='node is not installed')
def test_the_pinned_vendor_bundles_are_not_refetched_on_every_launch(tmp_path):
    """
    The other half. zxing_reader.wasm and the Tailwind bundle are megabytes, they
    change only when someone deliberately replaces them, and this app runs on metered
    phone data - revalidating them every cold start would be a real cost paid for
    nothing.
    """
    app_js = 'https://karani.example/static/js/pwa.js'
    vendor_js = 'https://karani.example/static/js/vendor/alpine.min.js'
    icon = 'https://karani.example/static/icons/icon-192.png'

    harness = tmp_path / 'sw.js'
    harness.write_text(SERVICE_WORKER_HARNESS)
    result = subprocess.run(
        ['node', str(harness), str(SERVICE_WORKER),
         json.dumps([[app_js, 'NEW'], [vendor_js, 'NEW'], [icon, 'NEW']]),
         json.dumps([[app_js, 'OLD'], [vendor_js, 'OLD'], [icon, 'OLD']])],
        capture_output=True, text=True, timeout=60,
    )
    assert result.returncode == 0, result.stderr
    out = json.loads(result.stdout.strip().splitlines()[-1])
    assert 'error' not in out, out['error']

    assert out[vendor_js]['second'] == 'OLD'
    assert out[icon]['second'] == 'OLD'
    assert vendor_js not in out['fetched']
    assert icon not in out['fetched']


def test_the_worker_was_reversioned_for_the_scripts_that_changed_with_it():
    """
    Revalidation heals a missed bump on the next launch; it does not make the bump
    pointless. The version is what evicts the old precache in one step rather than
    asset by asset, and it is the only thing that fixes the phones already sitting on
    a broken pairing right now.
    """
    source = SERVICE_WORKER.read_text()
    assert "const CACHE_VERSION = 'v5'" not in source, (
        'pwa.js, scanner.js and the scan shell changed; CACHE_VERSION is still v5, '
        'so installed phones keep the old scripts.'
    )
    assert "const CACHE_VERSION = 'v6'" not in source, (
        'scanner.js now takes the still at the sensor resolution instead of the '
        "viewfinder's; CACHE_VERSION is still v6, so every already-installed phone "
        'keeps uploading the 720x1280 preview frames that no decoder can read.'
    )
    assert "const CACHE_VERSION = 'v7'" not in source, (
        'pwa.js no longer lets a stuck outbox suppress the history pull; '
        'CACHE_VERSION is still v7, so every already-installed phone keeps the build '
        'whose history screen stays empty for as long as one receipt cannot be sent.'
    )
    assert "const CACHE_VERSION = 'v8'" not in source, (
        'scanner.js, pwa.js, scan.css and the scan shell all changed together - a '
        'choosable camera, a capture cropped to what the viewfinder showed, and the '
        'photograph kept alongside a decoded code. CACHE_VERSION is still v8, so every '
        'already-installed phone keeps the build that discards the picture and files '
        'photos full of the margins nobody framed.'
    )
    assert "const CACHE_VERSION = 'v10'" not in source, (
        'the scan shell, scanner.js, pwa.js and scan.css changed together again - the '
        'capture is the whole preview, a photo the server refuses as too large is '
        'shrunk and re-sent instead of being parked, and the sent list no longer '
        'renders as nothing when two receipts share a date. CACHE_VERSION is still '
        'v10, so every already-installed phone keeps the build with the empty history '
        'screen.'
    )


# --- The history that stopped arriving ----------------------------------------
#
# Reported from the field as two separate complaints - "I cannot see the receipts this
# phone sent" and "my photos will not sync" - which were one bug wearing two hats. The
# phone's summary kept updating (its own endpoint, its own timer), so the screen showed
# live totals above an empty list: the most convincing way an app can say a receipt is
# gone when it is not.

SYNC_HARNESS = """
const fs = require('fs');

/*
 * Enough of idb to run the real sync(), and no more: get/put/delete/getAll/count on a
 * plain Map per store, plus the one cursor walk cachedHistory does. Stubbing sync()
 * itself would test the stub; this way the code under test is the shipped file.
 */
function fakeDB() {
    const stores = { outbox: new Map(), submissions: new Map(), meta: new Map() };
    const keyOf = (name, value) => name === 'outbox' ? value.client_uuid
                                 : name === 'meta' ? value.key : value.id;
    const db = {
        version: 1,
        objectStoreNames: { contains: (n) => n in stores },
        async get(name, key) { return stores[name].get(key); },
        async getAll(name) { return Array.from(stores[name].values()); },
        async count(name) { return stores[name].size; },
        async put(name, value) { stores[name].set(keyOf(name, value), value); },
        async delete(name, key) { stores[name].delete(key); },
        transaction(name) {
            const store = {
                async put(value) { stores[name].set(keyOf(name, value), value); },
                async openCursor() {
                    const rows = Array.from(stores[name].values()).sort((a, b) => b.id - a.id);
                    let i = 0;
                    const at = () => i >= rows.length ? null : {
                        value: rows[i], async continue() { i += 1; return at(); },
                    };
                    return at();
                },
            };
            return { store, objectStore: () => store, done: Promise.resolve() };
        },
        close() {},
    };
    db._stores = stores;
    return db;
}

const db = fakeDB();
global.idb = { openDB: async () => db };

const calls = [];
global.fetch = async (url, options = {}) => {
    calls.push(String(url));
    const respond = (status, body) => ({
        ok: status >= 200 && status < 300,
        status,
        async json() { return body; },
        clone() { return this; },
    });
    if (String(url).indexOf('/scan/api/sync/photo') === 0) {
        // The ingress refusing a photo larger than its body limit. Permanent, and the
        // whole point of the scenario.
        return respond(413, {});
    }
    if (String(url).indexOf('/scan/api/submissions') === 0) {
        return respond(200, { submissions: JSON.parse(process.argv[3]), has_more: false });
    }
    return respond(200, {});
};

global.AbortController = class { constructor() { this.signal = null; } abort() {} };
global.window = {
    navigator: { onLine: true },
    document: { addEventListener() {}, visibilityState: 'visible' },
    addEventListener() {},
    localStorage: {
        getItem: () => JSON.stringify({ token: 'session-token', device: { name: 'Test Phone' } }),
        setItem() {}, removeItem() {},
    },
    setTimeout: setTimeout,
    crypto: { randomUUID: () => 'uuid-' + Math.random().toString(36).slice(2) },
    fetch: global.fetch,
    AbortController: global.AbortController,
};
eval(fs.readFileSync(process.argv[2], 'utf8'));
const PWA = global.window.PWA;

(async () => {
    const out = {};
    // One photo this server will never accept, sitting at the head of the queue - the
    // state both screenshots from the field were in.
    await db.put('outbox', {
        client_uuid: 'stuck-photo', kind: 'photo',
        // A real Blob: syncPhoto puts it through FormData, which rejects anything else.
        photo: new Blob([new Uint8Array(1024)], { type: 'image/jpeg' }),
        captured_at: '2026-08-11T19:10:00', attempts: 0, last_error: null,
    });

    const first = await PWA.sync();
    out.firstSync = first;
    out.historyAfterFirstSync = (await PWA.cachedHistory({})).map((r) => r.id);
    out.askedFor = calls.slice();

    const stuck = await db.get('outbox', 'stuck-photo');
    out.attempts = stuck.attempts;
    out.lastError = stuck.last_error;
    // Still in the outbox: a receipt the server refused is not a receipt to delete.
    out.stillQueued = (await PWA.outboxAll()).length;

    // A second pass must not spend another request on a verdict already delivered...
    calls.length = 0;
    await PWA.sync();
    out.retriedThePhoto = calls.some((u) => u.indexOf('/scan/api/sync/photo') === 0);
    // ...and must still read history back.
    out.pulledAgain = calls.some((u) => u.indexOf('/scan/api/submissions') === 0);

    console.log(JSON.stringify(out));
})().catch((e) => console.log(JSON.stringify({ error: String(e && e.stack || e) })));
"""


@pytest.mark.skipif(not shutil.which('node'), reason='node is not installed')
def test_a_receipt_that_cannot_be_sent_does_not_hide_the_ones_that_were(tmp_path):
    """
    sync() drains the outbox and then reads history back. Those are two different jobs
    that happen to share a timer, and the read used to be reachable only by falling off
    the end of the drain: any `stop` returned early, and the pull was additionally
    gated on `sent > 0 || settings.refresh`.

    So one undeliverable photo - a 413 from the ingress body limit, say - was enough to
    switch the history screen off permanently. `sent` cannot climb while the queue is
    stuck, and the periodic sync passes no `refresh`, so the gate stayed shut on every
    tick after boot. The phone showed a summary that kept moving above a list that
    stayed empty, and reinstalling made it worse: that clears the cache the screen had
    been falling back on.
    """
    rows = [{'id': 41, 'status': 'completed', 'captured_at': '2026-08-10T09:00:00'},
            {'id': 42, 'status': 'completed', 'captured_at': '2026-08-11T09:00:00'}]

    harness = tmp_path / 'sync.js'
    harness.write_text(SYNC_HARNESS)
    result = subprocess.run(
        ['node', str(harness), str(STATIC / 'js' / 'pwa.js'), json.dumps(rows)],
        capture_output=True, text=True, timeout=60,
    )
    assert result.returncode == 0, result.stderr
    out = json.loads(result.stdout.strip().splitlines()[-1])
    assert 'error' not in out, out['error']

    # The photo genuinely did not go, and is reported as not having gone...
    assert out['firstSync']['sent'] == 0
    assert out['firstSync']['failed'] == 1
    # ...and is still on the phone. A refusal is not permission to drop a receipt.
    assert out['stillQueued'] == 1

    # The whole bug, in one assertion: history arrived anyway.
    assert out['historyAfterFirstSync'] == [42, 41], \
        'a stuck outbox is still suppressing the history pull'
    assert any(u.startswith('/scan/api/submissions') for u in out['askedFor'])

    # A permanent rejection is spent at once rather than dressed up as "Waiting" for
    # eight more identical attempts, and it says what happened in words.
    assert out['attempts'] == 8
    assert 'too large' in out['lastError']
    assert '413' in out['lastError']
    assert not out['retriedThePhoto'], 'a 413 is being retried with the same bytes'

    # And the read half keeps running on every tick, not just the one that sent something.
    assert out['pulledAgain']


GROUPING_DRIVER = """
const page = historyPage();

// The order the list actually arrives in: by id, which is the order the server
// received them - while the heading comes from captured_at, which is when somebody
// stood in front of the receipt. A gallery import carries the photograph's own date,
// so a June receipt sent today has a today-sized id and a June heading, and the same
// day turns up in more than one place in the list.
page.submissions = [
    { id: 60, captured_at: '2026-08-27T09:00:00', received_at: '2026-08-27T09:00:00' },
    { id: 59, captured_at: '2026-06-28T11:04:00', received_at: '2026-08-27T08:59:00' },
    { id: 58, captured_at: '2026-08-27T08:00:00', received_at: '2026-08-27T08:00:00' },
    { id: 57, captured_at: '2026-06-28T09:15:00', received_at: '2026-08-27T07:00:00' },
    { id: 56, captured_at: null,                  received_at: null },
    { id: 55, captured_at: '2026-08-03T12:00:00', received_at: '2026-08-03T12:00:00' },
];

console.log(JSON.stringify({
    keys: page.groupedSubmissions.map((g) => g.key),
    labels: page.groupedSubmissions.map((g) => g.label),
    items: page.groupedSubmissions.map((g) => g.items.map((s) => s.id)),
}));
"""


@pytest.mark.skipif(not shutil.which('node'), reason='node is not installed')
def test_two_receipts_from_the_same_day_never_produce_two_groups(tmp_path):
    """
    Thirteen receipts on the server, thirteen in the cache, none on the screen.

    The sent list is an x-for keyed on the group's heading, and the groups used to be
    runs of consecutive rows sharing one. That is safe only while the list's order and
    the headings agree, and importing from the gallery is exactly what parts them: an
    import carries the photograph's own date, so a receipt taken in June arrives with a
    today-sized id, and the list runs "Today, 28 Jun, Today" - the same heading twice.

    Alpine's keyed x-for reconciles a duplicate key by looking up one that is no longer
    in its table, throws inside the effect, and never reaches the pass that inserts new
    elements. Not one row rendered. The screen still had the receipts - the empty state
    stayed hidden and "You've reached the beginning" was printed underneath the nothing,
    both of which read `submissions.length` - and the hero above it kept counting them,
    because the summary comes from its own endpoint. An app saying "13 receipts" over an
    empty list is the most convincing way it can say a day's work is gone.

    So the key is the calendar day, unique by construction, and a day is one group
    wherever in the list its receipts turn up.
    """
    bundle = tmp_path / 'grouping.js'
    bundle.write_text(
        "global.PWA = { MAX_ATTEMPTS: 8 };\n"
        "global.navigator = { onLine: true };\n"
        + history_component_source() + GROUPING_DRIVER
    )

    result = subprocess.run(['node', str(bundle)], capture_output=True, text=True, timeout=30)
    assert result.returncode == 0, result.stderr
    out = json.loads(result.stdout.strip().splitlines()[-1])

    assert len(out['keys']) == len(set(out['keys'])), \
        f"two groups share a key, which renders the whole list as nothing: {out['keys']}"
    assert len(out['labels']) == len(set(out['labels'])), \
        'two headings read the same, which is the same bug one step later'

    # One group per day, newest day first, and the undated row at the bottom rather
    # than somewhere arbitrary in the middle of the year.
    assert out['keys'] == ['2026-08-27', '2026-08-03', '2026-06-28', 'unknown']
    assert out['items'] == [[60, 58], [55], [59, 57], [56]]


SHRINK_HARNESS = """
const fs = require('fs');

/*
 * A server behind a proxy with a one-megabyte body limit, which is the default that
 * put "HTTP 413" under receipts in the field. Anything larger never reaches Flask.
 */
const LIMIT = 1024 * 1024;
const posted = [];

function fakeDB() {
    const stores = { outbox: new Map(), submissions: new Map(), meta: new Map() };
    const keyOf = (name, value) => name === 'outbox' ? value.client_uuid
                                 : name === 'meta' ? value.key : value.id;
    const db = {
        version: 1,
        objectStoreNames: { contains: (n) => n in stores },
        async get(name, key) { return stores[name].get(key); },
        async getAll(name) { return Array.from(stores[name].values()); },
        async count(name) { return stores[name].size; },
        async put(name, value) { stores[name].set(keyOf(name, value), value); },
        async delete(name, key) { stores[name].delete(key); },
        transaction(name) {
            const store = {
                async put(value) { stores[name].set(keyOf(name, value), value); },
                async openCursor() { return null; },
            };
            return { store, objectStore: () => store, done: Promise.resolve() };
        },
        close() {},
    };
    db._stores = stores;
    return db;
}

const db = fakeDB();
global.idb = { openDB: async () => db };

global.fetch = async (url, options = {}) => {
    const respond = (status, body) => ({
        ok: status >= 200 && status < 300, status,
        async json() { return body; }, clone() { return this; },
    });
    if (String(url).indexOf('/scan/api/sync/photo') === 0) {
        const photo = options.body.get('receiptphoto');
        posted.push(photo.size);
        if (photo.size > LIMIT) return respond(413, {});
        return respond(200, { client_uuid: 'shrink-me', submission_id: 7, status: 'accepted' });
    }
    if (String(url).indexOf('/scan/api/submissions') === 0) {
        return respond(200, { submissions: [], has_more: false });
    }
    return respond(200, {});
};

global.AbortController = class { constructor() { this.signal = null; } abort() {} };

/*
 * Enough of a canvas to run the real Scanner.shrinkToFit rather than a stand-in for it.
 * The encoder is modelled, not performed: JPEG bytes go roughly with area times
 * quality, and the constant is fitted so a 3000px frame at 0.82 lands where a real one
 * does - about 1.8MB. What is under test is which rungs of the ladder get tried and
 * which blob is sent, not libjpeg.
 */
const BYTES_PER_PIXEL_QUALITY = 0.244;
global.window = {
    navigator: { onLine: true },
    document: {
        addEventListener() {}, visibilityState: 'visible',
        createElement: () => ({
            width: 0, height: 0,
            getContext: () => ({ drawImage() {} }),
            toBlob(callback, type, quality) {
                const bytes = Math.round(this.width * this.height * quality * BYTES_PER_PIXEL_QUALITY);
                callback(new Blob([new Uint8Array(bytes)], { type: 'image/jpeg' }));
            },
        }),
    },
    createImageBitmap: async () => ({ width: 3000, height: 4000, close() {} }),
    addEventListener() {},
    localStorage: {
        getItem: () => JSON.stringify({ token: 'session-token', device: { name: 'Test Phone' } }),
        setItem() {}, removeItem() {},
    },
    setTimeout: setTimeout,
    crypto: { randomUUID: () => 'uuid-' + Math.random().toString(36).slice(2) },
    fetch: global.fetch,
    AbortController: global.AbortController,
};
eval(fs.readFileSync(process.argv[2], 'utf8'));   // scanner.js
eval(fs.readFileSync(process.argv[3], 'utf8'));   // pwa.js
const PWA = global.window.PWA;

(async () => {
    const out = {};
    // A gallery import: a twelve-megapixel original, which is what a phone's camera
    // roll actually holds and what the shutter path never produces.
    const original = 1800 * 1024;
    await db.put('outbox', {
        client_uuid: 'shrink-me', kind: 'photo',
        photo: new Blob([new Uint8Array(original)], { type: 'image/jpeg' }),
        captured_at: '2026-06-28T11:04:00', attempts: 0, last_error: null,
    });

    const result = await PWA.sync();
    out.sync = result;
    out.posted = posted.slice();
    out.limit = LIMIT;
    out.stillQueued = (await PWA.outboxAll()).length;
    const ceiling = db._stores.meta.get('uploadCeilingBytes');
    out.ceiling = ceiling ? ceiling.value.bytes : null;

    // A second photo, queued after the wall was found: it must not have to discover it
    // again. Same size, same server - and this time it should fit on the first request.
    posted.length = 0;
    await db.put('outbox', {
        client_uuid: 'second-photo', kind: 'photo',
        photo: new Blob([new Uint8Array(original)], { type: 'image/jpeg' }),
        captured_at: '2026-06-28T11:20:00', attempts: 0, last_error: null,
    });
    await PWA.sync({ force: true });
    out.secondPosted = posted.slice();

    console.log(JSON.stringify(out));
})().catch((e) => console.log(JSON.stringify({ error: String(e && e.stack || e) })));
"""


@pytest.mark.skipif(not shutil.which('node'), reason='node is not installed')
def test_a_photo_too_large_to_send_is_shrunk_rather_than_given_up_on(tmp_path):
    """
    The one failure in this app a field user could do nothing at all about.

    A photograph larger than the body limit of the proxy in front of the app is refused
    with a 413 before Flask is ever reached. The app marked the receipt permanently
    failed and left it in the outbox reading "Not sending. This photo is too large for
    the server to accept (HTTP 413)" - true, and useless: nobody standing in a shop
    knows what an ingress body limit is, no phone offers to resize a photograph, and
    the only person who could act on it was not holding the phone.

    Importing from the gallery made it routine rather than rare. The shutter's own
    output is a preview frame, already bounded; a picked file is a twelve-megapixel
    original, and a camera roll full of them lost every single one.

    So a refusal is now an instruction to re-encode. What this pins is that the second
    request carries fewer bytes than the first, that the receipt actually goes, and that
    the next photo up does not have to discover the same wall for itself.
    """
    harness = tmp_path / 'shrink.js'
    harness.write_text(SHRINK_HARNESS)
    result = subprocess.run(
        ['node', str(harness), str(STATIC / 'js' / 'scanner.js'), str(STATIC / 'js' / 'pwa.js')],
        capture_output=True, text=True, timeout=60,
    )
    assert result.returncode == 0, result.stderr
    out = json.loads(result.stdout.strip().splitlines()[-1])
    assert 'error' not in out, out['error']

    # Refused once, then sent smaller - not marked failed and parked.
    assert len(out['posted']) >= 2, 'a 413 ended the receipt instead of shrinking it'
    assert out['posted'][0] > out['limit'], 'the first attempt was already inside the limit'
    assert out['posted'][-1] <= out['limit']
    assert out['posted'][-1] < out['posted'][0]

    # And it went. The receipt is off the phone rather than sitting in the outbox
    # under a number nobody can act on.
    assert out['sync']['sent'] == 1
    assert out['stillQueued'] == 0

    # The wall is remembered, so the next photo is shrunk before it is sent rather than
    # spending a failed multi-megabyte upload to find the same limit again.
    assert out['ceiling'] and out['ceiling'] < out['posted'][0]
    assert len(out['secondPosted']) == 1, 'the second photo rediscovered the body limit'
    assert out['secondPosted'][0] <= out['limit']


# --- Which camera, and what the shutter actually keeps ------------------------


def test_the_saved_camera_is_read_by_the_head_script_under_the_same_key():
    """
    Two files, one localStorage key, and no import between them.

    The preference is written by scanner.js and read by an inline <script> in the page
    <head>, which runs before scanner.js has parsed - that is the whole reason it is in
    localStorage rather than in the IndexedDB store with everything else, and it means
    the key is a string duplicated across two files with nothing to keep them in step.
    Drift here is silent in the worst way: choosing a camera appears to work, and every
    launch afterwards opens the default one.
    """
    scanner = (STATIC / 'js' / 'scanner.js').read_text()
    shell = (TEMPLATES / 'scan' / 'shell.html').read_text()

    key = re.search(r"var CAMERA_PREF_KEY = '([^']+)';", scanner)
    assert key, 'CAMERA_PREF_KEY is no longer a literal; the head script reads it by hand.'
    assert f"getItem('{key.group(1)}')" in shell, \
        'the head script is reading a different localStorage key than scanner.js writes'


def test_a_named_camera_is_asked_for_exactly_and_never_alongside_facingmode():
    """
    `ideal` is a suggestion, and facingMode competes with a deviceId.

    Both mistakes have the same symptom - the choice appears to save and the same
    camera keeps opening - and neither throws, so nothing but this notices.
    """
    scanner = (STATIC / 'js' / 'scanner.js').read_text()
    shell = (TEMPLATES / 'scan' / 'shell.html').read_text()

    constraints = re.search(r'function constraintsFor\(deviceId\) \{(.*?)\n    \}', scanner, re.S)
    assert constraints, 'constraintsFor is where a chosen camera is turned into constraints'
    body = constraints.group(1)
    assert 'video.deviceId = { exact: deviceId }' in body, 'an ideal deviceId may be ignored'
    assert 'delete video.facingMode' in body, \
        'facingMode left beside a deviceId is how a browser answers with the wrong camera'

    # And the same on the head-script path, which opens the camera first.
    assert 'deviceId: { exact: chosen }' in shell


def test_a_camera_that_will_not_open_falls_back_instead_of_breaking_the_scanner():
    """
    A remembered camera is never allowed to be the reason this app cannot scan.

    Ids go stale - a cleaned-up device, a USB camera unplugged, a browser that rotates
    them - and an app that will not open its viewfinder because of a setting is worse
    than one that opens the wrong lens.
    """
    scanner = (STATIC / 'js' / 'scanner.js').read_text()
    request = re.search(r'function requestCamera\(deviceId\) \{(.*?)\n    \}\n', scanner, re.S)
    assert request, 'requestCamera is where a stale preference has to be survived'
    body = request.group(1)

    assert '.catch(' in body, 'an exact deviceId that fails has no fallback'
    assert 'getUserMedia(CAMERA_CONSTRAINTS)' in body
    # A refusal is not a stale id, and retrying it just asks the person twice.
    assert "NotAllowedError" in body


@pytest.mark.skipif(not shutil.which('node'), reason='node is not installed')
def test_the_photograph_matches_what_the_viewfinder_was_showing(tmp_path):
    """
    The crop, evaluated - this is pure arithmetic and worth actually running.

    The viewfinder is `object-fit: cover`: the video is scaled until it fills the
    screen and everything past the edges is cut off, in the preview only. `capture()`
    drew the whole video, so the photograph contained a wide band down each side that
    nobody had seen - a third of a 3:4 sensor on a tall phone screen. That band is not
    just untidy: it is pixels inside the upload budget, so the receipt itself arrived
    smaller than it needed to be, and its QR code with it.
    """
    bundle = tmp_path / 'crop.js'
    bundle.write_text(
        # Enough of a DOM for a canvas to be created and measured. drawImage is
        # recorded rather than performed; the numbers it is called with are the test.
        "const drawn = [];\n"
        "global.window = {\n"
        "  innerWidth: 390, innerHeight: 844,\n"
        "  document: { createElement: () => ({\n"
        "    width: 0, height: 0,\n"
        "    getContext: () => ({ drawImage: (...a) => drawn.push(a.slice(1)) }),\n"
        "  }) },\n"
        "  navigator: {},\n"
        "};\n"
        + (STATIC / 'js' / 'scanner.js').read_text() + "\n"
        "const crop = window.Scanner.cropToViewfinder;\n"
        "const canvas = (w, h) => ({ width: w, height: h,\n"
        "  getContext: () => ({ drawImage: () => {} }) });\n"
        "const out = {};\n"
        "// A 3:4 sensor behind a 390x844 screen: the sides are what the person could\n"
        "// not see, so the sides are what goes.\n"
        "const tall = crop(canvas(3000, 4000), { clientWidth: 390, clientHeight: 844 });\n"
        "out.tall = [tall.width, tall.height];\n"
        "out.tallDraw = drawn[drawn.length - 1];\n"
        "// Already the shape of the screen: nothing to give up, and no re-encode.\n"
        "const exact = canvas(390, 844);\n"
        "out.untouched = crop(exact, { clientWidth: 390, clientHeight: 844 }) === exact;\n"
        "// A landscape still behind a portrait screen crops hard, and stays centred.\n"
        "const wide = crop(canvas(4000, 3000), { clientWidth: 390, clientHeight: 844 });\n"
        "out.wide = [wide.width, wide.height];\n"
        "out.wideDraw = drawn[drawn.length - 1];\n"
        "// No element to measure: fall back to the window rather than to nothing.\n"
        "out.fallback = (() => { const c = crop(canvas(3000, 4000), null); return [c.width, c.height]; })();\n"
        "console.log(JSON.stringify(out));\n"
    )

    result = subprocess.run(['node', str(bundle)], capture_output=True, text=True, timeout=30)
    assert result.returncode == 0, result.stderr
    out = json.loads(result.stdout.strip().splitlines()[-1])

    # 390/844 is taller than 3000/4000, so the height is kept and the width is trimmed
    # to 4000 * (390/844) = 1848.
    assert out['tall'] == [1848, 4000]
    # Centred: (3000 - 1848) / 2 = 576 off each side, full height taken.
    assert out['tallDraw'][:4] == [576, 0, 1848, 4000]

    assert out['untouched'], 'a frame already at the screen ratio was re-encoded for nothing'

    # 4000x3000 behind a portrait screen: the width is kept and the height collapses to
    # 4000 / (390/844) = 8656 - more than there is - so the width gives instead.
    assert out['wide'] == [1386, 3000]
    assert out['wideDraw'][:4] == [1307, 0, 1386, 3000]

    assert out['fallback'] == [1848, 4000], 'the window is the fallback measurement'


@pytest.mark.skipif(not shutil.which('node'), reason='node is not installed')
def test_the_photograph_is_the_whole_preview_and_nothing_narrower(tmp_path):
    """
    The other half of "the capture does not match what I framed".

    Photo mode draws corner brackets, and for one version those brackets were the crop:
    they sat inset from the screen edges - a strip above them, a much taller strip below
    where the mode switch and the shutter sit - and the photograph was cut down to what
    was between them. A receipt that filled the preview came back with its top line or
    its total gone, and nothing on screen said which.

    A viewfinder that keeps less than it shows is not a viewfinder. The crop is the
    preview: everything `object-fit: cover` puts on the glass, and everything it hangs
    over the edges is what goes. Measured on a real geometry - a 390x844 screen over a
    1080x1920 stream - because this is arithmetic and worth running rather than reading.
    """
    bundle = tmp_path / 'framecrop.js'
    bundle.write_text(
        "const drawn = [];\n"
        "global.window = {\n"
        "  innerWidth: 390, innerHeight: 844,\n"
        "  document: { createElement: () => ({\n"
        "    width: 0, height: 0,\n"
        "    getContext: () => ({ drawImage: (...a) => drawn.push(a.slice(1)) }),\n"
        "  }) },\n"
        "  navigator: {},\n"
        "};\n"
        + (STATIC / 'js' / 'scanner.js').read_text() + "\n"
        "const crop = window.Scanner.cropToViewfinder;\n"
        "const canvas = (w, h) => ({ width: w, height: h,\n"
        "  getContext: () => ({ drawImage: () => {} }) });\n"
        "const el = (left, top, width, height) => ({\n"
        "  getBoundingClientRect: () => ({ left, top, width, height }) });\n"
        "const out = {};\n"
        "const video = el(0, 0, 390, 844);\n"
        "const shot = crop(canvas(1080, 1920), video);\n"
        "out.whole = [shot.width, shot.height];\n"
        "out.wholeDraw = drawn[drawn.length - 1];\n"
        "// The brackets are no longer an argument. Handing one over anyway - a stale\n"
        "// caller, a merge - must not quietly shrink the photograph again.\n"
        "const withFrame = crop(canvas(1080, 1920), video, el(16, 56, 358, 652));\n"
        "out.withFrame = [withFrame.width, withFrame.height];\n"
        "// A stream already the shape of the screen gives up nothing, and is not\n"
        "// re-encoded for a rounding difference.\n"
        "const exact = canvas(390, 844);\n"
        "out.untouched = crop(exact, video) === exact;\n"
        "console.log(JSON.stringify(out));\n"
    )

    result = subprocess.run(['node', str(bundle)], capture_output=True, text=True, timeout=30)
    assert result.returncode == 0, result.stderr
    out = json.loads(result.stdout.strip().splitlines()[-1])

    # `cover` scales 1080x1920 by max(390/1080, 844/1920) = 0.4396 and centres it, so
    # the stream is drawn 475x844 and 42.5px of it hangs off each side. The full height
    # is kept and the sides go: 1920 * (390/844) = 887, centred, (1080 - 887) / 2 in.
    assert out['whole'] == [887, 1920]
    assert out['wholeDraw'][:4] == [96, 0, 887, 1920]

    assert out['withFrame'] == out['whole'], \
        'something is still narrowing the capture to an element inside the screen'
    assert out['untouched'], 'a frame already at the screen ratio was re-encoded for nothing'


def test_a_camera_switch_that_did_not_happen_is_not_saved_as_one():
    """
    "I picked a different camera and nothing changed."

    requestCamera answers an exact deviceId it cannot open by reopening with no
    preference at all - deliberately, so a stale id costs one extra call rather than a
    scanner that will not start. But switchCamera then saved the preference anyway and
    the screen said "Saved. This camera opens from now on." So the tick moved, the
    viewfinder did not, and every launch afterwards paid for a getUserMedia that fell
    back to the same camera as before.

    What is streaming is the only honest answer, and a browser that will not report a
    deviceId counts as honoured - "cannot tell" is not "wrong", and refusing to save on
    it would make the preference unusable on the phones that need it most.
    """
    scanner = (STATIC / 'js' / 'scanner.js').read_text()
    switch = re.search(r'async switchCamera\(deviceId\) \{(.*?)\n            \},', scanner, re.S)
    assert switch, 'switchCamera has moved; this asserts what it saves and when'
    body = switch.group(1)

    assert 'rememberCamera(honoured ? deviceId : null)' in body, \
        'a preference is only worth saving for a camera that actually opened'
    assert body.count('rememberCamera(') == 1, 'one place decides, or the two disagree'
    assert 'honoured = !deviceId || !got || got === deviceId' in body

    diagnostics = (TEMPLATES / 'scan' / '_diagnostics.html').read_text()
    assert 'result.honoured === false' in diagnostics, \
        'the screen has to be able to say the switch did not take'


def test_the_shutter_crops_before_it_decodes():
    """
    Order matters, and this is the one that is easy to get backwards.

    Decoding the wider frame first would occasionally read a QR code from outside the
    viewfinder - a poster behind the counter, the next receipt in the pile - and queue
    it against a photograph that visibly does not contain it. That is a worse answer
    than not reading it at all, because it looks right.
    """
    scanner = (STATIC / 'js' / 'scanner.js').read_text()
    capture = re.search(r'async capture\(\) \{(.*?)\n            \},', scanner, re.S)
    assert capture, 'capture() has moved; this asserts what it does in what order'
    body = capture.group(1)

    assert body.index('cropToViewfinder') < body.index('decodeStill'), \
        'the frame is decoded before it is cropped, so a code outside the view can win'
    assert 'decodeStill(framed)' in body and 'toJpeg(framed' in body, \
        'the decode and the upload must be of the same pixels'


def test_a_decoded_code_no_longer_costs_the_photograph_it_was_read_from():
    """
    The half of a scan that used to be thrown away.

    Both capture paths held a photograph and a decoded URL at the same moment, and
    dropped the photograph on the reasoning that a verified receipt beats a picture of
    one. They are not alternatives: the receipt TRA confirmed then had no image behind
    it at all, and the server's own QR decoder only ever saw photographs this phone had
    already failed to read - a selection effect that makes its hit rate look like a
    fault.
    """
    scanner_view = (TEMPLATES / 'scan' / '_scanner.html').read_text()

    # The shutter keeps the blob on both branches, not just the undecoded one.
    take = re.search(r'async takePhoto\(\) \{(.*?)\n        \},', scanner_view, re.S)
    assert take, 'takePhoto has moved'
    assert "kind: 'qr'" in take.group(1) and 'blob: shot.blob' in take.group(1), \
        'a decoded capture is still discarding its photograph'

    # And the gallery import builds one shape for both outcomes rather than two.
    imported = re.search(r'async importFromGallery\(event\) \{(.*?)\n        \},', scanner_view, re.S)
    assert imported, 'importFromGallery has moved'
    assert 'blob: shot.blob' in imported.group(1)

    # Which is only worth anything if the outbox sends both up.
    queue_one = re.search(r'queueOne\(item, note\) \{(.*?)\n        \},', scanner_view, re.S)
    assert queue_one, 'queueOne has moved'
    body = queue_one.group(1)
    assert 'if (item.receipt) fields.receipturl' in body
    assert 'if (item.blob) fields.photo' in body

    pwa = (STATIC / 'js' / 'pwa.js').read_text()
    assert "form.append('receipturl', entry.receipturl)" in pwa, \
        'the photo upload is not carrying the code the phone read'
