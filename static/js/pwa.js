/*
 * pwa.js - the scanner's offline spine.
 *
 * The app is written as if the network does not exist. A scan is written to IndexedDB
 * and the UI is done; a separate loop drains that queue whenever the server happens to
 * be reachable. Nothing the field user does ever waits on a request, because in the
 * places this runs the request frequently never finishes.
 *
 * Three things here are less obvious than they look:
 *
 *   - navigator.onLine is not a network test. On captive Wi-Fi, on a hotel portal, or
 *     behind a router that is up but not routing, it reports true and every fetch hangs
 *     until the browser's own timeout, which is far longer than a person will wait. So
 *     every request has an explicit AbortController deadline, and repeated failures
 *     open a circuit breaker that stops us dialling for a while.
 *
 *   - IndexedDB transactions auto-commit as soon as the microtask queue drains. Awaiting
 *     anything that is not an IDB request inside a transaction - a fetch, a timer -
 *     silently ends it, and the writes after that point are lost without an error. So
 *     network work is never done inside a transaction.
 *
 *   - A 401 must never delete the outbox. Receipts that have not reached the server are
 *     the only copy in existence; the session is replaceable and they are not.
 */
(function (global) {
    'use strict';

    var DB_NAME = 'taxconsult-pwa-db';
    var DB_VERSION = 1;

    var SESSION_KEY = 'taxconsult.activeSession';
    var NET_TIMEOUT_MS = 8000;      // Interactive reads.
    var SYNC_TIMEOUT_MS = 20000;    // Background writes, which carry photos.
    // Activation gets its own, much longer budget. It is one-shot and it is the only
    // thing standing between a field user and a working app, so it must not be given
    // the deadline meant for a read that can simply be tried again later. It also
    // happens on a cold cache, when the connection is at its busiest.
    var ACTIVATE_TIMEOUT_MS = 45000;
    var CIRCUIT_OPEN_MS = 15000;    // Stop dialling for this long after a hard failure.
    var SYNC_INTERVAL_MS = 60000;
    var URL_BATCH_SIZE = 50;        // Matches SCAN_HISTORY_PAGE_SIZE on the server.

    // A scan that has failed this many times is parked rather than retried forever. It
    // stays in the outbox and stays visible; it just stops consuming the sync loop.
    var MAX_ATTEMPTS = 8;

    // How long to wait for indexedDB.open() before treating storage as unavailable.
    // Generous for a local database, but it is a deadline rather than an expectation:
    // the case it exists for is an open that never answers at all.
    var DB_OPEN_TIMEOUT_MS = 4000;

    var dbPromise = null;
    var dbUnavailable = false;
    var serverDownUntil = 0;
    var syncing = false;
    var listeners = [];

    // ---------------------------------------------------------------- storage

    /*
     * Opens the database, or gives up.
     *
     * indexedDB.open() is not reliably a promise that settles. WebKit has a long
     * standing bug where it fires no event at all - not success, not error, not
     * blocked - most often on the first access after a page load or a restore from
     * the back/forward cache. An `await` on that never returns and never throws, so a
     * try/catch around it is no protection: the caller simply stops, forever.
     *
     * That is not a theoretical failure. It stranded activation on "Activating…" with
     * the session already saved and the token already spent.
     *
     * So the open is raced against a deadline and the result is latched. Callers get a
     * rejection they can handle instead of a promise that never comes back.
     */
    function getDB() {
        if (dbUnavailable) return Promise.reject(new Error('storage-unavailable'));
        if (dbPromise) return dbPromise;

        dbPromise = new Promise(function (resolve, reject) {
            var settled = false;

            function fail(error) {
                if (settled) return;
                settled = true;
                clearTimeout(timer);
                dbUnavailable = true;
                dbPromise = null;
                reject(error);
            }

            var timer = setTimeout(function () {
                fail(new Error('storage-unavailable'));
            }, DB_OPEN_TIMEOUT_MS);

            var opening;
            try {
                opening = idb.openDB(DB_NAME, DB_VERSION, {
                    upgrade: function (db) {
                        if (!db.objectStoreNames.contains('outbox')) {
                            // Keyed on the uuid the phone minted, which is also the server's
                            // idempotency key - so a scan has one identity from the moment it
                            // exists, before it has ever been near a network.
                            var outbox = db.createObjectStore('outbox', { keyPath: 'client_uuid' });
                            outbox.createIndex('by-captured', 'captured_at');
                        }
                        if (!db.objectStoreNames.contains('submissions')) {
                            var subs = db.createObjectStore('submissions', { keyPath: 'id' });
                            subs.createIndex('by-received', 'received_at');
                            subs.createIndex('by-client-uuid', 'client_uuid');
                        }
                        if (!db.objectStoreNames.contains('meta')) {
                            db.createObjectStore('meta', { keyPath: 'key' });
                        }
                    },
                    // Another tab is holding the old version open. Left unhandled this is
                    // the other way an open silently never completes.
                    blocked: function () { fail(new Error('storage-blocked')); },
                    // This tab is now the one in the way. Close so the other can upgrade.
                    blocking: function (currentVersion, blockedVersion, event) {
                        try { event.target.close(); } catch (e) { /* already gone */ }
                        dbPromise = null;
                    },
                });
            } catch (e) {
                fail(e);
                return;
            }

            opening.then(function (db) {
                if (settled) {
                    // The watchdog already gave up; do not leak the connection.
                    try { db.close(); } catch (e) { /* nothing to do */ }
                    return;
                }
                settled = true;
                clearTimeout(timer);
                resolve(db);
            }, fail);
        });

        return dbPromise;
    }

    /* Lets the diagnostics page's repair button try storage again after a failure. */
    function resetStorage() {
        dbUnavailable = false;
        dbPromise = null;
    }

    async function metaGet(key, fallback) {
        var db = await getDB();
        var row = await db.get('meta', key);
        return row ? row.value : fallback;
    }

    async function metaSet(key, value) {
        var db = await getDB();
        await db.put('meta', { key: key, value: value });
        return value;
    }

    // ---------------------------------------------------------------- session
    //
    // Held in localStorage rather than sessionStorage because mobile operating systems
    // discard background tabs aggressively, and a field user who loses their session
    // every time they take a phone call is a field user who stops using the app.

    function loadSession() {
        try {
            var raw = global.localStorage.getItem(SESSION_KEY);
            return raw ? JSON.parse(raw) : null;
        } catch (e) {
            return null;
        }
    }

    function saveSession(session) {
        try {
            global.localStorage.setItem(SESSION_KEY, JSON.stringify(session));
        } catch (e) { /* private mode; the app still works for this session */ }
        // Mirrored into IndexedDB, which is the one store a service worker can read.
        // localStorage is not visible outside a document, and the whole point of the
        // background-sync path below is to send an outbox the document is not open to
        // watch.
        metaSet('sessionToken', session && session.token ? session.token : null).catch(function () {});
        return session;
    }

    function clearSession() {
        try {
            global.localStorage.removeItem(SESSION_KEY);
        } catch (e) { /* nothing to do */ }
        metaSet('sessionToken', null).catch(function () {});
    }

    function sessionToken() {
        var s = loadSession();
        return s && s.token ? s.token : null;
    }

    // ---------------------------------------------------------------- network

    function markServerDown() { serverDownUntil = Date.now() + CIRCUIT_OPEN_MS; }
    function markServerUp() { serverDownUntil = 0; }

    function serverLikelyReachable() {
        if (global.navigator.onLine === false) return false;
        return Date.now() >= serverDownUntil;
    }

    async function apiFetch(url, options, settings) {
        options = options || {};
        settings = settings || {};
        var timeout = settings.timeout || NET_TIMEOUT_MS;

        if (!settings.force && !serverLikelyReachable()) {
            throw netError('offline', 'No connection.');
        }

        var controller = new AbortController();
        var timedOut = false;
        var timer = setTimeout(function () { timedOut = true; controller.abort(); }, timeout);
        var headers = Object.assign({}, options.headers);
        var token = sessionToken();
        if (token && !headers.Authorization) headers.Authorization = 'Bearer ' + token;

        try {
            var response = await fetch(url, Object.assign({}, options, {
                headers: headers,
                signal: controller.signal,
                credentials: 'same-origin',
                // Lets the request outlive the page. Set only by the flush that fires
                // when the app is about to go to the background - everywhere else a
                // dead page should abort its own requests.
                keepalive: !!settings.keepalive,
            }));
            markServerUp();
            return response;
        } catch (e) {
            markServerDown();
            // Distinguished rather than merged. "The server took too long" and "there is
            // no connection" call for different things from the person holding the
            // phone, and reporting both as the latter sends them to go and find better
            // signal they may already have.
            if (timedOut) {
                throw netError('timeout', 'The server took too long to reply (' + Math.round(timeout / 1000) + 's).');
            }
            throw netError('network', 'Could not reach the server.');
        } finally {
            clearTimeout(timer);
        }
    }

    function netError(kind, message) {
        var error = new Error(message);
        error.kind = kind;
        return error;
    }

    /*
     * A 401 ends the session but never the outbox.
     *
     * Returns the reason the server gave, so the UI can say "this device was activated
     * on another phone" instead of the useless "unauthorised".
     */
    async function handleAuthFailure(response) {
        if (!response || response.status !== 401) return null;
        var reason = 'unknown';
        var message = '';
        try {
            var body = await response.clone().json();
            reason = body.reason || reason;
            message = body.error || '';
        } catch (e) { /* keep the defaults */ }

        clearSession();
        await metaSet('signOutReason', { reason: reason, message: message, at: Date.now() });
        emit();
        return reason;
    }

    // ---------------------------------------------------------------- outbox

    function newUuid() {
        if (global.crypto && global.crypto.randomUUID) return global.crypto.randomUUID();
        // Not cryptographic; it only has to be unique across one phone's queue.
        return 'x-' + Date.now().toString(36) + '-' + Math.random().toString(36).slice(2, 10);
    }

    /*
     * Records a scan. Returns as soon as it is durable on the device - deliberately
     * without waiting for, or even checking, the network.
     */
    async function queueScan(fields) {
        var entry = {
            client_uuid: newUuid(),
            kind: fields.photo ? 'photo' : 'url',
            receipturl: fields.receipturl || null,
            photo: fields.photo || null,
            description: fields.description || null,
            location: fields.location || null,
            captured_at: new Date().toISOString(),
            attempts: 0,
            last_error: null,
            submission_id: null,
        };

        var db = await getDB();
        await db.put('outbox', entry);
        emit();

        // Fire and forget: a failure here is not this call's problem, the sync loop
        // will come back to it. Background Sync is asked for alongside it rather than
        // instead of it, so a phone that goes straight into a pocket after this scan
        // still has a chance of sending it without anyone reopening the app.
        sync().catch(function () {});
        requestBackgroundSync();
        return entry;
    }

    async function outboxAll() {
        var db = await getDB();
        return db.getAll('outbox');
    }

    async function outboxCount() {
        var db = await getDB();
        return db.count('outbox');
    }

    async function discard(clientUuid) {
        var db = await getDB();
        await db.delete('outbox', clientUuid);
        emit();
    }

    async function noteAttempt(entry, error) {
        var db = await getDB();
        var current = await db.get('outbox', entry.client_uuid);
        if (!current) return;
        current.attempts = (current.attempts || 0) + 1;
        current.last_error = error ? String(error).slice(0, 200) : null;
        await db.put('outbox', current);
    }

    // ---------------------------------------------------------------- sync

    /*
     * Drains the outbox.
     *
     * URL scans go up as one batch - they are a few dozen bytes each and the common
     * case is a handful of them. Photos go one request at a time, because a
     * multi-megabyte upload that fails on a weak connection must not take the rest of
     * the queue down with it.
     */
    async function sync(settings) {
        settings = settings || {};
        if (syncing) return { skipped: 'already-running' };
        if (!sessionToken()) return { skipped: 'no-session' };
        if (!settings.force && !serverLikelyReachable()) return { skipped: 'offline' };

        syncing = true;
        emit();
        var sent = 0, failed = 0;

        try {
            var queued;
            try {
                queued = await outboxAll();
            } catch (e) {
                // No queue to drain is not a sync failure; it is a storage failure, and
                // the diagnostics screen is where that gets reported.
                return { skipped: 'storage-unavailable' };
            }
            var pending = queued.filter(function (e) {
                return (e.attempts || 0) < MAX_ATTEMPTS;
            });

            var urls = pending.filter(function (e) { return e.kind === 'url'; });
            var photos = pending.filter(function (e) { return e.kind === 'photo'; });

            for (var i = 0; i < urls.length; i += URL_BATCH_SIZE) {
                var batch = urls.slice(i, i + URL_BATCH_SIZE);
                var result = await syncUrlBatch(batch);
                sent += result.sent;
                failed += result.failed;
                if (result.stop) return finish(sent, failed);
            }

            for (var j = 0; j < photos.length; j++) {
                var photoResult = await syncPhoto(photos[j]);
                sent += photoResult.sent;
                failed += photoResult.failed;
                if (photoResult.stop) return finish(sent, failed);
            }

            if (sent > 0 || settings.refresh) await pullHistory();
            return finish(sent, failed);
        } finally {
            syncing = false;
            emit();
        }

        function finish(s, f) { return { sent: s, failed: f }; }
    }

    async function syncUrlBatch(batch) {
        var payload = {
            items: batch.map(function (e) {
                return {
                    client_uuid: e.client_uuid,
                    receipturl: e.receipturl,
                    description: e.description,
                    location: e.location,
                    captured_at: e.captured_at,
                };
            }),
        };

        var response;
        try {
            response = await apiFetch('/scan/api/sync', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify(payload),
            }, { timeout: SYNC_TIMEOUT_MS });
        } catch (e) {
            for (var k = 0; k < batch.length; k++) await noteAttempt(batch[k], e.message);
            return { sent: 0, failed: batch.length, stop: true };
        }

        if (response.status === 401) {
            await handleAuthFailure(response);
            return { sent: 0, failed: batch.length, stop: true };
        }
        if (!response.ok) {
            for (var m = 0; m < batch.length; m++) await noteAttempt(batch[m], 'HTTP ' + response.status);
            return { sent: 0, failed: batch.length, stop: response.status >= 500 };
        }

        var body = await response.json();
        var byUuid = {};
        (body.results || []).forEach(function (r) { byUuid[r.client_uuid] = r; });

        var sent = 0, failed = 0;
        for (var n = 0; n < batch.length; n++) {
            var entry = batch[n];
            var res = byUuid[entry.client_uuid];
            // 'duplicate' means the server already had this exact scan, which is just as
            // final as 'accepted' - either way it is safe to stop holding it here.
            if (res && (res.status === 'accepted' || res.status === 'duplicate')) {
                await discard(entry.client_uuid);
                sent += 1;
            } else {
                await noteAttempt(entry, res ? res.error : 'no result returned');
                failed += 1;
            }
        }
        emit();
        return { sent: sent, failed: failed, stop: false };
    }

    async function syncPhoto(entry) {
        if (!entry.photo) {
            await discard(entry.client_uuid);
            return { sent: 0, failed: 0 };
        }

        var form = new FormData();
        form.append('receiptphoto', entry.photo, (entry.client_uuid || 'receipt') + '.jpg');
        form.append('client_uuid', entry.client_uuid);
        form.append('captured_at', entry.captured_at || '');
        if (entry.description) form.append('description', entry.description);
        if (entry.location) form.append('location', entry.location);

        var response;
        try {
            response = await apiFetch('/scan/api/sync/photo', { method: 'POST', body: form },
                                      { timeout: SYNC_TIMEOUT_MS });
        } catch (e) {
            await noteAttempt(entry, e.message);
            return { sent: 0, failed: 1, stop: true };
        }

        if (response.status === 401) {
            await handleAuthFailure(response);
            return { sent: 0, failed: 1, stop: true };
        }
        if (!response.ok) {
            await noteAttempt(entry, 'HTTP ' + response.status);
            return { sent: 0, failed: 1, stop: response.status >= 500 };
        }

        await discard(entry.client_uuid);
        emit();
        return { sent: 1, failed: 0, stop: false };
    }

    // ---------------------------------------------------------------- history

    /*
     * Pulls this device's submissions and caches them so the history screen reads the
     * same whether or not there is a network.
     */
    async function pullHistory() {
        if (!sessionToken()) return [];

        var response;
        try {
            response = await apiFetch('/scan/api/submissions');
        } catch (e) {
            return cachedHistory();
        }
        if (response.status === 401) {
            await handleAuthFailure(response);
            return cachedHistory();
        }
        if (!response.ok) return cachedHistory();

        var body = await response.json();
        var rows = body.submissions || [];

        var db = await getDB();
        // Replaced wholesale rather than merged: the server's list is the truth, and a
        // row that has disappeared from it should disappear here too.
        var tx = db.transaction('submissions', 'readwrite');
        await tx.store.clear();
        for (var i = 0; i < rows.length; i++) tx.store.put(rows[i]);
        await tx.done;

        await metaSet('historySyncedAt', Date.now());
        emit();
        return rows;
    }

    async function cachedHistory() {
        try {
            var db = await getDB();
            var rows = await db.getAll('submissions');
            return rows.sort(function (a, b) {
                return (b.received_at || '').localeCompare(a.received_at || '');
            });
        } catch (e) {
            return [];
        }
    }

    async function retrySubmission(id) {
        var response = await apiFetch('/scan/api/submissions/' + id + '/retry', { method: 'POST' });
        if (response.status === 401) {
            await handleAuthFailure(response);
            return false;
        }
        if (!response.ok) return false;
        await pullHistory();
        return true;
    }

    // ---------------------------------------------------------------- activation

    /*
     * Spends an activation token and opens a session.
     *
     * Never throws: every failure comes back as { ok: false, error, reason } so the
     * caller does not have to guess what an exception meant. That mattered - a blanket
     * catch around this reported a timeout as "check your connection", which is advice
     * for a problem the user did not have.
     */
    async function activate(token) {
        var response;
        try {
            response = await apiFetch('/scan/api/activate', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ token: token }),
            }, { force: true, timeout: ACTIVATE_TIMEOUT_MS });
        } catch (e) {
            return { ok: false, reason: e.kind || 'network', error: e.message };
        }

        var body = await response.json().catch(function () { return {}; });
        if (!response.ok) {
            return { ok: false, error: body.error || 'Activation failed.', reason: body.reason };
        }
        if (!body.session_token) {
            return { ok: false, reason: 'network', error: 'The server sent an incomplete reply.' };
        }

        // The token has now been spent server-side, so from here on the activation has
        // happened whatever else goes wrong. Anything below that fails must not be
        // reported as failure: telling the user to try again would send them back to a
        // link that is legitimately dead, needing an admin to issue another.
        saveSession({ token: body.session_token, device: body.device, at: Date.now() });

        // Everything from here is best-effort and deliberately fenced off.
        //
        // The activation has already happened; the token cannot be spent twice. So no
        // failure below may reach the caller - not a rejection, not a synchronous throw,
        // and above all not a promise that never settles. Any of those leaves the user
        // looking at "Activating…" forever while holding a link that is now dead.
        try {
            // Not awaited: the session is in localStorage, which is what being activated
            // actually means. IndexedDB holds only the note about why we were last
            // signed out, and its open() is exactly the call that can hang.
            metaSet('signOutReason', null).catch(function () {});
            emit();
            // Now that there is something to be offline *for*, install the app shell.
            // Doing this earlier is what starved the activation request itself.
            registerServiceWorker().then(topUpShellCache);
            // Anything queued before activation - or left over from the previous session
            // - belongs to this device too, so send it now.
            sync({ force: true }).catch(function () {});
        } catch (e) { /* activated regardless; the app will catch up on next load */ }

        return { ok: true, device: body.device };
    }

    /*
     * Reads an activation token out of a scanned QR code, if that is what it is.
     *
     * Only same-origin URLs count: pointing the camera at some other site's QR code
     * must never be able to steer this app at a different server.
     */
    function activationTokenFrom(text) {
        if (!text) return null;
        var url;
        try {
            url = new URL(text, global.location.origin);
        } catch (e) {
            return null;
        }
        if (url.origin !== global.location.origin) return null;
        var match = url.pathname.match(/^\/scan\/a\/([^/]+)\/?$/);
        return match ? decodeURIComponent(match[1]) : null;
    }

    // ---------------------------------------------------------------- geolocation
    //
    // Started early and cached. GPS works with no network at all, but a fix can take
    // several seconds, and a receipt must never wait on one.

    var lastFix = null;

    function watchLocation() {
        if (!global.navigator.geolocation) return;
        global.navigator.geolocation.getCurrentPosition(
            function (pos) {
                lastFix = pos.coords.latitude.toFixed(6) + ',' + pos.coords.longitude.toFixed(6);
                metaSet('lastFix', lastFix).catch(function () {});
            },
            function () { /* denied or unavailable; submissions simply carry no location */ },
            { enableHighAccuracy: false, maximumAge: 120000, timeout: 5000 }
        );
    }

    function currentLocation() { return lastFix; }

    // ---------------------------------------------------------------- service worker

    /*
     * Never throws and never rejects; resolves to null when there is no worker.
     *
     * `'serviceWorker' in navigator` is not a sufficient test. Firefox in private
     * browsing, and any insecure context, leave the property present but undefined -
     * so the feature check passes and the call that follows throws synchronously. That
     * mattered because this runs immediately after a successful activation, where an
     * exception strands the caller with the token already spent.
     */
    function registerServiceWorker() {
        try {
            if (!global.navigator.serviceWorker) return Promise.resolve(null);
            return global.navigator.serviceWorker.register('/scan/sw.js', { scope: '/scan/' })
                .catch(function () { return null; });
        } catch (e) {
            return Promise.resolve(null);
        }
    }

    /*
     * Tops up anything the install missed.
     *
     * A first load on a weak connection can leave the precache half-filled, and without
     * this the app stays broken offline until the next version bump - which is exactly
     * the situation where nobody can reach a version bump.
     */
    function topUpShellCache() {
        try {
            if (!global.navigator.serviceWorker || !serverLikelyReachable()) return;
            global.navigator.serviceWorker.ready.then(function (reg) {
                if (reg && reg.active) reg.active.postMessage({ type: 'top-up-cache' });
            }).catch(function () {});
        } catch (e) { /* no worker here; the app still runs, just not offline */ }
    }

    /*
     * Asks the platform to wake the service worker and finish sending the outbox even
     * after this tab is gone - a screen locked in a pocket, the browser killed to free
     * memory, the phone genuinely put down mid-drive. `sync()` above only ever runs
     * while a document is open to call it; this is what covers the rest.
     *
     * Not every browser has Background Sync (notably Safari/iOS does not), so this is
     * best-effort and silently does nothing where it is unsupported - the interval
     * timer and the visibility/online listeners are what those platforms fall back to.
     * `.ready` never settling because no worker was ever registered is guarded with the
     * same kind of deadline used for indexedDB.open(), for the same reason: an awaited
     * promise that never resolves is worse than one that rejects.
     */
    function requestBackgroundSync() {
        try {
            if (!global.navigator.serviceWorker) return;
            var ready = global.navigator.serviceWorker.ready;
            var timeout = new Promise(function (resolve) { setTimeout(resolve, 3000, null); });
            Promise.race([ready, timeout]).then(function (reg) {
                if (reg && reg.sync) reg.sync.register('outbox-sync').catch(function () {});
            }).catch(function () {});
        } catch (e) { /* unsupported; the foreground sync paths still cover this device */ }
    }

    /*
     * The last thing done before this tab stops running - going to the background,
     * losing focus, or being torn down outright. A normal fetch would be cancelled the
     * moment the page unloads; `keepalive` is what lets the browser finish sending it
     * anyway. That budget is small (a low number of kilobytes), which is exactly what a
     * batch of receipt URLs is and exactly what a photo is not, so photos are left for
     * the interval timer, the next foreground, or Background Sync to pick up.
     *
     * Fire-and-forget on purpose: nothing here awaits the response or clears the
     * outbox, because the tab may not exist by the time one arrives. The entries stay
     * queued until an ordinary sync - on this device or, if it lands first, from the
     * service worker - confirms them and clears them for real.
     */
    function flushOnHide() {
        if (!sessionToken() || !serverLikelyReachable()) return;
        outboxAll().then(function (all) {
            var urls = all
                .filter(function (e) { return e.kind === 'url' && (e.attempts || 0) < MAX_ATTEMPTS; })
                .slice(0, URL_BATCH_SIZE);
            if (!urls.length) return;
            var payload = { items: urls.map(function (e) {
                return {
                    client_uuid: e.client_uuid, receipturl: e.receipturl,
                    description: e.description, location: e.location, captured_at: e.captured_at,
                };
            }) };
            apiFetch('/scan/api/sync', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify(payload),
            }, { timeout: 4000, keepalive: true }).catch(function () {});
        }).catch(function () {});
        requestBackgroundSync();
    }

    function requestPersistence() {
        if (!global.navigator.storage || !global.navigator.storage.persist) return;
        // Without this the OS may evict IndexedDB under memory pressure, taking unsent
        // receipts with it.
        global.navigator.storage.persisted().then(function (granted) {
            if (!granted) global.navigator.storage.persist();
        }).catch(function () {});
    }

    // ---------------------------------------------------------------- events

    function onChange(fn) { listeners.push(fn); return function () {
        listeners = listeners.filter(function (l) { return l !== fn; });
    }; }

    function emit() {
        for (var i = 0; i < listeners.length; i++) {
            try { listeners[i](); } catch (e) { /* a broken listener is not fatal */ }
        }
    }

    /*
     * Never rejects. Every screen refreshes off this, so a storage failure has to
     * degrade the numbers rather than break the page that reports them.
     */
    async function status() {
        var pending = 0;
        var signOutReason = null;
        var storageOk = true;

        try {
            pending = await outboxCount();
            signOutReason = await metaGet('signOutReason', null);
        } catch (e) {
            storageOk = false;
        }

        return {
            online: global.navigator.onLine !== false,
            reachable: serverLikelyReachable(),
            syncing: syncing,
            pending: pending,
            session: loadSession(),
            signOutReason: signOutReason,
            storageOk: storageOk,
        };
    }

    // ---------------------------------------------------------------- boot

    function start() {
        requestPersistence();
        // Deliberately not before there is a session.
        //
        // Installing the worker downloads roughly two megabytes of app shell across a
        // dozen requests. On a phone on mobile data that saturates the connection, and
        // an unactivated phone has exactly one request that matters - the activation
        // POST - which then queues behind all of it and times out. The app is no use
        // offline until it is activated anyway, so there is nothing to lose by waiting;
        // activate() installs the worker the moment a session exists.
        if (sessionToken()) registerServiceWorker().then(topUpShellCache);
        // Not started here. Geolocation is its own permission prompt, and history and
        // diagnostics - which also call start() - have no use for a location fix at
        // all. The scan screen, the one place that attaches one to a receipt, asks for
        // it itself once the camera is settled, so a cold launch does not stack two
        // permission prompts on top of each other.

        global.addEventListener('online', function () {
            markServerUp();
            if (sessionToken()) topUpShellCache();
            sync({ force: true }).catch(function () {});
            emit();
        });
        global.addEventListener('offline', emit);

        global.document.addEventListener('visibilitychange', function () {
            if (global.document.visibilityState === 'visible') {
                if (sessionToken()) topUpShellCache();
                sync().catch(function () {});
            } else {
                // Whoever is holding the phone may not open this tab again for hours -
                // driving, a queue, a shop floor. This is the last reliable moment to
                // get anything queued onto the wire before it does.
                flushOnHide();
            }
        });
        // The 'visibilitychange' above covers switching apps or locking the screen;
        // this covers the tab being closed or navigated away from outright, which on
        // some browsers fires pagehide without ever marking the document hidden first.
        global.addEventListener('pagehide', flushOnHide);

        setInterval(function () { sync().catch(function () {}); }, SYNC_INTERVAL_MS);
        sync({ refresh: true }).catch(function () {});
    }

    global.PWA = {
        // storage
        getDB: getDB, metaGet: metaGet, metaSet: metaSet, resetStorage: resetStorage,
        // session
        loadSession: loadSession, saveSession: saveSession, clearSession: clearSession,
        sessionToken: sessionToken, activate: activate, activationTokenFrom: activationTokenFrom,
        // queue
        queueScan: queueScan, outboxAll: outboxAll, outboxCount: outboxCount, discard: discard,
        // sync
        sync: sync, pullHistory: pullHistory, cachedHistory: cachedHistory,
        retrySubmission: retrySubmission, apiFetch: apiFetch,
        // misc
        currentLocation: currentLocation, watchLocation: watchLocation,
        status: status, onChange: onChange, emit: emit,
        topUpShellCache: topUpShellCache, registerServiceWorker: registerServiceWorker,
        requestBackgroundSync: requestBackgroundSync,
        serverLikelyReachable: serverLikelyReachable,
        newUuid: newUuid, start: start,
        MAX_ATTEMPTS: MAX_ATTEMPTS,
    };
})(window);
