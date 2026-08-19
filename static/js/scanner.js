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
 *      camera is asked for 1920 on its long edge - and asked again once the track can
 *      be interrogated, because the first request is only a wish - and the region of
 *      interest is cut out at the sensor's own resolution and never scaled down.
 *      Downscaling to "go faster" is what makes a marginal code unreadable.
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

    // How long to let the camera reconfigure, and how many frames to let it settle
    // afterwards. Both deliberately short: this sits between the user pressing the
    // shutter and the photo appearing, and a still that arrives late enough to feel
    // broken is worse than one taken at the preview's resolution.
    var FULL_RES_DEADLINE_MS = 900;
    var SETTLE_FRAMES = 3;

    /* One delivered frame, where the browser will say so, and one paint otherwise. */
    function nextFrame(video) {
        return new Promise(function (resolve) {
            if (video.requestVideoFrameCallback) video.requestVideoFrameCallback(function () { resolve(); });
            else global.requestAnimationFrame(function () { resolve(); });
        });
    }

    /*
     * Waits for the video element to actually be producing the frames we asked for.
     *
     * `applyConstraints` resolving means the track has accepted the new mode, not that a
     * frame in it has arrived - and on a phone the camera is genuinely reconfiguring
     * hardware, which takes a moment. Drawing before that lands copies the old
     * resolution, or worse a torn frame from the middle of the switch.
     *
     * Two waits, because either alone is wrong. The dimensions must change - or the
     * deadline pass, since a camera is free to have given us nothing new and this may
     * not hang the shutter on it - and then a few more frames must go by: the first
     * frame at a new mode is routinely mis-exposed and still hunting for focus, which on
     * a QR code is the difference between a read and a retake.
     */
    async function settleAfterModeChange(video, wasEdge, deadlineMs) {
        var until = Date.now() + deadlineMs;
        while (Date.now() < until) {
            if (Math.max(video.videoWidth, video.videoHeight) !== wasEdge) break;
            await nextFrame(video);
        }
        for (var i = 0; i < SETTLE_FRAMES; i++) await nextFrame(video);
    }

    /*
     * The still, at the best resolution this camera actually has.
     *
     * This exists because of what the fallback used to be. Where `ImageCapture` is
     * missing - which means every iPhone, since WebKit has never shipped it - `capture`
     * simply drew the viewfinder, and the viewfinder is a preview stream: negotiated for
     * smooth playback at a size chosen to be cheap, routinely 720x1280 on a phone whose
     * sensor holds twelve megapixels. A receipt's QR code in such a frame is about sixty
     * pixels across, under two pixels per module, and it is unreadable by this decoder,
     * by the one on the server, and by any preprocessing either of them could apply.
     * That single fallback is why server-side decoding on iOS never once succeeded.
     *
     * So the track is asked for its maximum just long enough to take the frame, and put
     * back afterwards. Not left raised: the preview loop decodes ten frames a second and
     * doing that at full sensor resolution is a hot phone and a stuttering viewfinder,
     * which is the trade PREVIEW_TARGET_EDGE already settled for the live path.
     *
     * Every step is allowed to fail into the old behaviour. A camera that will not
     * change mode, or has no capabilities to report, still yields the preview frame -
     * which is exactly what this used to do, and is worth having over an error.
     */
    async function grabAtFullResolution(track, video, canvas) {
        var restore = null;
        var wasEdge = Math.max(video.videoWidth || 0, video.videoHeight || 0);

        try {
            var caps = track.getCapabilities ? track.getCapabilities() : null;
            var settings = track.getSettings ? track.getSettings() : {};
            var upright = (settings.height || 0) > (settings.width || 0);
            var limit = caps && (upright ? caps.height : caps.width);
            var current = Math.max(settings.width || 0, settings.height || 0);

            if (limit && limit.max && limit.max > current) {
                // Only the long edge is constrained, so the camera keeps its own aspect
                // ratio instead of being asked for a mode it does not have - the same
                // reasoning as raiseResolution, and the same failure if ignored.
                restore = upright ? { height: { ideal: settings.height } }
                                  : { width: { ideal: settings.width } };
                await track.applyConstraints(
                    upright ? { height: { ideal: limit.max } } : { width: { ideal: limit.max } });
                await settleAfterModeChange(video, wasEdge, FULL_RES_DEADLINE_MS);
            }
        } catch (e) { /* the preview resolution is still a photograph */ }

        var drew = drawFull(video, canvas);

        // Restored even when the draw failed, or the viewfinder is left running at full
        // sensor resolution for the rest of the session.
        if (restore) {
            try {
                await track.applyConstraints(restore);
            } catch (e) { /* it will be corrected the next time the camera is opened */ }
        }
        return drew;
    }

    // ------------------------------------------------------------- camera

    /*
     * Asking for more than we display on purpose: the extra pixels are for the decoder,
     * not the viewfinder.
     *
     * Asked for as a long edge and an aspect ratio rather than as a width and a height,
     * which is not a stylistic choice. A phone held upright produces a portrait stream,
     * and `width: 1920, height: 1080` describes a landscape one; browsers resolve that
     * pair by aspect ratio first and then hand back the nearest mode they have, which
     * on a portrait device is routinely 720x1280 - a megapixel, from a camera holding
     * twelve. That is not a theory: a receipt reached the server at exactly 720x1280,
     * with a QR code about 110px across, which is three pixels a module before the JPEG
     * gets to it and below what any amount of server-side preprocessing can recover.
     *
     * `aspectRatio` is expressed the same way round for either orientation - the spec
     * defines it as width over height, so 9/16 is what asks for an upright frame.
     */
    var CAMERA_CONSTRAINTS = {
        audio: false,
        video: {
            facingMode: { ideal: 'environment' },
            height: { ideal: 1920 },
            aspectRatio: { ideal: 9 / 16 },
            frameRate: { ideal: 30 },
        },
    };

    /*
     * What the track will give, once we can see what it has.
     *
     * The constraints above are a request made before anything is known about the
     * camera, and a browser is free to answer one with whatever it feels like - `ideal`
     * carries no obligation. Capabilities are the other half of the conversation: they
     * are the modes this camera actually has, and they can only be read after the
     * stream exists. So the resolution is asked for twice, and the second time it is
     * asked with the answer in hand.
     *
     * Capped rather than maximised. The top mode on a modern sensor can be 4000px and
     * 30fps of it is a preview that stutters and a phone that gets hot; 1920 on the long
     * edge is where a receipt's QR code stops being the limiting factor, and past it
     * the gain goes to the still (which ImageCapture takes at full sensor resolution
     * anyway) rather than to the live decode.
     */
    var PREVIEW_TARGET_EDGE = 1920;

    async function raiseResolution(track) {
        if (!track.getCapabilities || !track.applyConstraints) return;
        try {
            var caps = track.getCapabilities();
            var settings = track.getSettings ? track.getSettings() : {};
            var upright = (settings.height || 0) > (settings.width || 0);
            var limit = upright ? caps.height : caps.width;
            if (!limit || !limit.max) return;

            var target = Math.min(PREVIEW_TARGET_EDGE, limit.max);
            if (target <= Math.max(settings.width || 0, settings.height || 0)) return;

            // Only the long edge, so the browser keeps the camera's own aspect ratio
            // rather than being asked for a mode that does not exist and falling back
            // to something smaller than it started with.
            await track.applyConstraints(
                upright ? { height: { ideal: target } } : { width: { ideal: target } });
        } catch (e) { /* the stream is still usable at whatever it negotiated */ }
    }

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
                if (track) {
                    await applyFocusHints(track);
                    await raiseResolution(track);
                }

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
                if (track) {
                    await applyFocusHints(track);
                    await raiseResolution(track);
                }
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
             *
             * Both routes to those pixels are here because neither is available
             * everywhere, and the difference between them is the difference between a
             * receipt TRA confirms and one a model guesses at. See grabAtFullResolution.
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
                } else if (!await grabAtFullResolution(track, video, full)) {
                    throw new Error('No frame to capture.');
                }

                var text = await decodeStill(full);
                return { text: text, blob: await toJpeg(full, text ? null : UNDECODED_MAX_EDGE) };
            },
        };
    }

    /*
     * The same read, applied to a picture that already exists.
     *
     * Receipts do not only arrive through this app's viewfinder. They arrive as photos
     * someone already took - a driver who snapped a fuel receipt before the app was
     * installed, a receipt WhatsApped in by a colleague, the pile that built up while a
     * phone was offline in the field. Those are the same receipts and deserve the same
     * treatment, so a picked file goes through the identical two engines a live frame
     * does, and a decode here means it is queued as a verified TRA scan rather than as
     * a photograph of one.
     *
     * A gallery photo is usually far larger than a preview frame - twelve megapixels is
     * ordinary - so the decode canvas is capped. Past a couple of thousand pixels the
     * extra detail buys nothing on a QR code and costs a buffer per file, on a phone
     * that may be importing twenty of them at once.
     */
    var IMPORT_DECODE_MAX_EDGE = 2000;

    /*
     * The upload size for a photo whose code this phone could not read.
     *
     * Two sizes rather than one, because the two photos are not carrying the same job.
     * A photo whose QR already decoded here is evidence - it is filed beside a receipt
     * whose numbers came from TRA, and 2000px is comfortably enough to look at. A photo
     * that did not decode is the only copy of a receipt nobody has read yet, and every
     * pass the server makes at it is made on whatever pixels this line decided to keep.
     *
     * The server is not merely asking the same question again: it stretches the
     * contrast, and it crops the frame into overlapping tiles and triples each one, so
     * the code is judged against its own corner of the photograph rather than against
     * the whole picture (utils/qr.py). That preprocessing is what makes the extra
     * pixels worth carrying - but it cannot recover a module that was resampled away
     * before the upload, and at 2000px a code that started at 150px in a 12MP frame
     * arrives at about 74px, which is two pixels a module and below anything that
     * decodes. At 3000 the same code lands near 110px and comes back.
     *
     * Measured, not reasoned about: at 2000 that code comes back 'no code found' from
     * the full ladder, and at 3000 it decodes on the first pass. It costs about 50KB
     * more - 98KB against 48KB - on the photos that take this path, which are the
     * minority of them and exactly the minority where the alternative is a receipt
     * transcribed by a model instead of confirmed by the revenue authority.
     *
     * Not higher than 3000 because the next band down does not come back either way: a
     * 120px code is still under two and a quarter pixels a module at 3000, and the
     * answer for it is the verification code printed underneath in plain type, which
     * the vision model reads and `reconstructed_receipt_url` turns back into a portal
     * address.
     */
    var UNDECODED_MAX_EDGE = 3000;

    /*
     * Every read this phone can make of a single still, cheapest first.
     *
     * The live loop gets ten frames a second and can afford to try one thing on each.
     * A still gets one look, so it is worth spending all three passes on it here rather
     * than uploading and hoping - a decode on this side means the receipt is queued as
     * a verified TRA scan instead of as a photograph of one.
     *
     * The contrast pass works on a copy, and that is not fastidiousness. `stretchContrast`
     * rewrites the pixels it is given, and on the capture path the canvas it would be
     * given is also the canvas that gets uploaded: stretching it in place would send the
     * server a grey, clipped rendering of the receipt to run its own decoder over and
     * show its vision model. The copy is bounded on its long edge because it is the
     * desperation pass, and a full-size copy of a 12MP frame is a large allocation to
     * make on a phone at the exact moment it is already holding the photograph.
     */
    async function decodeStill(canvas) {
        var text = await decodeWithNative(canvas);
        if (!text) {
            try { text = await decodeWithZXing(canvas); } catch (e) { text = null; }
        }
        if (text) return text;

        var boosted = global.document.createElement('canvas');
        var scale = Math.min(1, IMPORT_DECODE_MAX_EDGE / Math.max(canvas.width, canvas.height));
        boosted.width = Math.max(1, Math.round(canvas.width * scale));
        boosted.height = Math.max(1, Math.round(canvas.height * scale));
        boosted.getContext('2d', { willReadFrequently: true })
               .drawImage(canvas, 0, 0, boosted.width, boosted.height);

        if (!stretchContrast(boosted)) return null;
        try { return await decodeWithZXing(boosted); } catch (e) { return null; }
    }

    async function readImageFile(file) {
        var source = await loadBitmap(file);
        var canvas = global.document.createElement('canvas');
        var scale = Math.min(1, IMPORT_DECODE_MAX_EDGE / Math.max(source.width, source.height));

        canvas.width = Math.max(1, Math.round(source.width * scale));
        canvas.height = Math.max(1, Math.round(source.height * scale));
        canvas.getContext('2d').drawImage(source, 0, 0, canvas.width, canvas.height);
        if (source.close) source.close();

        var text = await decodeStill(canvas);

        // Re-encoded from the source rather than from the canvas above: that one has
        // been downscaled for the decoder. What gets uploaded should be the photograph,
        // at the size the server's vision model and its own QR decoder can work with.
        var full = global.document.createElement('canvas');
        var reload = await loadBitmap(file);
        full.width = reload.width;
        full.height = reload.height;
        full.getContext('2d').drawImage(reload, 0, 0);
        if (reload.close) reload.close();

        return { text: text, blob: await toJpeg(full, text ? null : UNDECODED_MAX_EDGE) };
    }

    /*
     * A picked file as something drawable, the right way up.
     *
     * Phones write orientation into EXIF instead of rotating the pixels, so a receipt
     * shot in portrait decodes sideways without this - and 'sideways' is one of the
     * states a QR finder pattern tolerates least once the image is also soft. The
     * option is honoured by every current browser and ignored by older ones, which is
     * why there are three attempts and not one.
     */
    async function loadBitmap(file) {
        if (global.createImageBitmap) {
            try {
                return await global.createImageBitmap(file, { imageOrientation: 'from-image' });
            } catch (e) { /* older Safari rejects the options argument outright */ }
            try {
                return await global.createImageBitmap(file);
            } catch (e) { /* fall through to the <img> path */ }
        }

        return new Promise(function (resolve, reject) {
            var url = URL.createObjectURL(file);
            var image = new global.Image();
            image.onload = function () {
                URL.revokeObjectURL(url);
                resolve(image);
            };
            image.onerror = function () {
                URL.revokeObjectURL(url);
                reject(new Error('That file could not be read as an image.'));
            };
            image.src = url;
        });
    }

    /*
     * Down to something a phone can hold hundreds of and still sync over 3G.
     *
     * 2000px on the long edge, raised from 1600. 1600 was chosen when the vision model
     * was the only thing that ever looked at an uploaded photo, and for reading printed
     * words it is plenty. It is not plenty for the server's QR decoder, which is the
     * other consumer now and the one that produces a receipt TRA confirms rather than
     * one a model transcribed.
     *
     * The difference is narrow and entirely real. A receipt photographed end to end
     * puts its QR code at roughly 150px in a 12MP frame; at 1600 that lands at about
     * 60px - three pixels a module before JPEG gets to it - and does not decode at any
     * amount of preprocessing. At 2000 the same code decodes, for about 70KB a photo.
     *
     * 2000 rather than more because this curve has a knee and it is here. Codes larger
     * than that band were never in danger, and each further step up recovers a thinner
     * slice of smaller ones for a steeper price - 2400px would buy the 125px code at
     * half again the bytes, on a device that syncs a day's receipts over 3G and stores
     * them until it can. Below the band the answer is not a bigger upload anyway: it is
     * the verification code the vision model reads off the paper in print, which is
     * legible long after the code above it has stopped scanning.
     *
     * That is the size for a photo whose code this phone has already read. When it has
     * not, the callers pass UNDECODED_MAX_EDGE instead - see the note there.
     */
    function toJpeg(canvas, maxEdge, quality) {
        maxEdge = maxEdge || 2000;
        quality = quality || 0.82;

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
        readImageFile: readImageFile,
        requestCamera: requestCamera,
        acquireStream: acquireStream,
        getBarcodeDetector: getBarcodeDetector,
        getZXing: getZXing,
        warmDecoder: warmDecoder,
        toJpeg: toJpeg,
        // Exported to be testable. Whether the still is taken at the sensor's
        // resolution or at the viewfinder's is the single decision that determines
        // whether a server-side decode is possible at all, and it is not observable
        // from anywhere else without a camera in the room.
        grabAtFullResolution: grabAtFullResolution,
        RECEIPT_URL_RE: RECEIPT_URL_RE,
        CAMERA_CONSTRAINTS: CAMERA_CONSTRAINTS,
    };
})(window);
