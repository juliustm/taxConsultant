/*
 * service-worker.js - scope /scan/ only.
 *
 * The scope is the security boundary as much as the caching one. The admin dashboard,
 * /stream and /api/* sit outside it, so no bug in here can serve a stale VAT figure or
 * hold an event stream open. Everything this worker can touch is the field app, which
 * carries no data of its own.
 *
 * Bump CACHE_VERSION on any change to APP_SHELL or to the files it names. Old caches
 * are deleted on activate, so the bump is the whole upgrade mechanism.
 */
const CACHE_VERSION = 'v2';
const PRECACHE = `scan-precache-${CACHE_VERSION}`;
const RUNTIME = `scan-runtime-${CACHE_VERSION}`;

// Everything needed to open the app with the network switched off. If a URL here is
// wrong the install still succeeds - see precacheShell - but the app is degraded, so
// tests/test_pwa_assets.py checks every entry resolves.
const APP_SHELL = [
    '/scan/',
    '/scan/history',
    '/scan/diagnostics',
    '/scan/manifest.json',
    '/static/css/scan.css',
    '/static/js/pwa.js',
    '/static/js/scanner.js',
    '/static/js/vendor/tailwind.min.js',
    '/static/js/vendor/alpine.min.js',
    '/static/js/vendor/idb.umd.min.js',
    '/static/js/vendor/zxing-reader.js',
    '/static/js/vendor/zxing_reader.wasm',
    '/static/icons/icon-192.png',
    '/static/icons/icon-512.png',
    '/static/icons/icon-maskable-512.png',
];

// Navigations we will serve from cache. Anything else under /scan/ - an activation
// link, say - goes to the network and falls back to the scanner shell.
const CACHEABLE_NAVIGATIONS = new Set([
    '/scan/',
    '/scan/history',
    '/scan/diagnostics',
]);

const NAVIGATION_TIMEOUT_MS = 6000;

/*
 * Fills the precache, tolerating individual failures.
 *
 * cache.addAll() is all-or-nothing: one asset timing out on a weak connection throws
 * away every asset that did download, and the install fails. Fetching them one at a
 * time means a bad connection produces a partly-cached app that the top-up completes
 * later, rather than nothing at all.
 */
async function precacheShell({ missingOnly = false } = {}) {
    const cache = await caches.open(PRECACHE);
    return Promise.all(APP_SHELL.map(async (url) => {
        if (missingOnly && (await cache.match(url))) return true;
        try {
            const response = await fetch(url, { credentials: 'same-origin', cache: 'reload' });
            // A redirect means we asked for one thing and got another - usually a login
            // page. Caching that would pin the wrong document to a shell URL.
            if (!response || !response.ok || response.redirected) return false;
            await cache.put(url, response.clone());
            return true;
        } catch (e) {
            return false;
        }
    }));
}

self.addEventListener('install', (event) => {
    // No waiting: a phone that has just been handed a fix should get it now, and there
    // is no cross-tab state here that a mid-session swap could corrupt.
    event.waitUntil(precacheShell().then(() => self.skipWaiting()));
});

self.addEventListener('activate', (event) => {
    event.waitUntil((async () => {
        const names = await caches.keys();
        await Promise.all(
            names
                .filter((name) => name.startsWith('scan-') && name !== PRECACHE && name !== RUNTIME)
                .map((name) => caches.delete(name))
        );
        await self.clients.claim();
    })());
});

self.addEventListener('message', (event) => {
    const data = event.data || {};
    if (data.type === 'top-up-cache') {
        event.waitUntil(precacheShell({ missingOnly: true }));
    } else if (data.type === 'repair-cache') {
        // The diagnostics page's repair button: refetch everything, not just gaps.
        event.waitUntil(precacheShell());
    }
});

function shellFallback(pathname) {
    // History and diagnostics degrade to the scanner rather than to an error page -
    // scanning is what the app is for, and it is the one screen that works with
    // nothing cached but itself.
    if (pathname.startsWith('/scan/history')) return '/scan/history';
    if (pathname.startsWith('/scan/diagnostics')) return '/scan/diagnostics';
    return '/scan/';
}

/*
 * Navigations: cached shell first, fresh copy in the background.
 *
 * The user gets a screen immediately and the next launch gets the update. The network
 * leg is bounded because a captive portal will otherwise hold the request open long
 * past the point the person has given up.
 */
async function handleNavigation(request) {
    const url = new URL(request.url);
    const cache = await caches.open(PRECACHE);
    const cached = await cache.match(url.pathname);

    const network = (async () => {
        const controller = new AbortController();
        const timer = setTimeout(() => controller.abort(), NAVIGATION_TIMEOUT_MS);
        try {
            const response = await fetch(request, { signal: controller.signal });
            if (response && response.ok && !response.redirected
                && CACHEABLE_NAVIGATIONS.has(url.pathname)) {
                await cache.put(url.pathname, response.clone());
            }
            return response;
        } finally {
            clearTimeout(timer);
        }
    })();

    if (cached) {
        // Kept alive past the response so the revalidation actually completes.
        network.catch(() => {});
        return cached;
    }

    try {
        return await network;
    } catch (e) {
        const fallback = await cache.match(shellFallback(url.pathname));
        if (fallback) return fallback;
        return new Response(
            '<!doctype html><meta charset="utf-8"><title>Offline</title>'
            + '<body style="font:16px system-ui;padding:2rem;background:#000;color:#fff">'
            + '<h1>Offline</h1><p>This app has not finished installing. '
            + 'Reconnect once and it will work offline from then on.</p>',
            { status: 503, headers: { 'Content-Type': 'text/html; charset=utf-8' } }
        );
    }
}

async function handleStatic(request) {
    const cached = await caches.match(request);
    if (cached) return cached;
    const response = await fetch(request);
    if (response && response.ok) {
        const runtime = await caches.open(RUNTIME);
        runtime.put(request, response.clone());
    }
    return response;
}

self.addEventListener('fetch', (event) => {
    const request = event.request;
    if (request.method !== 'GET') return;

    const url = new URL(request.url);
    if (url.origin !== self.location.origin) return;

    // The API is the client's business: pwa.js owns retries, timeouts and the outbox,
    // and a cached write endpoint would be actively harmful.
    if (url.pathname.startsWith('/scan/api/')) return;
    // Never cache the worker itself; the server marks it no-store for the same reason.
    if (url.pathname === '/scan/sw.js') return;

    if (request.mode === 'navigate') {
        if (!url.pathname.startsWith('/scan/')) return;   // Admin app: leave it alone.
        event.respondWith(handleNavigation(request));
        return;
    }

    if (url.pathname.startsWith('/static/') || url.pathname === '/scan/manifest.json') {
        event.respondWith(handleStatic(request));
    }
});

/*
 * Sends whatever is in the outbox without a document open to ask for it.
 *
 * pwa.js's own sync() only ever runs while some /scan/ tab exists to call it, which
 * covers everything except the case this whole app is built around: a phone put away
 * mid-round, screen off, tab gone from memory. The Background Sync API is the
 * platform's answer - it wakes this worker, with its own battery/network budget,
 * whenever a tag registered while offline finally has a connection to run on.
 *
 * Deliberately narrower than the page's sync(): only URL-kind entries, because photos
 * are multi-megabyte and this worker has no ImageCapture stream to shrink them from -
 * they wait for a document to be open again, exactly as before this existed. Losing a
 * race with the page's own sync() over the same rows is harmless: the server keys on
 * client_uuid, so a row synced twice comes back 'duplicate' the second time and is
 * simply dropped here too.
 *
 * DB_NAME/DB_VERSION/the store shapes/MAX_ATTEMPTS mirror pwa.js exactly. This worker
 * cannot `import` that file - it is written against `window`, which does not exist
 * here - so the two must be kept in step by hand.
 *
 * OUTBOX_DB_NAME keeps its pre-rename 'taxconsult-...' value deliberately - see the
 * matching note by DB_NAME in pwa.js. It must stay byte-identical to that constant.
 */
const OUTBOX_DB_NAME = 'taxconsult-pwa-db';
const OUTBOX_DB_VERSION = 1;
const OUTBOX_MAX_ATTEMPTS = 8;
const OUTBOX_BATCH_SIZE = 50;
const OUTBOX_SYNC_TAG = 'outbox-sync';

async function syncOutboxInBackground() {
    let db;
    try {
        importScripts('/static/js/vendor/idb.umd.min.js');
        db = await idb.openDB(OUTBOX_DB_NAME, OUTBOX_DB_VERSION);
    } catch (e) {
        // The page's own sync paths will catch this up once one is open again.
        return;
    }

    try {
        const tokenRow = await db.get('meta', 'sessionToken');
        const token = tokenRow && tokenRow.value;
        if (!token) return;

        const all = await db.getAll('outbox');
        const pending = all
            .filter((e) => e.kind === 'url' && (e.attempts || 0) < OUTBOX_MAX_ATTEMPTS)
            .slice(0, OUTBOX_BATCH_SIZE);
        if (!pending.length) return;

        const response = await fetch('/scan/api/sync', {
            method: 'POST',
            credentials: 'same-origin',
            headers: { 'Content-Type': 'application/json', 'Authorization': `Bearer ${token}` },
            body: JSON.stringify({
                items: pending.map((e) => ({
                    client_uuid: e.client_uuid, receipturl: e.receipturl,
                    description: e.description, location: e.location, captured_at: e.captured_at,
                })),
            }),
        });

        // A rejected promise here (network failure, not an HTTP error status) is what
        // tells the Background Sync API to retry this tag later with its own backoff -
        // swallowing it would silently drop a receipt that never actually sent.
        if (!response.ok && response.status < 500) return;   // Not going to succeed on retry.
        if (!response.ok) throw new Error(`sync failed: ${response.status}`);

        const body = await response.json();
        const tx = db.transaction('outbox', 'readwrite');
        for (const result of body.results || []) {
            if (result.status === 'accepted' || result.status === 'duplicate') {
                tx.store.delete(result.client_uuid);
            }
        }
        await tx.done;
    } finally {
        db.close();
    }
}

self.addEventListener('sync', (event) => {
    if (event.tag === OUTBOX_SYNC_TAG) event.waitUntil(syncOutboxInBackground());
});
