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

    // Kept as 'taxconsult-...' on purpose, even after the app's rename to Karani:
    // this is the actual IndexedDB name and localStorage key already sitting on
    // phones. Changing either orphans a real, already-activated device - a fresh
    // empty DB in the DB_NAME case, a silent sign-out in the SESSION_KEY case.
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

    // How many times one photo may be re-encoded smaller and sent again inside a single
    // sync, when the server refuses it as too large. Two is enough to cross any real
    // body limit from any real phone photograph - each round is a substantial step down
    // the ladder in Scanner.shrinkToFit - and it bounds what a stuck receipt can cost
    // the queue behind it.
    var PHOTO_SHRINK_ROUNDS = 2;

    // What the server has been found to refuse, and when. See shrinkAfterRefusal.
    var UPLOAD_CEILING_KEY = 'uploadCeilingBytes';
    // A week. The ceiling is a guess about somebody else's proxy configuration, and the
    // real fix for a 413 is raising that limit - so the guess has to be able to expire,
    // or every phone in the field would go on shrinking for a wall that is no longer
    // there.
    var UPLOAD_CEILING_TTL_MS = 7 * 24 * 60 * 60 * 1000;
    // The smallest a receipt photograph is worth sending. Under about a quarter of a
    // megabyte the printed verification code stops being legible to the vision model,
    // and an unreadable upload is not a rescue - see the ladder's floor in
    // Scanner.shrinkToFit.
    var UPLOAD_FLOOR_BYTES = 240 * 1024;

    // How long to wait for indexedDB.open() before treating storage as unavailable.
    // Generous for a local database, but it is a deadline rather than an expectation:
    // the case it exists for is an open that never answers at all.
    var DB_OPEN_TIMEOUT_MS = 4000;
    // Same idea, for a single read/write once the database is already open. getDB()
    // guards the open; this guards the transaction after it - WebKit's hang is not
    // only reported on open, and a scan that cannot be confirmed or denied within a
    // few seconds is one the person holding the phone needs to hear about, not one
    // that leaves "Saving…" on screen forever.
    var DB_OP_TIMEOUT_MS = 5000;
    // How long a failed open suppresses further attempts. Deliberately short: the
    // failures this guards against are transient (a bfcache restore, a moment of
    // contention), so the old behaviour - latching storage off for the entire life of
    // the page and demanding a visit to the diagnostics screen - turned a hiccup into
    // an app that could not save a receipt again until it was reloaded. Long enough
    // that a tight loop cannot spend its life waiting on 4-second opens; short enough
    // that the next thing the user does retries for real.
    var DB_RETRY_AFTER_MS = 2000;

    var dbPromise = null;
    var dbRetryAfter = 0;
    var dbLastError = null;
    var serverDownUntil = 0;
    var syncing = false;
    var listeners = [];

    // ---------------------------------------------------------------- storage

    // Every store this app needs. Named once so both the creator below and the
    // integrity check in getDB() work from the same list; a store that is missing from
    // an *existing* database is not a theoretical condition - see repairSchema.
    var DB_STORES = ['outbox', 'submissions', 'meta'];

    function createStores(db) {
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
    }

    function missingStores(db) {
        return DB_STORES.filter(function (name) {
            return !db.objectStoreNames.contains(name);
        });
    }

    /*
     * One open attempt, raced against a deadline.
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
     * So the open is raced against a deadline; callers get a rejection they can handle
     * instead of a promise that never comes back.
     */
    function openWithDeadline(version) {
        return new Promise(function (resolve, reject) {
            var settled = false;

            function fail(error) {
                if (settled) return;
                settled = true;
                clearTimeout(timer);
                reject(error);
            }

            var timer = setTimeout(function () {
                fail(new Error('storage-unavailable'));
            }, DB_OPEN_TIMEOUT_MS);

            var opening;
            try {
                opening = idb.openDB(DB_NAME, version, {
                    upgrade: createStores,
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
    }

    /*
     * Recreates stores that are missing from a database that already exists.
     *
     * An open at DB_VERSION only runs `upgrade` when the stored version is older, so a
     * database that is already at DB_VERSION but has no object stores in it opens
     * perfectly happily and then fails every single read and write with NotFoundError -
     * permanently, because nothing will ever trigger the upgrade that would fix it.
     *
     * That state was reachable, and phones are in it now: the service worker used to
     * open this database with no upgrade callback at all (see its own note), and
     * whichever of the two got there first on a device whose storage had just been
     * evicted decided the schema. If the worker won, it created version 1 with nothing
     * in it and the app could not save a receipt again, on that phone, ever.
     *
     * Bumping the version is the only thing that runs an upgrade, so that is what this
     * does. Existing stores are left alone by createStores, so a database that is
     * merely missing one of them keeps everything already queued in the others.
     */
    async function repairSchema(db) {
        var version = db.version + 1;
        try { db.close(); } catch (e) { /* already gone */ }
        var repaired = await openWithDeadline(version);
        if (missingStores(repaired).length) {
            try { repaired.close(); } catch (e) { /* already gone */ }
            throw new Error('storage-unavailable');
        }
        return repaired;
    }

    /*
     * The open database, opening it and repairing its schema if need be.
     *
     * Failures are remembered only briefly (DB_RETRY_AFTER_MS) rather than latched for
     * the life of the page, so that the next thing the user does is a real retry.
     */
    function getDB() {
        if (dbPromise) return dbPromise;
        if (Date.now() < dbRetryAfter) {
            return Promise.reject(dbLastError || new Error('storage-unavailable'));
        }

        var attempt = (async function () {
            var db = await openWithDeadline(DB_VERSION);
            return missingStores(db).length ? repairSchema(db) : db;
        })();

        dbPromise = attempt;
        attempt.then(function () {
            dbLastError = null;
        }, function (error) {
            // Cleared so the next call opens again rather than re-awaiting a rejection.
            if (dbPromise === attempt) dbPromise = null;
            dbLastError = error;
            dbRetryAfter = Date.now() + DB_RETRY_AFTER_MS;
        });

        return attempt;
    }

    /* Lets the diagnostics page's repair button try storage again immediately. */
    function resetStorage() {
        dbPromise = null;
        dbRetryAfter = 0;
        dbLastError = null;
    }

    /*
     * Races any promise against a deadline, rejecting rather than leaving a caller
     * awaiting something that may never settle.
     *
     * getDB() already does this for the open itself; this covers what comes after -
     * an individual get/put/getAll can wedge the same way on the same platforms, and
     * a caller `await`-ing one directly has no way to notice until the browser tab
     * is killed. Not used to abort the underlying IDB request (IndexedDB has no such
     * thing); a request left running behind a timed-out caller is harmless since
     * nothing reads its result.
     */
    function withDeadline(promise, ms, message) {
        return new Promise(function (resolve, reject) {
            var settled = false;
            var timer = setTimeout(function () {
                if (settled) return;
                settled = true;
                reject(new Error(message));
            }, ms);
            promise.then(function (value) {
                if (settled) return;
                settled = true;
                clearTimeout(timer);
                resolve(value);
            }, function (error) {
                if (settled) return;
                settled = true;
                clearTimeout(timer);
                reject(error);
            });
        });
    }

    async function metaGet(key, fallback) {
        var db = await getDB();
        var row = await withDeadline(db.get('meta', key), DB_OP_TIMEOUT_MS, 'Reading local storage timed out.');
        return row ? row.value : fallback;
    }

    async function metaSet(key, value) {
        var db = await getDB();
        await withDeadline(db.put('meta', { key: key, value: value }), DB_OP_TIMEOUT_MS, 'Writing to local storage timed out.');
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
            // Which door it goes through, not what it is. An entry carrying a photograph
            // goes up as multipart whether or not it also carries a decoded URL - the
            // JSON batch has no way to send bytes - and the server works out from the
            // pair that the URL is the input and the picture is evidence.
            kind: fields.photo ? 'photo' : 'url',
            receipturl: fields.receipturl || null,
            photo: fields.photo || null,
            description: fields.description || null,
            location: fields.location || null,
            // Normally now, because normally the receipt is in front of the camera. A
            // photo imported from the gallery passes the date the picture was taken
            // instead: a receipt from last week belongs in last week, and 'when the
            // phone got round to uploading it' is not a date anyone can file on.
            captured_at: fields.captured_at || new Date().toISOString(),
            attempts: 0,
            last_error: null,
            submission_id: null,
        };

        var db = await getDB();
        await withDeadline(db.put('outbox', entry), DB_OP_TIMEOUT_MS,
            'Saving to this phone is taking too long.');
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
        return withDeadline(db.getAll('outbox'), DB_OP_TIMEOUT_MS, 'Reading the outbox timed out.');
    }

    async function outboxCount() {
        var db = await getDB();
        return withDeadline(db.count('outbox'), DB_OP_TIMEOUT_MS, 'Reading the outbox timed out.');
    }

    async function discard(clientUuid) {
        var db = await getDB();
        await withDeadline(db.delete('outbox', clientUuid), DB_OP_TIMEOUT_MS, 'Discarding this receipt timed out.');
        emit();
    }

    /*
     * Writes what the server just said about an accepted scan into the history cache,
     * before the outbox row that carried it is dropped.
     *
     * Without this there is a hole. discard() takes the receipt out of "on this phone"
     * the instant the server acknowledges it, and nothing puts it into the sent list
     * until /scan/api/submissions has been fetched and come back - a round trip, on a
     * connection that may not manage one. In between, a receipt somebody watched
     * themselves scan is in neither list, which reads as "it was lost", and is exactly
     * what leaves people tapping refresh to get their own receipt back on screen.
     *
     * The row written here is deliberately thin: an id, an identity and a status the
     * server has actually recorded. Everything else arrives with the pull that follows,
     * or with the push event that beats it - both keyed on this same server id, so both
     * replace this row rather than duplicating it.
     */
    async function cacheAccepted(entry, result) {
        if (!result || result.submission_id == null) return;
        try {
            var db = await getDB();
            var existing = await withDeadline(db.get('submissions', result.submission_id),
                                              DB_OP_TIMEOUT_MS, 'Reading cached history timed out.');
            // Already have the server's own version - a duplicate scan of a receipt sent
            // days ago, say. It knows more than we do; leave it alone.
            if (existing) return;

            await withDeadline(db.put('submissions', {
                id: result.submission_id,
                client_uuid: entry.client_uuid,
                // What is true at this moment: taken by the server, not yet checked
                // against the portal. The runner moves it on from here.
                status: result.status === 'duplicate' ? 'duplicate' : 'queued',
                is_duplicate: result.status === 'duplicate',
                received_at: new Date().toISOString(),
                captured_at: entry.captured_at || null,
                // Mirrors what ingest_submission decides on the server: a URL alongside
                // a photograph is still a URL submission. Getting this wrong would show
                // the row as a photo for the few seconds before the server's own version
                // of it arrives and replaces this one.
                input_type: entry.receipturl ? 'url' : 'photo',
                input_data: entry.receipturl || null,
                description: entry.description || null,
                location: entry.location || null,
                receipt: null,
                receipt_code: receiptCodeOf(entry.receipturl),
                error_message: null,
                retry: null,
            }), DB_OP_TIMEOUT_MS, 'Saving history locally timed out.');
        } catch (e) {
            // Storage is down, or slow. The receipt is safe either way - the server has
            // it, which is the whole point of the call that got us here - and the next
            // pull is what puts it on screen.
        }
    }

    /* The code printed on the receipt, so a row that has not been through the portal yet
       still says which receipt it is. Scanner owns the pattern the server also uses; a
       document without it (nothing today) simply gets no code. */
    function receiptCodeOf(url) {
        try {
            var parsed = url && global.Scanner && global.Scanner.parseReceipt
                ? global.Scanner.parseReceipt(url) : null;
            return parsed ? parsed.code : null;
        } catch (e) {
            return null;
        }
    }

    async function noteAttempt(entry, error, settings) {
        settings = settings || {};
        var db = await getDB();
        var current = await db.get('outbox', entry.client_uuid);
        if (!current) return;
        // A permanent rejection spends the whole budget at once. Retrying it is not
        // caution, it is eight identical requests carrying the same bytes to the same
        // refusal - and, until the last one, a row that reads "Waiting" to the person
        // holding the phone when nothing is waiting for anything. See terminalStatus().
        current.attempts = settings.terminal ? MAX_ATTEMPTS : (current.attempts || 0) + 1;
        current.last_error = error ? String(error).slice(0, 200) : null;
        await db.put('outbox', current);
    }

    /*
     * Whether an HTTP status means "not this receipt, ever" rather than "not now".
     *
     * 5xx is the server having a bad minute and 429 is it asking for a slower one; both
     * are worth coming back to. A 4xx is a verdict on what was sent, and sending it
     * again unchanged asks the same question.
     *
     * 413 is the exception that proves it, and syncPhoto answers it before this is ever
     * consulted: patience does not shrink a photo, but re-encoding it does. Only a photo
     * that cannot be made any smaller and is still refused arrives here as terminal.
     *
     * 401 is not in here deliberately: it is handled before this is reached, and it is
     * a verdict on the session rather than on the receipt. The queue survives it.
     */
    function terminalStatus(status) {
        if (status === 408 || status === 429) return false;
        return status >= 400 && status < 500;
    }

    /*
     * What a rejection says to somebody standing in a shop.
     *
     * "HTTP 413" is a fact and it is useless: it appears under a receipt on a phone, in
     * a list whose whole job is to say whether a receipt got where it was going. The
     * number is kept on the end regardless, because the same string is what the
     * diagnostics screen shows and what gets read down a phone line to whoever runs the
     * server - and for 413 in particular that person, not this one, is the one who can
     * do anything about it.
     */
    function httpReason(status) {
        var reasons = {
            400: 'The server could not read this receipt',
            403: 'This device is not allowed to send',
            404: 'The server has no address for this',
            // Only ever reached after the ladder in Scanner.shrinkToFit has been walked
            // to the bottom, so it says what was tried. The number stays on the end for
            // whoever runs the server: they are the one who can raise the body limit.
            413: 'This photo is still too large for the server, even shrunk',
        };
        var reason = reasons[status];
        if (!reason) return status >= 500 ? 'The server had a problem (HTTP ' + status + ')'
                                          : 'HTTP ' + status;
        return reason + ' (HTTP ' + status + ')';
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

            // `stopped` rather than an early return, because what follows this loop is
            // the *read* half of a sync and it does not belong to the outbox. See the
            // note above pullHistory() below.
            var stopped = false;

            for (var i = 0; i < urls.length && !stopped; i += URL_BATCH_SIZE) {
                var batch = urls.slice(i, i + URL_BATCH_SIZE);
                var result = await syncUrlBatch(batch);
                sent += result.sent;
                failed += result.failed;
                stopped = !!result.stop;
            }

            for (var j = 0; j < photos.length && !stopped; j++) {
                var photoResult = await syncPhoto(photos[j]);
                sent += photoResult.sent;
                failed += photoResult.failed;
                stopped = !!photoResult.stop;
            }

            /*
             * Unconditional, and that is the fix for the empty history screen.
             *
             * This used to be `if (sent > 0 || settings.refresh)`, reached only by
             * falling off the end of both loops - so one undeliverable receipt in the
             * outbox took the whole read path down with it. A photo the server would not
             * accept (an upload past the ingress body limit answers 413, and a stalled
             * one throws) set `stop`, the function returned here, and history was never
             * asked for. The periodic sync passes no `refresh`, and `sent` cannot climb
             * while the queue is stuck, so the condition stayed false on every tick
             * afterwards: the phone would go on showing an empty list, next to a summary
             * that kept updating because /scan/api/summary is polled on its own timer.
             * That is the exact pair of symptoms - live totals above, nothing below.
             *
             * Sending and reading are two different jobs that happen to share a timer.
             * A queue that cannot drain is a reason to keep showing what the server has,
             * not a reason to stop asking. When the network really is gone this costs one
             * fetch that fails and falls back to the cache, which is what it did before.
             *
             * A 401 on the way up needs no guard: handleAuthFailure() has already cleared
             * the session, and pullHistory() returns without a request when there is no
             * token to send.
             */
            await pullHistory();
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
            var urlTerminal = terminalStatus(response.status);
            for (var m = 0; m < batch.length; m++) {
                await noteAttempt(batch[m], httpReason(response.status), { terminal: urlTerminal });
            }
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
                // Into the sent list before it leaves the outbox, never the other way
                // round: for the moment between the two the receipt is in both lists,
                // which is survivable, where being in neither is not.
                await cacheAccepted(entry, res);
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

    /*
     * One photo, at whatever size this server turns out to accept.
     *
     * The loop is the whole point. A photograph larger than the body limit of the proxy
     * in front of the app is refused with a 413 before Flask sees it, and this used to
     * be the end of that receipt: the entry was marked permanently failed and sat in the
     * outbox reading "Not sending. This photo is too large for the server to accept
     * (HTTP 413)" - a sentence that is both true and useless to the person holding the
     * phone, who cannot resize a photograph and should never have been asked to. The
     * gallery made it routine: an import is a twelve-megapixel original, not a preview
     * frame, and a phone full of them lost every one.
     *
     * A refusal is now an instruction. The photo is re-encoded smaller (see
     * Scanner.shrinkToFit) and sent again, at most PHOTO_SHRINK_ROUNDS times, and what
     * the server refused is remembered so the next photo goes up already fitting rather
     * than discovering the same wall. A shrink does not spend an attempt: nothing was
     * wrong with the receipt, only with its file size, and burning the retry budget on
     * that is how a receipt that would have fitted on the second try gets parked.
     *
     * Only a photo that has been through the whole ladder and is still refused is
     * terminal - and by then the message can honestly say so.
     */
    async function syncPhoto(entry) {
        if (!entry.photo) {
            await discard(entry.client_uuid);
            return { sent: 0, failed: 0 };
        }

        // What this phone already knows the server will take, applied before the first
        // request rather than after the first refusal.
        entry = await fitKnownCeiling(entry);

        for (var round = 0; round <= PHOTO_SHRINK_ROUNDS; round++) {
            var response;
            try {
                response = await postPhoto(entry);
            } catch (e) {
                await noteAttempt(entry, e.message);
                return { sent: 0, failed: 1, stop: true };
            }

            if (response.status === 401) {
                await handleAuthFailure(response);
                return { sent: 0, failed: 1, stop: true };
            }

            if (response.status === 413) {
                // Not on the last round: a re-encode nobody is going to send is a
                // decode, a resize and a write to storage spent on a photograph that is
                // about to be parked either way.
                var smaller = round < PHOTO_SHRINK_ROUNDS ? await shrinkAfterRefusal(entry) : null;
                if (smaller) {
                    entry = smaller;
                    continue;
                }
                break;
            }

            if (!response.ok) {
                await noteAttempt(entry, httpReason(response.status),
                                  { terminal: terminalStatus(response.status) });
                return { sent: 0, failed: 1, stop: response.status >= 500 };
            }

            // Same order as the URL batch, for the same reason: the photo lands in
            // history before it leaves the outbox, so it is never in neither place.
            await cacheAccepted(entry, await response.json().catch(function () { return null; }));
            await discard(entry.client_uuid);
            emit();
            return { sent: 1, failed: 0, stop: false };
        }

        // Out of room: either the picture cannot be made smaller on this phone, or it
        // was made as small as it can usefully be and is still refused. That is a server
        // to fix, not a receipt to keep re-sending.
        await noteAttempt(entry, httpReason(413), { terminal: true });
        return { sent: 0, failed: 1, stop: false };
    }

    function postPhoto(entry) {
        var form = new FormData();
        form.append('receiptphoto', entry.photo, (entry.client_uuid || 'receipt') + '.jpg');
        form.append('client_uuid', entry.client_uuid);
        form.append('captured_at', entry.captured_at || '');
        if (entry.description) form.append('description', entry.description);
        if (entry.location) form.append('location', entry.location);
        // A scan whose QR code the phone read, sent with the photograph it was read
        // from. The server files one submission carrying both: verified from the code,
        // with the paper kept beside it. Multipart rather than the JSON batch because a
        // photograph cannot go in the batch, and splitting the pair across two requests
        // is how they end up as two submissions when the second one fails.
        if (entry.receipturl) form.append('receipturl', entry.receipturl);

        return apiFetch('/scan/api/sync/photo', { method: 'POST', body: form },
                        { timeout: SYNC_TIMEOUT_MS });
    }

    /*
     * Shrinks a photo the server has just refused, and remembers the refusal.
     *
     * Returns the entry to send next, or null when there is nothing left to try - a
     * phone that cannot re-encode at all (no canvas, an image it cannot decode), or a
     * photograph already at the floor, where making it smaller would cost the printed
     * verification code the vision model reads.
     *
     * The remembered ceiling only ever ratchets downwards, and it expires, because it is
     * a guess about somebody else's configuration: the true limit is somewhere below
     * what was just refused, and if it is raised - which is the actual fix, and the one
     * this message asks for - no phone should go on shrinking for a limit that is gone.
     */
    async function shrinkAfterRefusal(entry) {
        var size = (entry.photo && entry.photo.size) || 0;
        if (size <= UPLOAD_FLOOR_BYTES) return null;

        var ceiling = Math.max(UPLOAD_FLOOR_BYTES, Math.floor(size * 0.6));
        var smaller = await shrinkPhoto(entry.photo, ceiling);
        if (!smaller || smaller.size >= size) return null;

        try {
            var known = await metaGet(UPLOAD_CEILING_KEY, null);
            if (!known || !known.bytes || known.bytes > ceiling) {
                await metaSet(UPLOAD_CEILING_KEY, { bytes: ceiling, at: Date.now() });
            }
        } catch (e) { /* storage is down; the shrink still stands for this attempt */ }

        return await replacePhoto(entry, smaller);
    }

    /* The ceiling this phone has learned, applied to a photo that is over it. */
    async function fitKnownCeiling(entry) {
        var known;
        try {
            known = await metaGet(UPLOAD_CEILING_KEY, null);
        } catch (e) {
            return entry;
        }
        if (!known || !known.bytes) return entry;
        if (known.at && Date.now() - known.at > UPLOAD_CEILING_TTL_MS) return entry;
        if (entry.photo.size <= known.bytes) return entry;

        var smaller = await shrinkPhoto(entry.photo, known.bytes);
        if (!smaller || smaller.size >= entry.photo.size) return entry;
        return await replacePhoto(entry, smaller);
    }

    function shrinkPhoto(blob, maxBytes) {
        var scanner = global.Scanner;
        if (!scanner || typeof scanner.shrinkToFit !== 'function') return Promise.resolve(null);
        return scanner.shrinkToFit(blob, maxBytes).catch(function () { return null; });
    }

    /*
     * Swaps in the smaller photograph, in the outbox as well as in hand.
     *
     * Written back so the work survives: a phone that shrinks a twelve-megapixel import
     * and then loses its connection should not decode and re-encode it again on the next
     * tick, and a relaunch should not start the whole discovery over.
     */
    async function replacePhoto(entry, blob) {
        entry.photo = blob;
        try {
            var db = await getDB();
            var current = await db.get('outbox', entry.client_uuid);
            if (current) {
                current.photo = blob;
                await db.put('outbox', current);
            }
        } catch (e) { /* the smaller copy is still what this attempt sends */ }
        return entry;
    }

    // ---------------------------------------------------------------- history

    /*
     * Pulls this device's most recent submissions and caches them so the history
     * screen reads the same whether or not there is a network.
     *
     * Upserted rather than wholesale-replaced: this only ever asks for the latest
     * page, and clearing the store first used to mean the local cache could never
     * hold more than one page's worth of history - everything paged into it by
     * loadOlderHistory() was thrown away on the very next ordinary sync. A row that
     * genuinely no longer exists server-side (there is no such path today) simply
     * stops being refreshed; it does not need to be actively deleted here.
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
        var tx = db.transaction('submissions', 'readwrite');
        for (var i = 0; i < rows.length; i++) tx.store.put(rows[i]);
        await withDeadline(tx.done, DB_OP_TIMEOUT_MS, 'Saving history locally timed out.');

        await metaSet('historySyncedAt', Date.now());
        // Whatever moved in that page moved the totals with it.
        scheduleSummary();
        emit();
        return rows;
    }

    /*
     * Pages further back into history than what's cached, for the history screen's
     * infinite scroll. Requests older than `beforeId`, or matching `q` (searches
     * vendor, receipt code and line items - see /scan/api/submissions), and caches
     * whatever comes back the same way pullHistory does, so paging through history
     * once means it is available offline from then on.
     */
    async function loadOlderHistory(options) {
        options = options || {};
        if (!sessionToken()) return { rows: [], hasMore: false };

        var params = new URLSearchParams();
        if (options.beforeId != null) params.set('before_id', options.beforeId);
        if (options.limit != null) params.set('limit', options.limit);
        if (options.q) params.set('q', options.q);

        var response;
        try {
            response = await apiFetch('/scan/api/submissions?' + params.toString());
        } catch (e) {
            return { rows: [], hasMore: false };
        }
        if (response.status === 401) {
            await handleAuthFailure(response);
            return { rows: [], hasMore: false };
        }
        if (!response.ok) return { rows: [], hasMore: false };

        var body = await response.json();
        var rows = body.submissions || [];

        var db = await getDB();
        var tx = db.transaction('submissions', 'readwrite');
        for (var i = 0; i < rows.length; i++) tx.store.put(rows[i]);
        await withDeadline(tx.done, DB_OP_TIMEOUT_MS, 'Saving history locally timed out.');

        emit();
        return { rows: rows, hasMore: !!body.has_more };
    }

    /*
     * Reads back what's cached, newest first, optionally bounded - so a history
     * screen with thousands of rows cached over time never has to pull all of them
     * into memory just to show the first page. The `submissions` store's keyPath is
     * the server's own `id`, which already orders chronologically here (one writer,
     * one increasing id) and is exactly what the server's own before_id cursor is
     * built on - so a plain cursor over the primary key is enough, no extra index.
     */
    async function cachedHistory(options) {
        options = options || {};
        try {
            var db = await getDB();
            var range = options.beforeId != null ? IDBKeyRange.upperBound(options.beforeId, true) : null;
            var rows = [];
            var cursor = await withDeadline(
                db.transaction('submissions').store.openCursor(range, 'prev'),
                DB_OP_TIMEOUT_MS, 'Reading cached history timed out.'
            );
            while (cursor && (!options.limit || rows.length < options.limit)) {
                rows.push(cursor.value);
                cursor = await withDeadline(cursor.continue(), DB_OP_TIMEOUT_MS, 'Reading cached history timed out.');
            }
            return rows;
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
            startEventStream();
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
        // null, not 0. A storage failure used to leave this at its initial 0, which
        // every screen then rendered as "nothing waiting, everything has been sent" -
        // the single most reassuring thing the app can say, displayed at precisely the
        // moment it cannot account for a receipt at all. Unknown has to look different
        // from empty.
        var pending = null;
        var signOutReason = null;
        var uploadCeiling = null;
        var storageOk = true;
        var storageError = null;

        try {
            pending = await outboxCount();
            signOutReason = await metaGet('signOutReason', null);
            uploadCeiling = await metaGet(UPLOAD_CEILING_KEY, null);
        } catch (e) {
            storageOk = false;
            storageError = (e && e.message) || 'storage-unavailable';
        }

        return {
            online: global.navigator.onLine !== false,
            reachable: serverLikelyReachable(),
            syncing: syncing,
            pending: pending,
            session: loadSession(),
            signOutReason: signOutReason,
            storageOk: storageOk,
            storageError: storageError,
            // Whether this app is currently being told about changes as they happen,
            // as opposed to finding out at the next poll. Shown, not hidden: "up to
            // date" and "not listening" must not look the same on a screen someone is
            // relying on.
            live: streamLive,
            // Last known numbers for the top of the screen. Served from the cache when
            // offline, so the figures do not blank out the moment a phone loses signal.
            summary: summary,
            // A copy: the sync pill's expanded view must not be able to mutate the
            // internal feed by holding a reference to it.
            recentEvents: recentEvents.slice(),
            // Set only once this phone has had a photo refused for its size. Nobody in
            // the field needs to see it - the app deals with it silently now - but the
            // diagnostics screen is read down a phone line to whoever runs the server,
            // and they are the one person who can raise the limit.
            uploadCeiling: uploadCeiling,
        };
    }

    // ---------------------------------------------------------------- summary
    //
    // The few numbers above the history list. Cached in `meta` rather than recomputed
    // on the phone: the server already holds every receipt this device has sent, and
    // a total assembled locally from a partial cache would quietly disagree with it.

    var summary = null;
    var summaryTimer = null;

    async function cachedSummary() {
        if (summary) return summary;
        try { summary = await metaGet('summary', null); } catch (e) { /* storage is down */ }
        return summary;
    }

    async function pullSummary() {
        if (!sessionToken()) return summary;
        var response;
        try {
            response = await apiFetch('/scan/api/summary');
        } catch (e) {
            return summary;   // Offline. The cached figures stand; they are still true.
        }
        if (response.status === 401) {
            await handleAuthFailure(response);
            return summary;
        }
        if (!response.ok) return summary;

        summary = await response.json();
        try { await metaSet('summary', summary); } catch (e) { /* not worth failing over */ }
        emit();
        return summary;
    }

    /* Debounced: a runner tick finishing eight receipts is one change to these totals,
       not eight. */
    function scheduleSummary(delay) {
        clearTimeout(summaryTimer);
        summaryTimer = setTimeout(function () { pullSummary().catch(function () {}); },
                                  delay == null ? 500 : delay);
    }

    // ---------------------------------------------------------------- live updates
    //
    // A push channel layered on top of the poll loop above, never a replacement for
    // it. Every failure mode here - the fetch rejects, the server never answers, the
    // browser kills a background connection - degrades to exactly what the app
    // already did without any of this: the interval timer and the visibility/online
    // sync() calls above never stop running. This only ever makes an update arrive
    // sooner than the next one of those would have.
    //
    // Not EventSource: /scan/api/stream is guarded by device_required, which reads
    // the session from an Authorization header on purpose (see its docstring), and
    // EventSource cannot set one. fetch() with a streamed body reads the same
    // `data: {...}\n\n` framing by hand instead.

    var STREAM_RECONNECT_MS = 2000;       // First retry after a stream drops.
    var STREAM_RECONNECT_MAX_MS = 30000;  // Ceiling - a nicety reconnecting, not the outbox.
    // The server sends a ping every 15s. Silence past this means the connection is
    // gone in the one way a reader cannot otherwise detect: still open, still awaiting
    // bytes that will never come, which is what a mobile network dropping a long-lived
    // socket looks like from in here.
    var STREAM_SILENCE_MS = 45000;
    var streamGeneration = 0;
    var streamController = null;
    var streamReconnectTimer = null;
    var streamWatchdog = null;
    var streamLive = false;
    // The cursor. Every frame carries the id of the event that produced it, and
    // reconnecting with the last one seen is what turns a dropped connection into a
    // gap the server can fill rather than events nobody will ever hear about.
    var lastEventId = null;

    /*
     * Applies one pushed event to the local cache.
     *
     * Read-modify-write, never a bare put(): the payload off the wire is a partial
     * status update, not a full submissions row, and put()-ing it as-is would blank
     * out every field an earlier poll had already filled in. A submission not yet
     * cached is left alone - there is nothing to patch, and the next ordinary sync
     * or pull fetches it in full anyway.
     */
    // A short, capped feed of what the stream has said recently, for the sync pill's
    // expanded view - a notification list, not a record of truth, so in-memory only
    // is fine; it starts empty on every fresh page load, which is the correct state
    // for "what happened since this screen opened."
    var MAX_RECENT_EVENTS = 5;
    var recentEvents = [];

    var EVENT_VERBS = {
        completed: 'verified', processing: 'checking…', failed: 'failed',
        duplicate: 'duplicate', queued: 'queued',
    };

    function describeStreamEvent(data, existing) {
        var name = (data.data && data.data.vendor_name)
            || (existing && existing.receipt && existing.receipt.vendor_name)
            || (existing && existing.receipt_code)
            || 'A receipt';
        return name + ' — ' + (EVENT_VERBS[data.status] || data.status || 'updated');
    }

    async function applyStreamEvent(eventType, data) {
        // The server saying it cannot account for what we missed: everything cached is
        // suspect, so refetch rather than patch. Same thing a cold start does.
        if (eventType === 'stream.resync') {
            pullHistory().catch(function () {});
            scheduleSummary(0);
            return;
        }
        if (!data || data.submission_id == null) return;
        var db = await getDB();
        var existing = await withDeadline(
            db.get('submissions', data.submission_id), DB_OP_TIMEOUT_MS, 'Reading cached history timed out.'
        );

        recentEvents.unshift({ label: describeStreamEvent(data, existing), at: Date.now() });
        recentEvents.length = Math.min(recentEvents.length, MAX_RECENT_EVENTS);

        if (existing) {
            var patched = Object.assign({}, existing);
            if (data.status !== undefined) patched.status = data.status;
            if (data.error_message !== undefined) patched.error_message = data.error_message;
            // Only 'submission.processed' carries the receipt itself (receipt_to_dict's
            // shape, which happens to already match what a submissions row's `receipt`
            // field holds - vendor_name, total_amount, items, and so on).
            if (eventType === 'submission.processed' && data.data) patched.receipt = data.data;

            await withDeadline(
                db.put('submissions', patched), DB_OP_TIMEOUT_MS, 'Saving history locally timed out.'
            );
        } else {
            // An event about a receipt this phone has never cached - sent from another
            // device on the same account, or queued before this install existed. There
            // is nothing to patch, so fetch it: leaving it out is how a receipt ends up
            // existing on the server and nowhere on the screen.
            pullHistory().catch(function () {});
        }

        // The totals above the list just moved.
        scheduleSummary();
        emit();
    }

    /*
     * Parses one SSE frame into its fields.
     *
     * The wire format is a block of `field: value` lines, and a frame now carries more
     * than one of them - an `id:` the reconnect resumes from, an `event:` naming the
     * heartbeat, and the `data:` itself. Reading only the first line, as this used to,
     * means a frame whose id happens to come first parses as nothing at all.
     *
     * Returns null for anything with no usable payload: comment lines, `retry:`, and
     * blank blocks.
     */
    function parseEventFrame(frame) {
        var out = { id: null, event: 'message', data: '' };
        var lines = frame.split('\n');
        for (var i = 0; i < lines.length; i++) {
            var line = lines[i];
            if (!line || line.charAt(0) === ':') continue;   // Comment, or padding.
            var colon = line.indexOf(':');
            if (colon < 0) continue;
            var field = line.slice(0, colon);
            var value = line.slice(colon + 1).replace(/^ /, '');
            if (field === 'id') out.id = value;
            else if (field === 'event') out.event = value;
            else if (field === 'data') out.data = out.data ? out.data + '\n' + value : value;
        }
        return out.data === '' && out.id === null ? null : out;
    }

    /*
     * Turns raw chunks off a fetch body into parsed frames.
     *
     * Frames are not guaranteed to land in one chunk or even one read() - TCP does
     * not know or care where a frame boundary is - so this buffers across reads and
     * only ever acts on text up to the last complete `\n\n` it has seen.
     */
    async function readEventStream(response, onFrame) {
        var reader = response.body.getReader();
        var decoder = new TextDecoder();
        var buffer = '';
        while (true) {
            var chunk = await reader.read();
            if (chunk.done) return;
            buffer += decoder.decode(chunk.value, { stream: true });
            var blocks = buffer.split('\n\n');
            buffer = blocks.pop(); // Last entry: whatever came after the final \n\n, possibly incomplete.
            for (var i = 0; i < blocks.length; i++) {
                var frame = parseEventFrame(blocks[i]);
                if (frame) onFrame(frame);
            }
        }
    }

    function stopEventStream() {
        // Bumped first: a connect attempt already past its fetch() and into
        // readEventStream() checks this after abort() settles it, and must not
        // schedule a reconnect on a stream that was stopped on purpose.
        streamGeneration++;
        if (streamController) { try { streamController.abort(); } catch (e) { /* already gone */ } }
        streamController = null;
        streamLive = false;
        clearTimeout(streamReconnectTimer);
        clearTimeout(streamWatchdog);
    }

    function startEventStream() {
        if (!sessionToken()) return;
        stopEventStream();
        connectStream(streamGeneration, STREAM_RECONNECT_MS);
    }

    /*
     * Restarts the silence timer.
     *
     * Called on every frame, heartbeats included. A stream that stops delivering
     * without closing is the failure mode that leaves an app confidently showing
     * yesterday's state, and nothing else here can see it: the fetch has not rejected,
     * the reader has not finished, there is simply nothing arriving. Aborting is what
     * turns that into an ordinary reconnect.
     */
    function armStreamWatchdog(generation) {
        clearTimeout(streamWatchdog);
        streamWatchdog = setTimeout(function () {
            if (generation !== streamGeneration) return;
            streamLive = false;
            if (streamController) { try { streamController.abort(); } catch (e) { /* already gone */ } }
        }, STREAM_SILENCE_MS);
    }

    async function connectStream(generation, delay) {
        if (generation !== streamGeneration) return;
        // Nothing is watching a background tab's status pill; holding a connection
        // open for one just spends battery and a socket the platform may kill anyway.
        if (global.document.visibilityState !== 'visible') return;
        var token = sessionToken();
        if (!token) return;

        streamController = new AbortController();
        var reconnect = true;
        var openedAt = 0;
        try {
            // ?since= rather than a header: this is fetch(), not EventSource, so there
            // is no Last-Event-ID being resent on our behalf. Without a cursor the
            // server can only start us at "now", and anything that happened while the
            // phone was in a pocket is lost.
            var url = '/scan/api/stream' + (lastEventId ? '?since=' + encodeURIComponent(lastEventId) : '');
            var response = await fetch(url, {
                headers: { Authorization: 'Bearer ' + token },
                credentials: 'same-origin',
                signal: streamController.signal,
            });
            if (response.status === 401) {
                await handleAuthFailure(response);
                reconnect = false; // Dead token; retrying just repeats the same 401.
            } else if (!response.ok || !response.body) {
                throw new Error('stream unavailable');
            } else {
                openedAt = Date.now();
                streamLive = true;
                markServerUp();
                armStreamWatchdog(generation);
                emit();
                // Anything that happened between this app last listening and now is
                // replayed by the cursor above - but a first connection has no cursor,
                // so catch up the ordinary way too.
                if (!lastEventId) { pullHistory().catch(function () {}); }
                scheduleSummary(0);

                await readEventStream(response, function (frame) {
                    armStreamWatchdog(generation);
                    if (frame.id) lastEventId = frame.id;
                    if (frame.event === 'ping') return;   // Liveness only; nothing to apply.
                    var envelope;
                    try { envelope = JSON.parse(frame.data); }
                    catch (e) { return; }   // One malformed frame is not worth dropping the connection.
                    applyStreamEvent(envelope && envelope.event_type, envelope && envelope.data)
                        .catch(function () {});
                });
            }
        } catch (e) {
            // Aborted on purpose by stopEventStream() or the watchdog, or a network
            // hiccup - either way the reconnect logic below decides what happens next.
        }

        streamLive = false;
        clearTimeout(streamWatchdog);
        emit();

        if (!reconnect || generation !== streamGeneration) return;
        if (global.document.visibilityState !== 'visible') return;

        // A connection that actually held resets the backoff. Without this, a phone
        // moving between cells all morning ends the morning waiting half a minute
        // before each reconnect, having been on a working connection the whole time.
        // "Held" is measured, not assumed: a server or proxy that accepts the request
        // and closes it a moment later would otherwise be reconnected to in a tight
        // loop, from a device on metered data.
        var healthy = openedAt && (Date.now() - openedAt) > 5000;
        var nextDelay = healthy ? STREAM_RECONNECT_MS : Math.min(delay * 2, STREAM_RECONNECT_MAX_MS);
        streamReconnectTimer = setTimeout(function () { connectStream(generation, nextDelay); },
                                          healthy ? 500 : delay);
    }

    // ---------------------------------------------------------------- boot

    /*
     * Safe to call more than once, because it now is.
     *
     * When Scan, History and Diagnostics were three documents, each one called this
     * exactly once and the question never came up. They share a document now, and two
     * of the components on it call it during their own boot - so without this guard a
     * user who opened History would be running two sync intervals, two sets of
     * online/offline/visibilitychange listeners and two flushOnHide handlers, firing
     * duplicate requests at the server for the rest of the session.
     */
    var started = false;

    function start() {
        if (started) return;
        started = true;

        requestPersistence();
        // Deliberately not before there is a session.
        //
        // Installing the worker downloads roughly two megabytes of app shell across a
        // dozen requests. On a phone on mobile data that saturates the connection, and
        // an unactivated phone has exactly one request that matters - the activation
        // POST - which then queues behind all of it and times out. The app is no use
        // offline until it is activated anyway, so there is nothing to lose by waiting;
        // activate() installs the worker the moment a session exists.
        if (sessionToken()) {
            registerServiceWorker().then(topUpShellCache);
            startEventStream();
            // Cache first so the numbers are on screen before the network answers -
            // and, on a phone with no signal, instead of it.
            cachedSummary().then(function () { emit(); scheduleSummary(0); });
        }
        // Not started here. Geolocation is its own permission prompt, and history and
        // diagnostics - which also call start() - have no use for a location fix at
        // all. The scan screen, the one place that attaches one to a receipt, asks for
        // it itself once the camera is settled, so a cold launch does not stack two
        // permission prompts on top of each other.

        global.addEventListener('online', function () {
            markServerUp();
            if (sessionToken()) { topUpShellCache(); startEventStream(); scheduleSummary(0); }
            sync({ force: true }).catch(function () {});
            emit();
        });
        global.addEventListener('offline', function () { streamLive = false; emit(); });

        global.document.addEventListener('visibilitychange', function () {
            if (global.document.visibilityState === 'visible') {
                // Coming back is the moment the screen is most likely to be wrong: the
                // stream was stopped when the phone went away, and whatever happened in
                // between is exactly what the person is looking for now. The stream
                // reconnects with its cursor and replays it; the sync and the summary
                // cover anything older than the log still holds.
                if (sessionToken()) { topUpShellCache(); startEventStream(); scheduleSummary(0); }
                sync().catch(function () {});
            } else {
                // Whoever is holding the phone may not open this tab again for hours -
                // driving, a queue, a shop floor. This is the last reliable moment to
                // get anything queued onto the wire before it does.
                stopEventStream();
                flushOnHide();
            }
        });
        // The 'visibilitychange' above covers switching apps or locking the screen;
        // this covers the tab being closed or navigated away from outright, which on
        // some browsers fires pagehide without ever marking the document hidden first.
        global.addEventListener('pagehide', flushOnHide);

        // No `{ refresh: true }` on the boot call any more. It used to be the one way to
        // make a sync also read history back; every sync does that now, so the flag would
        // only suggest the periodic ticks do less than they do.
        setInterval(function () { sync().catch(function () {}); }, SYNC_INTERVAL_MS);
        sync().catch(function () {});
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
        sync: sync, pullHistory: pullHistory, loadOlderHistory: loadOlderHistory,
        cachedHistory: cachedHistory, retrySubmission: retrySubmission, apiFetch: apiFetch,
        pullSummary: pullSummary, cachedSummary: cachedSummary,
        // live updates
        startEventStream: startEventStream, stopEventStream: stopEventStream,
        parseEventFrame: parseEventFrame,
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
