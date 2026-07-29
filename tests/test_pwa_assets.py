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

    assert {'/scan/', '/scan/history', '/scan/diagnostics'} <= shell
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
    page = (ROOT / 'templates' / 'scan' / 'scanner.html').read_text()
    assert 'onEngineFailure' in page
    assert 'engineError' in page


def test_the_viewfinder_shows_what_will_actually_be_captured():
    """
    A QR box drawn over a full-frame photo tells the user to fit their receipt inside a
    crop that is never applied. The two modes capture different things and so must be
    framed differently.
    """
    css = (STATIC / 'css' / 'scan.css').read_text()
    page = (ROOT / 'templates' / 'scan' / 'scanner.html').read_text()

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

    # Registration in start() is conditional on already holding a session...
    assert 'if (sessionToken()) registerServiceWorker()' in pwa
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

    for template in ('activate.html', 'scanner.html'):
        source = (ROOT / 'templates' / 'scan' / template).read_text()
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
    body = pwa.split('function getDB()')[1].split('\n    /* Lets the diagnostics')[0]

    assert 'DB_OPEN_TIMEOUT_MS' in body          # raced against a deadline
    assert 'blocked:' in body                    # the other silent-stall path
    assert 'dbUnavailable = true' in body        # latched, so nothing waits twice


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

    diagnostics = (ROOT / 'templates' / 'scan' / 'diagnostics.html').read_text()
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
