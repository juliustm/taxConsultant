/*
 * scanner.js - reading EFD QR codes off thermal paper.
 *
 * The hard part is not decoding a QR code; it is decoding *this* QR code. An EFD
 * receipt is printed by a thermal head onto a narrow roll, so by the time anyone
 * scans it the code is typically some combination of: physically small, printed with
 * a fading head, creased across the middle, curled, photographed in a dim shop, and
 * overlaid with the security watermark the paper stock already carried.
 *
 * Three decisions follow from that:
 *
 *   1. Resolution beats framerate. A small code has few pixels per module, so the
 *      camera is asked for 1920 wide and the region of interest is cut out at the
 *      sensor's own resolution and never scaled down. Downscaling to "go faster" is
 *      what makes a marginal code unreadable.
 *
 *   2. Binarization beats everything. Deciding which pixels are black is the whole
 *      problem on faded ink and watermarked stock, and a single global threshold -
 *      what the small pure-JS decoders use - fails on exactly that. ZXing's
 *      local-average binarizer thresholds per region, which is why it is the fallback
 *      here rather than something smaller.
 *
 *   3. There must be a way out. Some receipts genuinely cannot be decoded, and a
 *      scanner that only insists is worse than useless in the field. After a few
 *      seconds the app offers the photo path instead, which the server already
 *      handles end to end.
 */
(function (global) {
    'use strict';

    var ZXING_WASM_URL = '/static/js/vendor/zxing_reader.wasm';
    var DECODER_WORKER_URL = '/static/js/decoder-worker.js';

    // Mirrors _RECEIPT_URL_RE in utils/tra.py. The two must agree: this is what stops
    // an unusable scan from being queued now and failing silently hours later.
    // tests/test_pwa_assets.py asserts they stay in step.
    var RECEIPT_URL_RE = /([A-Za-z0-9]+)_(\d{2})(\d{2})(\d{2})\/?\s*$/;

    // Deliberately shy of 1080p60. Decoding is the bottleneck, and asking for more
    // frames than we can process just heats the phone up and drains it.
    var TARGET_FPS = 10;
    var FRAME_INTERVAL_MS = 1000 / TARGET_FPS;

    // Fraction of the shorter side taken as the region of interest.
    var ROI_RATIO = 0.7;

    // How long to insist before suggesting a photo instead.
    var STRUGGLE_AFTER_MS = 6000;

    // Every Nth failed attempt, widen the search. A contrast stretch rescues washed-out
    // prints; the full frame rescues a user who framed the receipt loosely. Both cost
    // more than the fast path, so neither runs on every frame.
    var STRETCH_EVERY = 3;
    var FULL_FRAME_EVERY = 8;

    var zxingReady = null;
    var barcodeDetector = null;
    var barcodeDetectorChecked = false;

    // ------------------------------------------------------------- engines

    /*
     * The native detector, where the platform has one.
     *
     * On Android Chrome this is the platform's own barcode stack: hardware accelerated,
     * a couple of milliseconds a frame, and better on damaged codes than anything we
     * can ship. Safari has no such thing, which is why the WASM fallback exists.
     */
    async function getBarcodeDetector() {
        if (barcodeDetectorChecked) return barcodeDetector;
        barcodeDetectorChecked = true;
        try {
            if (!('BarcodeDetector' in global)) return null;
            var supported = await global.BarcodeDetector.getSupportedFormats();
            if (supported.indexOf('qr_code') === -1) return null;
            barcodeDetector = new global.BarcodeDetector({ formats: ['qr_code'] });
        } catch (e) {
            barcodeDetector = null;
        }
        return barcodeDetector;
    }

    /*
     * ZXing-C++ compiled to WASM, loaded from our own origin.
     *
     * The bundled default fetches its .wasm from a CDN, which would make the scanner
     * dependent on the public internet - the one thing this app cannot assume. The
     * override points it at the copy the service worker has precached.
     */
    function getZXing() {
        if (zxingReady) return zxingReady;
        zxingReady = (async function () {
            var zxing = global.ZXingWASM;
            if (!zxing) throw new Error('The QR reader failed to load.');
            // Checked once, here, rather than discovered per frame. Calling a decode
            // entry point that does not exist throws on every single frame, and since
            // a failed frame is indistinguishable from a frame with no code in it, the
            // scanner looks like it is working and simply never finds anything. That
            // is precisely what happened: the export is readBarcodesFromImageData, and
            // a name that merely contained it was assumed to be it.
            if (typeof zxing.readBarcodesFromImageData !== 'function') {
                throw new Error('The QR reader is the wrong version (no readBarcodesFromImageData).');
            }
            zxing.setZXingModuleOverrides({
                locateFile: function (path, prefix) {
                    return path.endsWith('.wasm') ? ZXING_WASM_URL : (prefix || '') + path;
                },
            });
            await zxing.getZXingModule();
            return zxing;
        })();
        // A failed load must not be cached as a permanently rejected promise, or a
        // transient miss on the wasm can never be retried.
        zxingReady.catch(function () { zxingReady = null; });
        return zxingReady;
    }

    var ZXING_OPTIONS = {
        formats: ['QRCode'],
        // The defaults, stated rather than inherited, because they are the reason this
        // engine was chosen. tryInvert covers prints that photograph light-on-dark;
        // LocalAverage is what defeats uneven fade and watermark gradients.
        tryHarder: true,
        tryRotate: true,
        tryInvert: true,
        tryDownscale: true,
        binarizer: 'LocalAverage',
        maxNumberOfSymbols: 1,
    };

    /*
     * ZXing in a worker, where it belongs.
     *
     * See decoder-worker.js for why: on a phone with no native detector this decode is
     * the whole per-frame cost, and on the main thread it starves the UI and - less
     * obviously but far worse - IndexedDB's completion callbacks, which is what turned
     * a working scanner into one that could not save what it had just read.
     *
     * The worker is optional in the strict sense: if it cannot be constructed, or its
     * own load of the wasm fails, everything falls back to decoding in this thread,
     * exactly as before. Slow beats broken.
     */
    var worker = null;
    var workerBroken = false;
    var workerSeq = 0;
    var workerPending = {};

    function getWorker() {
        if (worker || workerBroken) return worker;
        try {
            worker = new global.Worker(DECODER_WORKER_URL);
        } catch (e) {
            workerBroken = true;
            return null;
        }
        worker.onmessage = function (event) {
            var message = event.data || {};
            var waiting = workerPending[message.id];
            if (!waiting) return;
            delete workerPending[message.id];
            if (message.error) waiting.reject(new Error(message.error));
            else waiting.resolve(message.ready ? true : message.text);
        };
        // A worker that dies takes every request in flight with it. Reject them rather
        // than leaving their callers awaiting a reply that is never coming, and stop
        // using it - the fallback path below is what keeps the scanner alive.
        worker.onerror = function () {
            workerBroken = true;
            var dead = worker;
            worker = null;
            try { dead.terminate(); } catch (e) { /* already gone */ }
            Object.keys(workerPending).forEach(function (id) {
                workerPending[id].reject(new Error('The QR reader stopped unexpectedly.'));
                delete workerPending[id];
            });
        };
        return worker;
    }

    function askWorker(message, transfer) {
        var active = getWorker();
        if (!active) return null;
        message.id = ++workerSeq;
        return new Promise(function (resolve, reject) {
            workerPending[message.id] = { resolve: resolve, reject: reject };
            try {
                active.postMessage(message, transfer || []);
            } catch (e) {
                delete workerPending[message.id];
                reject(e);
            }
        });
    }

    async function decodeWithZXing(canvas) {
        var ctx = canvas.getContext('2d', { willReadFrequently: true });
        var image = ctx.getImageData(0, 0, canvas.width, canvas.height);

        // Transferred, not copied: at 1920x1080 the pixel buffer is eight megabytes, and
        // structured-cloning that ten times a second is its own performance problem.
        // getImageData hands us a fresh buffer each call, so giving it away is safe.
        var viaWorker = askWorker(
            { buffer: image.data.buffer, width: image.width, height: image.height, options: ZXING_OPTIONS },
            [image.data.buffer]
        );

        if (viaWorker) {
            try {
                return await viaWorker;
            } catch (e) {
                // A worker that decoded and failed is a real answer; only a worker that
                // died is worth retrying here. Falling back on every decode error would
                // quietly run the expensive path twice per frame on the main thread -
                // the exact thing this indirection exists to avoid.
                if (!workerBroken) throw e;
                // Its pixels went with it: the buffer above was transferred, so it is
                // detached now and has to be read off the canvas again.
                image = ctx.getImageData(0, 0, canvas.width, canvas.height);
            }
        }

        var zxing = await getZXing();
        var results = await zxing.readBarcodesFromImageData(image, ZXING_OPTIONS);
        if (results && results.length && results[0].text) return results[0].text;
        return null;
    }

    /*
     * Compiles the wasm before the first receipt needs it, wherever it is going to run.
     * Resolves once something can decode; rejects with why nothing can.
     */
    function warmDecoder() {
        var viaWorker = askWorker({ type: 'warm' });
        if (viaWorker) {
            // The worker is preferred but not required. If it cannot load the wasm,
            // this thread might still manage it.
            return viaWorker.catch(function (error) {
                return getZXing().then(function () { return true; }, function () { throw error; });
            });
        }
        return getZXing().then(function () { return true; });
    }

    async function decodeWithNative(canvas) {
        var detector = await getBarcodeDetector();
        if (!detector) return null;
        try {
            var found = await detector.detect(canvas);
            if (found && found.length && found[0].rawValue) return found[0].rawValue;
        } catch (e) { /* fall through to ZXing */ }
        return null;
    }

    // ------------------------------------------------------------- image work

    /*
     * Normalises contrast across the region of interest.
     *
     * A tired thermal head prints grey on white instead of black on white, which
     * leaves every binarizer guessing. Rescaling the luma range it actually used back
     * out to full black-to-white costs one pass over a small canvas and turns a fair
     * number of "no code here" frames into decodes.
     */
    function stretchContrast(canvas) {
        var ctx = canvas.getContext('2d', { willReadFrequently: true });
        var image = ctx.getImageData(0, 0, canvas.width, canvas.height);
        var data = image.data;
        var min = 255, max = 0, i;

        for (i = 0; i < data.length; i += 4) {
            // Rec. 601 luma, integer-weighted; the exact coefficients matter less than
            // being consistent between the measuring pass and the applying pass.
            var luma = (data[i] * 77 + data[i + 1] * 150 + data[i + 2] * 29) >> 8;
            if (luma < min) min = luma;
            if (luma > max) max = luma;
        }

        var span = max - min;
        if (span < 8) return false;   // Flat frame: a wall, a hand, a lens cap.

        var scale = 255 / span;
        for (i = 0; i < data.length; i += 4) {
            var v = (data[i] * 77 + data[i + 1] * 150 + data[i + 2] * 29) >> 8;
            var out = (v - min) * scale;
            out = out < 0 ? 0 : (out > 255 ? 255 : out);
            data[i] = data[i + 1] = data[i + 2] = out;
        }
        ctx.putImageData(image, 0, 0);
        return true;
    }

    function drawRegion(video, canvas, ratio) {
        var vw = video.videoWidth, vh = video.videoHeight;
        if (!vw || !vh) return false;

        var size = Math.floor(Math.min(vw, vh) * ratio);
        var sx = Math.floor((vw - size) / 2);
        var sy = Math.floor((vh - size) / 2);

        // 1:1. The source pixels are the whole point; resampling them here would undo
        // the reason we asked for a high-resolution stream.
        canvas.width = size;
        canvas.height = size;
        canvas.getContext('2d', { willReadFrequently: true })
              .drawImage(video, sx, sy, size, size, 0, 0, size, size);
        return true;
    }

    function drawFull(video, canvas) {
        var vw = video.videoWidth, vh = video.videoHeight;
        if (!vw || !vh) return false;
        canvas.width = vw;
        canvas.height = vh;
        canvas.getContext('2d', { willReadFrequently: true }).drawImage(video, 0, 0, vw, vh);
        return true;
    }

    // ------------------------------------------------------------- camera

    var CAMERA_CONSTRAINTS = {
        audio: false,
        video: {
            facingMode: { ideal: 'environment' },
            // Asking for more than we display on purpose: the extra pixels are for the
            // decoder, not the viewfinder.
            width: { ideal: 1920 },
            height: { ideal: 1080 },
            frameRate: { ideal: 30 },
        },
    };

    function requestCamera() {
        if (!global.navigator.mediaDevices || !global.navigator.mediaDevices.getUserMedia) {
            return Promise.reject(new Error('This browser cannot open a camera.'));
        }
        return global.navigator.mediaDevices.getUserMedia(CAMERA_CONSTRAINTS);
    }

    /*
     * Is this stream still something a camera is feeding?
     *
     * A MediaStream whose tracks have ended looks completely normal - it is still an
     * object, it is still attached to the <video>, and no event you were listening for
     * necessarily fired. It just never produces another frame.
     */
    function streamIsLive(stream) {
        if (!stream) return false;
        return stream.getVideoTracks().some(function (t) { return t.readyState === 'live'; });
    }

    /*
     * Picks up the stream the shell started before this script parsed.
     *
     * The permission prompt and the camera warm-up are the slowest part of a cold
     * launch, so the page fires them in an inline script in <head> and we collect the
     * result here. Falls back to starting one if that did not happen.
     *
     * The cached promise is only reused while the stream behind it is actually live.
     * It used to be returned unconditionally, which meant that once the platform had
     * taken the camera back - see revive() - every subsequent start() cheerfully
     * attached the same dead stream and showed a black viewfinder with no error at all.
     */
    async function acquireStream() {
        var cached = global.__cameraPromise;
        if (cached) {
            var existing;
            try {
                existing = await cached;
            } catch (e) {
                // Cleared so a later attempt is a real one; a rejection cached here
                // forever outlives the reason for it, and permission granted from the
                // gate a second later could never take effect.
                global.__cameraPromise = null;
                throw e;
            }
            if (streamIsLive(existing)) return existing;
        }
        global.__cameraPromise = requestCamera();
        return global.__cameraPromise;
    }

    async function applyFocusHints(track) {
        if (!track.getCapabilities) return;
        try {
            var caps = track.getCapabilities();
            var advanced = [];
            if (caps.focusMode && caps.focusMode.indexOf('continuous') !== -1) {
                advanced.push({ focusMode: 'continuous' });
            }
            if (advanced.length) await track.applyConstraints({ advanced: advanced });
        } catch (e) { /* the stream is still usable without these */ }
    }

    function hasTorch(track) {
        try {
            return !!(track.getCapabilities && track.getCapabilities().torch);
        } catch (e) {
            return false;
        }
    }

    // ------------------------------------------------------------- the scanner

    /*
     * Creates a scanner bound to a <video>.
     *
     * `onResult(text)` is called with whatever decoded. It returns true to accept and
     * stop, or false to ignore and keep scanning - which is how the same loop serves
     * both receipt scanning and activation without a second code path.
     */
    function createScanner(video, callbacks) {
        callbacks = callbacks || {};
        var canvas = global.document.createElement('canvas');
        var stream = null;
        var track = null;
        var running = false;
        var busy = false;
        var attempts = 0;
        var startedAt = 0;
        var struggling = false;
        var lastFrameAt = 0;
        var engineFailure = null;
        var engineOk = false;
        var hasNative = false;

        function report(event, payload) {
            if (typeof callbacks[event] === 'function') {
                try { callbacks[event](payload); } catch (e) { /* keep scanning */ }
            }
        }

        async function attempt() {
            if (busy || !running) return;
            busy = true;
            try {
                attempts += 1;
                var wide = attempts % FULL_FRAME_EVERY === 0;
                var drew = wide ? drawFull(video, canvas) : drawRegion(video, canvas, ROI_RATIO);
                if (!drew) return;

                var text = await decodeWithNative(canvas);

                if (!text) {
                    if (attempts % STRETCH_EVERY === 0) stretchContrast(canvas);
                    try {
                        text = await decodeWithZXing(canvas);
                        engineOk = true;
                    } catch (e) {
                        // A decoder that cannot run is not "no code in this frame", and
                        // must never be mistaken for it. Reported once, loudly: the
                        // alternative is a scanner that looks alive and never decodes
                        // anything for the rest of its life.
                        //
                        // Only fatal when the platform has no native reader either -
                        // with one, this engine is the backup and its absence is a
                        // degradation rather than a dead scanner.
                        if (!engineFailure && !hasNative) {
                            engineFailure = e;
                            report('onEngineFailure', e);
                        }
                    }
                }

                if (!struggling && Date.now() - startedAt > STRUGGLE_AFTER_MS) {
                    struggling = true;
                    report('onStruggling');
                }

                if (text) {
                    // The callback returning false means "not for me, keep looking" -
                    // a foreign QR code in shot, say. Anything else counts as accepted.
                    var accepted = typeof callbacks.onResult === 'function'
                        ? callbacks.onResult(text) !== false
                        : true;
                    if (accepted) {
                        // Resets the clock so a stack of receipts scanned back to back
                        // never trips the "having trouble" hint.
                        struggling = false;
                        startedAt = Date.now();
                        attempts = 0;
                    }
                }
            } catch (e) {
                report('onError', e);
            } finally {
                busy = false;
            }
        }

        function pump() {
            if (!running) return;
            var now = Date.now();
            if (now - lastFrameAt >= FRAME_INTERVAL_MS) {
                lastFrameAt = now;
                attempt();
            }
            // requestVideoFrameCallback fires per decoded frame rather than per repaint,
            // so it does not spin when the camera is slower than the display.
            if (video.requestVideoFrameCallback) video.requestVideoFrameCallback(pump);
            else global.requestAnimationFrame(pump);
        }

        return {
            async start() {
                if (running) return;
                stream = await acquireStream();
                video.srcObject = stream;
                video.setAttribute('playsinline', '');   // iOS: do not go fullscreen native.
                video.muted = true;
                await video.play();

                track = stream.getVideoTracks()[0] || null;
                if (track) await applyFocusHints(track);

                hasNative = !!(await getBarcodeDetector());

                running = true;
                startedAt = Date.now();
                attempts = 0;
                struggling = false;
                report('onStarted', { torch: track ? hasTorch(track) : false, native: hasNative });
                pump();

                // Warmed here rather than on first use: compiling the module on the
                // frame that needs it shows up as a visible stall. A failure surfaces
                // now, at start, instead of hiding inside frames that look empty.
                warmDecoder().then(function () { engineOk = true; }, function (e) {
                    if (!hasNative && !engineFailure) {
                        engineFailure = e;
                        report('onEngineFailure', e);
                    }
                });
            },

            stop() {
                running = false;
                if (stream) {
                    stream.getTracks().forEach(function (t) { t.stop(); });
                    // The shared promise held a stream that is now dead; drop it so a
                    // later start() opens a fresh one.
                    if (global.__cameraPromise) global.__cameraPromise = null;
                }
                stream = null;
                track = null;
                video.srcObject = null;
            },

            pause() { running = false; },

            /*
             * Hands the camera back to the OS without forgetting there was one.
             *
             * stop() is for a scanner that is finished with; this is for one nobody is
             * looking at. Now that Scan, History and Diagnostics share a document, the
             * scan view is not destroyed when the user walks away from it - which is
             * the entire point, because destroying it is what cost a permission prompt
             * on the way back - but an open camera behind a history list is a lit
             * indicator light and a real battery draw for a sensor feeding nothing.
             *
             * The tracks stop; `stream` is deliberately kept, dead, so revive() can
             * tell "released, reopen it" from "never started". Reopening inside the
             * same document does not re-prompt: the grant belongs to the document and
             * the document is still here.
             */
            release() {
                running = false;
                if (!stream) return;
                stream.getTracks().forEach(function (t) { t.stop(); });
                // The shared promise is holding a stream that is now dead.
                global.__cameraPromise = null;
            },

            resume() {
                if (running || !stream) return;
                running = true;
                startedAt = Date.now();
                attempts = 0;
                struggling = false;
                pump();
            },

            /*
             * Reopens the camera when the platform has quietly taken it away.
             *
             * A phone call, the lock screen, another app wanting the camera, or simply
             * enough time in the background all end the video track - and nothing tells
             * the page. What the user comes back to is a viewfinder frozen on its last
             * frame, or black, with every control still enabled and no error anywhere.
             * Scanning silently does nothing from then on.
             *
             * That is the state that teaches a field user to reload the page, which on
             * WebKit means answering the camera permission prompt over again. Reopening
             * it here is what keeps that from ever being the fix.
             */
            async revive() {
                if (!stream || streamIsLive(stream)) return false;

                global.__cameraPromise = null;   // Do not hand back the dead one.
                stream = await acquireStream();
                video.srcObject = stream;
                try {
                    await video.play();
                } catch (e) { /* a tap resumes it; the stream itself is fine */ }

                track = stream.getVideoTracks()[0] || null;
                if (track) await applyFocusHints(track);
                report('onStarted', { torch: track ? hasTorch(track) : false, native: hasNative });
                return true;
            },

            get torchAvailable() { return track ? hasTorch(track) : false; },

            /* What can actually read a code right now, for the diagnostics screen. */
            get engineStatus() {
                return {
                    native: hasNative,
                    wasm: engineOk,
                    failure: engineFailure ? engineFailure.message : null,
                    working: hasNative || engineOk,
                };
            },

            async setTorch(on) {
                if (!track || !hasTorch(track)) return false;
                try {
                    await track.applyConstraints({ advanced: [{ torch: !!on }] });
                    return true;
                } catch (e) {
                    return false;
                }
            },

            /*
             * Grabs a still.
             *
             * Tried at the sensor's full resolution first, which is often several times
             * what the preview stream carries, and decoded once before giving up on the
             * code - a frame that video could not read frequently reads here. Whatever
             * happens, the caller gets a JPEG small enough to sit in an outbox and cross
             * a mobile connection.
             */
            async capture() {
                if (!track) throw new Error('Camera is not running.');
                var full = global.document.createElement('canvas');
                var bitmap = null;

                if (global.ImageCapture) {
                    try {
                        var capture = new global.ImageCapture(track);
                        var blob = await capture.takePhoto();
                        bitmap = await createImageBitmap(blob);
                    } catch (e) { bitmap = null; }
                }

                if (bitmap) {
                    full.width = bitmap.width;
                    full.height = bitmap.height;
                    full.getContext('2d').drawImage(bitmap, 0, 0);
                    bitmap.close();
                } else if (!drawFull(video, full)) {
                    throw new Error('No frame to capture.');
                }

                var text = await decodeWithNative(full);
                if (!text) {
                    try { text = await decodeWithZXing(full); } catch (e) { text = null; }
                }

                return { text: text, blob: await toJpeg(full) };
            },
        };
    }

    /*
     * Down to something a phone can hold hundreds of and still sync over 3G.
     *
     * 1600px on the long edge keeps a receipt's printed text legible to the vision
     * model that reads photos server-side, which is the only consumer that matters.
     */
    function toJpeg(canvas, maxEdge, quality) {
        maxEdge = maxEdge || 1600;
        quality = quality || 0.8;

        var w = canvas.width, h = canvas.height;
        var scale = Math.min(1, maxEdge / Math.max(w, h));
        var target = canvas;

        if (scale < 1) {
            target = global.document.createElement('canvas');
            target.width = Math.round(w * scale);
            target.height = Math.round(h * scale);
            target.getContext('2d').drawImage(canvas, 0, 0, target.width, target.height);
        }

        return new Promise(function (resolve, reject) {
            target.toBlob(function (blob) {
                if (blob) resolve(blob);
                else reject(new Error('Could not encode the photo.'));
            }, 'image/jpeg', quality);
        });
    }

    // ------------------------------------------------------------- receipt URLs

    /*
     * Is this a TRA receipt code, and what is its verification code?
     *
     * Accepts a full verification URL or a bare `CODE_HHMMSS`, matching what the
     * server's parser accepts. Rejecting anything else at the camera is what keeps a
     * poster's QR code or a wifi barcode out of the queue.
     */
    function parseReceipt(text) {
        if (!text) return null;
        var match = RECEIPT_URL_RE.exec(String(text).trim());
        if (!match) return null;
        return {
            code: match[1],
            time: match[2] + ':' + match[3] + ':' + match[4],
            raw: String(text).trim(),
        };
    }

    global.Scanner = {
        create: createScanner,
        parseReceipt: parseReceipt,
        requestCamera: requestCamera,
        acquireStream: acquireStream,
        getBarcodeDetector: getBarcodeDetector,
        getZXing: getZXing,
        warmDecoder: warmDecoder,
        toJpeg: toJpeg,
        RECEIPT_URL_RE: RECEIPT_URL_RE,
        CAMERA_CONSTRAINTS: CAMERA_CONSTRAINTS,
    };
})(window);
