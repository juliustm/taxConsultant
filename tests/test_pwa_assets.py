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

    decode = scanner.split('async function decodeWithZXing(canvas)')[1].split('\n    /*')[0]
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

    conditional = re.findall(r'toJpeg\(full, text \? null : UNDECODED_MAX_EDGE\)', scanner)
    assert len(conditional) == 2, 'both the shutter and the gallery import must do this'


CAPTURE_HARNESS = r"""
// Runs Scanner.grabAtFullResolution against a fake camera, and reports what it asked
// the track for, in order, and what it ended up drawing.
const fs = require('fs'), vm = require('vm');

function fakeContext() {
    return { drawImage() {}, getImageData: () => ({ data: new Uint8ClampedArray(4) }), putImageData() {} };
}

const sandbox = {
    console, setTimeout, clearTimeout, Promise,
    requestAnimationFrame: (fn) => setTimeout(() => fn(Date.now()), 0),
    document: { createElement: () => ({ width: 0, height: 0, getContext: fakeContext }) },
    navigator: {},
};
sandbox.window = sandbox; sandbox.self = sandbox; sandbox.globalThis = sandbox;
// The whole point of the path under test: no ImageCapture, as on every iPhone.
delete sandbox.ImageCapture;
vm.createContext(sandbox);
vm.runInContext(fs.readFileSync(process.argv[2], 'utf8'), sandbox);

const asked = [];
// A camera streaming a 720x1280 preview from a sensor that can do 2160x3840 - the
// arrangement that produced the unreadable uploads.
const video = { videoWidth: 720, videoHeight: 1280 };
const track = {
    getCapabilities: () => ({ width: { max: 2160 }, height: { max: 3840 } }),
    getSettings: () => ({ width: video.videoWidth, height: video.videoHeight }),
    async applyConstraints(c) {
        asked.push(JSON.parse(JSON.stringify(c)));
        // A real camera does not deliver the new size the instant the promise settles -
        // it is reconfiguring hardware. Frames keep arriving at the old resolution for
        // a while, which is the whole reason grabAtFullResolution waits rather than
        // drawing as soon as applyConstraints resolves.
        const edge = (c.height && c.height.ideal) || (c.width && c.width.ideal);
        let framesLate = 4;
        const tick = () => {
            if (--framesLate > 0) return setTimeout(tick, 0);
            video.videoHeight = edge;
            video.videoWidth = Math.round(edge * 720 / 1280);
        };
        setTimeout(tick, 0);
    },
};

const canvas = { width: 0, height: 0, getContext: fakeContext };

(async () => {
    const drew = await sandbox.Scanner.grabAtFullResolution(track, video, canvas);
    console.log(JSON.stringify({
        drew,
        asked,
        captured: canvas.width + 'x' + canvas.height,
    }));
})().catch((e) => console.log(JSON.stringify({ error: e.message, stack: e.stack })));
"""


@pytest.mark.skipif(not shutil.which('node'), reason='node is not installed')
def test_the_still_is_taken_at_the_sensors_resolution_not_the_viewfinders(tmp_path):
    """
    The bug that made every server-side decode fail, run rather than grepped for.

    `ImageCapture.takePhoto` is how a still is supposed to be taken at full sensor
    resolution, and WebKit has never shipped it - so on every iPhone `capture()` fell
    through to drawing the viewfinder instead. A viewfinder is a preview stream sized
    for smooth playback, routinely 720x1280, and a receipt's QR code in one is about
    sixty pixels across: under two pixels a module, which no decoder on either side of
    the wire can read. That is the whole reason a server-side scan had never once
    succeeded, and no amount of preprocessing in utils/qr.py could have changed it.

    So the track is raised to its maximum for the shutter and put back afterwards, and
    all three of those halves matter: raised, or the upload is unreadable; drawn after
    the camera has actually switched, or the frame is the old size anyway; restored, or
    the preview decode loop runs at full sensor resolution and cooks the phone.
    """
    harness = tmp_path / 'capture.js'
    harness.write_text(CAPTURE_HARNESS)

    result = subprocess.run(
        ['node', str(harness), str(STATIC / 'js' / 'scanner.js')],
        capture_output=True, text=True, timeout=30)
    report = json.loads(result.stdout.strip().splitlines()[-1])

    assert 'error' not in report, report
    assert report['drew'] is True

    # Raised to the sensor's own maximum, then put back to what the preview was on.
    assert len(report['asked']) == 2, report['asked']
    assert report['asked'][0] == {'height': {'ideal': 3840}}
    assert report['asked'][1] == {'height': {'ideal': 1280}}

    # And the frame was taken after the camera had switched, not before.
    assert report['captured'] == '2160x3840'


@pytest.mark.skipif(not shutil.which('node'), reason='node is not installed')
def test_a_camera_that_will_not_change_mode_still_yields_a_photograph(tmp_path):
    """
    Every step of the raise is allowed to fail into the old behaviour.

    A camera that reports no capabilities, or refuses the constraint, must still produce
    the viewfinder frame - which is exactly what this used to do unconditionally, and is
    worth having over an error in front of someone holding a receipt.
    """
    harness = tmp_path / 'capture-refuses.js'
    harness.write_text(CAPTURE_HARNESS.replace(
        'getCapabilities: () => ({ width: { max: 2160 }, height: { max: 3840 } }),',
        'getCapabilities: () => { throw new Error("not supported"); },'))

    result = subprocess.run(
        ['node', str(harness), str(STATIC / 'js' / 'scanner.js')],
        capture_output=True, text=True, timeout=30)
    report = json.loads(result.stdout.strip().splitlines()[-1])

    assert 'error' not in report, report
    assert report['drew'] is True
    assert report['asked'] == [], 'a camera that reports nothing must not be reconfigured'
    assert report['captured'] == '720x1280'


def test_the_camera_is_asked_for_a_long_edge_rather_than_a_landscape_frame():
    """
    Where the 720x1280 came from.

    `width: 1920, height: 1080` describes a landscape stream. A phone held upright
    produces a portrait one, and a browser resolving that pair by aspect ratio hands
    back the nearest mode it has - routinely 720p - from a camera holding twelve
    megapixels. And because `ideal` obliges nobody, the request has to be made a second
    time once the track's real capabilities can be read.
    """
    scanner = (STATIC / 'js' / 'scanner.js').read_text()
    constraints = re.search(r'var CAMERA_CONSTRAINTS = \{(.*?)\n    \};', scanner, re.S).group(1)

    assert 'width:' not in constraints, 'a width pins the frame to landscape'
    assert 'aspectRatio' in constraints and 'height:' in constraints

    # And asked again with the answer in hand, on every path that starts a camera.
    assert len(re.findall(r'await raiseResolution\(track\)', scanner)) == 2


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
