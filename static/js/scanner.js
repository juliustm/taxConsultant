/*
 * scanner.js - reading EFD QR codes off thermal paper.
 *
 * The hard part is not decoding a QR code; it is decoding *this* QR code. An EFD
 * receipt is printed by a thermal head onto a narrow roll, so by the time anyone
 * scans it the code is typically some combination of: physically small, printed with
 * a fading head, creased across the middle, curled, photographed in a dim shop, and
 * overlaid with the security watermark the paper stock already carried.
 *
 * Four decisions follow from that:
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
 *
 *   4. The photograph is what was on screen. The camera is configured once, when it
 *      opens, and never touched again - because on a phone whose rear camera is one
 *      virtual device in front of three lenses, changing a track's format resets the
 *      zoom, and resetting the zoom changes the lens. A shutter that reconfigures the
 *      camera to grab more pixels hands back a wider picture than the one that was
 *      framed, which is worth less than the smaller one: the extra pixels land on the
 *      table around the receipt, not on the receipt. So the still is the preview frame,
 *      cropped to what was on the glass and nothing narrower. See capture().
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
     * The same options, minus the pass a QR code cannot need.
     *
     * `tryRotate` re-runs the whole detector on a rotated copy of the image, which is
     * how a decoder finds a barcode printed sideways. A QR code's finder patterns are
     * three corners of a square: the detector reads it at any angle already, and the
     * rotated pass is a second full search that can only find what the first one did.
     * On the live loop - the only place the cost is paid ten times a second, and on
     * exactly the phones with no native detector to fall back to - that pass is pure
     * latency, and latency here reads as "the scanner is slow" and then as "the scanner
     * does not work", because a frame still being decoded is a frame the next one
     * queues behind.
     *
     * Dropped on the live path only. A still gets one look and can afford everything;
     * see decodeStill, which keeps the full set.
     */
    var ZXING_LIVE_OPTIONS = {};
    for (var zxingOption in ZXING_OPTIONS) {
        if (Object.prototype.hasOwnProperty.call(ZXING_OPTIONS, zxingOption)) {
            ZXING_LIVE_OPTIONS[zxingOption] = ZXING_OPTIONS[zxingOption];
        }
    }
    ZXING_LIVE_OPTIONS.tryRotate = false;

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

    async function decodeWithZXing(canvas, options) {
        options = options || ZXING_OPTIONS;
        var ctx = canvas.getContext('2d', { willReadFrequently: true });
        var image = ctx.getImageData(0, 0, canvas.width, canvas.height);

        // Transferred, not copied: at 1920x1080 the pixel buffer is eight megabytes, and
        // structured-cloning that ten times a second is its own performance problem.
        // getImageData hands us a fresh buffer each call, so giving it away is safe.
        var viaWorker = askWorker(
            { buffer: image.data.buffer, width: image.width, height: image.height, options: options },
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
        var results = await zxing.readBarcodesFromImageData(image, options);
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

    /*
     * The box an element occupies on screen, in CSS pixels, or null if it has none.
     *
     * getBoundingClientRect rather than clientWidth/clientHeight, because the element
     * this is asked about is the `position: fixed` viewfinder and its offset from the
     * top of the viewport is half the answer - a rect carries it, a width does not. The
     * clientWidth branch is for the test harness and for any element that answers a
     * rect of zeroes because it is not laid out yet.
     */
    function elementBox(el) {
        if (!el) return null;
        if (typeof el.getBoundingClientRect === 'function') {
            var rect = el.getBoundingClientRect();
            if (rect && rect.width && rect.height) {
                return { left: rect.left, top: rect.top, width: rect.width, height: rect.height };
            }
        }
        if (el.clientWidth && el.clientHeight) {
            return { left: 0, top: 0, width: el.clientWidth, height: el.clientHeight };
        }
        return null;
    }

    /*
     * Crops a captured frame down to exactly the part of it a person could see.
     *
     * The viewfinder is `object-fit: cover` (static/css/scan.css): the video is scaled
     * until it fills the screen and whatever hangs over the edges is cut off. A phone
     * held upright is a 9:19.5-ish screen showing a 9:16 or 3:4 sensor, so the part cut
     * off is substantial - a fifth of the frame down each side is routine - and it is
     * cut off only in the preview. `capture()` draws the whole video, so without this
     * the photograph would contain a band of floor, table and thumb down each side that
     * nobody had seen, let alone framed. What that costs is not only tidiness: those
     * bands are pixels inside the upload budget, so the receipt itself arrives smaller
     * than it needed to be, and the QR code with it.
     *
     * The screen, and nothing narrower. This briefly cropped to photo mode's corner
     * brackets instead, on the reasoning that the brackets are an instruction - "fit the
     * receipt in here" - and should therefore be the boundary. In the hand it was the
     * more confusing of the two mistakes: the brackets are inset from the screen edges,
     * so a receipt that was fully visible in the preview came back with its top or its
     * last line cut off, and there was nothing on screen to explain which. A viewfinder
     * that keeps less than it shows is not a viewfinder. So what is on the glass is what
     * is saved, and the brackets now sit at the edge of it (see .photo-frame) rather
     * than describing a second, smaller frame nobody was told about.
     *
     * The arithmetic is `cover` run backwards. `cover` scales the frame by
     * max(boxW/vw, boxH/vh) and centres it on the element, so a point on screen divides
     * back through that scale to land on a source pixel.
     *
     * Falls back to the window, and then to no crop: a photograph with too much in it
     * beats no photograph.
     */
    function cropToViewfinder(canvas, video) {
        var view = elementBox(video)
            || (global.innerWidth && global.innerHeight
                ? { left: 0, top: 0, width: global.innerWidth, height: global.innerHeight }
                : null);
        if (!view || !canvas.width || !canvas.height) return canvas;

        var target = view;

        var scale = Math.max(view.width / canvas.width, view.height / canvas.height);
        if (!scale || !isFinite(scale)) return canvas;

        // Where source pixel (0,0) sits on screen once `cover` has scaled and centred it.
        var originX = view.left + (view.width - canvas.width * scale) / 2;
        var originY = view.top + (view.height - canvas.height * scale) / 2;

        var sx = Math.round((target.left - originX) / scale);
        var sy = Math.round((target.top - originY) / scale);
        var w = Math.round(target.width / scale);
        var h = Math.round(target.height / scale);

        // Clamped, because rounding at the edges of a scaled rectangle can ask for a
        // pixel past the end of what the sensor gave us - and drawImage with a source
        // rectangle outside the image is a transparent band, not a crop.
        sx = Math.max(0, Math.min(sx, canvas.width - 1));
        sy = Math.max(0, Math.min(sy, canvas.height - 1));
        w = Math.max(1, Math.min(w, canvas.width - sx));
        h = Math.max(1, Math.min(h, canvas.height - sy));

        // Nothing to give up: do not re-encode the whole photograph for a rounding
        // difference.
        if (sx === 0 && sy === 0 && w === canvas.width && h === canvas.height) return canvas;

        var cropped = global.document.createElement('canvas');
        cropped.width = w;
        cropped.height = h;
        cropped.getContext('2d', { willReadFrequently: true })
               .drawImage(canvas, sx, sy, w, h, 0, 0, w, h);
        return cropped;
    }

    /*
     * One delivered frame, where the browser will say so, and one paint otherwise.
     *
     * The shutter waits on this before it draws. A tap is a moment of movement - the
     * finger lands, the phone dips - and the frame already sitting in the element is
     * the one from before that. Waiting for the next one costs a sixtieth of a second
     * and is the difference between a sharp receipt and a smeared one.
     */
    function nextFrame(video) {
        return new Promise(function (resolve) {
            if (video.requestVideoFrameCallback) video.requestVideoFrameCallback(function () { resolve(); });
            else global.requestAnimationFrame(function () { resolve(); });
        });
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

    /*
     * Close enough to leave alone.
     *
     * A format change is not free and it is not invisible: on a phone whose rear camera
     * is one virtual device in front of three lenses, reconfiguring the format resets
     * the zoom factor, and the zoom factor is which lens you are looking through. So a
     * stream that came back at 1600 when 1920 was asked for is not worth correcting -
     * the twenty percent of extra pixels does not decide any receipt, and the price of
     * asking is a viewfinder that jumps to the ultra-wide.
     */
    var RESOLUTION_SLACK = 1.25;

    /*
     * Everything this track needs, asked for once.
     *
     * One call rather than two, and that is the whole point of the function. Per spec
     * `applyConstraints` replaces a track's entire constraint set - it is not a patch -
     * so the old pair of calls, focus hints and then resolution, meant the second one
     * silently discarded the first. Continuous autofocus was requested on every camera
     * open and taken away again a moment later, every time, which on a phone being held
     * over a small printed code is most of the difference between a scanner that reads
     * and one that hunts.
     *
     * Every step is allowed to fail: a camera that reports no capabilities, or refuses
     * the constraint, still streams at whatever it negotiated, which is a working
     * scanner.
     */
    async function tuneCamera(track) {
        if (!track.applyConstraints) return;

        var caps = null;
        var settings = {};
        try {
            caps = track.getCapabilities ? track.getCapabilities() : null;
            settings = (track.getSettings ? track.getSettings() : {}) || {};
        } catch (e) { /* nothing to tune against; fall through and do nothing */ }
        if (!caps) return;

        var upright = (settings.height || 0) > (settings.width || 0);
        var current = Math.max(settings.width || 0, settings.height || 0);
        var wanted = {};

        var limit = upright ? caps.height : caps.width;
        if (limit && limit.max) {
            // Only the long edge, so the browser keeps the camera's own aspect ratio
            // rather than being asked for a mode that does not exist and falling back
            // to something smaller than it started with.
            var target = Math.min(PREVIEW_TARGET_EDGE, limit.max);
            if (target > current * RESOLUTION_SLACK) {
                if (upright) wanted.height = { ideal: target };
                else wanted.width = { ideal: target };
            }
        }

        var advanced = [];
        if (caps.focusMode && caps.focusMode.indexOf('continuous') !== -1) {
            advanced.push({ focusMode: 'continuous' });
        }
        if (!advanced.length && !wanted.height && !wanted.width) return;

        if (advanced.length) {
            wanted.advanced = advanced;
            // Pinned to what it is already streaming when the resolution needs no
            // change. Sending the focus hint on its own would clear the size and aspect
            // ratio getUserMedia negotiated and let the camera pick a format again -
            // which is the same reconfiguration, and the same lens reset, arriving by a
            // side door.
            if (!wanted.height && !wanted.width && current) {
                if (upright) wanted.height = { ideal: settings.height };
                else wanted.width = { ideal: settings.width };
            }
        }

        try {
            await track.applyConstraints(wanted);
        } catch (e) { /* the stream is still usable at whatever it negotiated */ }
    }

    /*
     * Which of this phone's cameras to open, remembered between launches.
     *
     * `facingMode: 'environment'` is a request for "a rear camera", not for a
     * particular one, and on a phone with three of them the browser picks. What it
     * picks is frequently the ultra-wide - it is the one the platform considers the
     * default - and an ultra-wide is the worst of the three for this job: the shortest
     * focal length, the closest minimum focus, and a receipt held at arm's length
     * rendered small in the middle of a very wide frame, which is the exact condition
     * under which a QR code stops having enough pixels per module to decode. There is
     * no constraint that says "the main camera"; the only way to reach a specific one
     * is by its deviceId, and the only way to know which deviceId is the good one is to
     * let whoever is holding the phone try them.
     *
     * localStorage rather than the IndexedDB store the rest of the app uses, and that
     * is forced rather than preferred: the camera is opened by an inline script in
     * <head> (templates/scan/shell.html) before pwa.js has parsed, let alone opened a
     * database. A preference that can only be read asynchronously is one that arrives
     * after the wrong camera is already running, and correcting it then costs a second
     * getUserMedia and a visible flicker on every single launch.
     */
    var CAMERA_PREF_KEY = 'karani.camera.deviceId';

    function preferredCameraId() {
        try {
            return global.localStorage.getItem(CAMERA_PREF_KEY) || null;
        } catch (e) {
            // Private mode on older WebKit throws on access rather than returning null.
            return null;
        }
    }

    function rememberCamera(deviceId) {
        try {
            if (deviceId) global.localStorage.setItem(CAMERA_PREF_KEY, deviceId);
            else global.localStorage.removeItem(CAMERA_PREF_KEY);
            return true;
        } catch (e) {
            return false;
        }
    }

    /*
     * The cameras this device has, as {deviceId, label, isDefault}.
     *
     * Labels are empty until a camera permission has been granted - the spec hides them
     * from an unprivileged page, because the list of cameras is itself a fingerprint -
     * so a caller that wants readable names has to ask after the stream is open. The
     * numbering is provided here rather than left to the caller so that an unlabelled
     * list is still something a person can work through: "Camera 2" is a poor name and
     * a perfectly good instruction.
     */
    async function listCameras() {
        if (!global.navigator.mediaDevices || !global.navigator.mediaDevices.enumerateDevices) {
            return [];
        }
        var devices;
        try {
            devices = await global.navigator.mediaDevices.enumerateDevices();
        } catch (e) {
            return [];
        }
        var preferred = preferredCameraId();
        var index = 0;
        return (devices || [])
            .filter(function (device) { return device.kind === 'videoinput'; })
            .map(function (device) {
                index += 1;
                return {
                    deviceId: device.deviceId,
                    label: device.label || ('Camera ' + index),
                    labelled: !!device.label,
                    selected: !!preferred && device.deviceId === preferred,
                };
            });
    }

    /*
     * The camera constraints, with a remembered camera named where there is one.
     *
     * `exact`, not `ideal`, and that matters: an `ideal` deviceId is a suggestion the
     * browser may decline, which would silently hand back the same default camera the
     * preference exists to escape and leave the diagnostics screen claiming a choice
     * that never took effect. `exact` fails loudly instead, and requestCamera below
     * turns that failure into a retry with no preference at all - so a stale id (a
     * cleaned-up device, a USB camera unplugged) costs one extra call rather than a
     * scanner that will not open.
     */
    function constraintsFor(deviceId) {
        if (!deviceId) return CAMERA_CONSTRAINTS;
        var video = {};
        for (var key in CAMERA_CONSTRAINTS.video) {
            if (Object.prototype.hasOwnProperty.call(CAMERA_CONSTRAINTS.video, key)) {
                video[key] = CAMERA_CONSTRAINTS.video[key];
            }
        }
        // The two cannot both be honoured, and naming a device is the more specific
        // request: facingMode left in place is what lets a browser answer with a
        // different camera than the one that was asked for.
        delete video.facingMode;
        video.deviceId = { exact: deviceId };
        return { audio: false, video: video };
    }

    /*
     * Which camera a stream is actually coming from, or null where the browser will not
     * say. Null is "cannot tell", never "wrong one" - see switchCamera.
     */
    function streamDeviceId(stream) {
        try {
            var tracks = stream && stream.getVideoTracks ? stream.getVideoTracks() : [];
            var first = tracks[0];
            return first && first.getSettings ? (first.getSettings().deviceId || null) : null;
        } catch (e) {
            return null;
        }
    }

    function requestCamera(deviceId) {
        if (!global.navigator.mediaDevices || !global.navigator.mediaDevices.getUserMedia) {
            return Promise.reject(new Error('This browser cannot open a camera.'));
        }
        var wanted = deviceId === undefined ? preferredCameraId() : deviceId;
        if (!wanted) return global.navigator.mediaDevices.getUserMedia(CAMERA_CONSTRAINTS);

        return global.navigator.mediaDevices.getUserMedia(constraintsFor(wanted))
            .catch(function (error) {
                // A remembered camera that no longer exists must never be the reason
                // this app cannot scan. Fall back to whatever the phone offers, and
                // leave the preference alone: an id that fails once because another app
                // holds the camera is not an id worth forgetting.
                if (error && error.name === 'NotAllowedError') throw error;
                return global.navigator.mediaDevices.getUserMedia(CAMERA_CONSTRAINTS);
            });
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
                        text = await decodeWithZXing(canvas, ZXING_LIVE_OPTIONS);
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
                    await tuneCamera(track);
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
                    await tuneCamera(track);
                }
                report('onStarted', { torch: track ? hasTorch(track) : false, native: hasNative });
                return true;
            },

            /*
             * Swaps to another of the phone's cameras, and remembers the choice.
             *
             * The old stream is stopped first and deliberately: a phone will not
             * generally hand out two of its rear cameras at once, so asking for the new
             * one while still holding the old is how this fails with NotReadableError on
             * exactly the devices that have a camera worth switching to.
             *
             * Which is also why the old stream cannot simply be dropped on failure.
             * Once it is stopped it is gone, so if the new camera will not open, this
             * reopens with no preference rather than leaving a viewfinder that is black
             * and a scanner that is running - the state that reads as "the app broke
             * when I touched the settings".
             */
            async switchCamera(deviceId) {
                var wasRunning = running;
                running = false;

                if (stream) stream.getTracks().forEach(function (t) { t.stop(); });
                global.__cameraPromise = null;

                var opened;
                var honoured;
                try {
                    opened = await requestCamera(deviceId);
                    // requestCamera answers an exact deviceId it cannot open by
                    // reopening with no preference at all, so a stream coming back is
                    // not the same thing as the switch having happened. Checked against
                    // what is really streaming, because a preference saved for a camera
                    // the phone declined is the bug this exists to stop: the settings
                    // screen ticks a lens that is not running, the viewfinder never
                    // changes, and every launch after this one pays for a getUserMedia
                    // that falls back anyway.
                    //
                    // A browser that will not report a deviceId at all counts as
                    // honoured. "Cannot tell" is not "wrong", and refusing to save on
                    // it would make the preference unusable on the phones that need it.
                    var got = streamDeviceId(opened);
                    honoured = !deviceId || !got || got === deviceId;
                } catch (e) {
                    opened = await requestCamera(null);
                    honoured = !deviceId;
                }
                rememberCamera(honoured ? deviceId : null);

                global.__cameraPromise = Promise.resolve(opened);
                stream = opened;
                video.srcObject = stream;
                try {
                    await video.play();
                } catch (e) { /* a tap resumes it; the stream itself is fine */ }

                track = stream.getVideoTracks()[0] || null;
                if (track) {
                    await tuneCamera(track);
                }

                report('onStarted', { torch: track ? hasTorch(track) : false, native: hasNative });
                if (wasRunning) {
                    running = true;
                    startedAt = Date.now();
                    attempts = 0;
                    struggling = false;
                    pump();
                }
                // Three facts rather than one id, because the caller's job is to tell
                // someone the truth about what just happened, and "here is a deviceId"
                // cannot distinguish a switch that worked from a phone that quietly
                // gave the same camera back.
                return {
                    requested: deviceId || null,
                    deviceId: streamDeviceId(opened),
                    honoured: honoured,
                };
            },

            get torchAvailable() { return track ? hasTorch(track) : false; },

            /* Which camera is actually feeding the viewfinder, for the settings list to
               tick - the preference says what was asked for, this says what was given. */
            get activeCameraId() {
                try {
                    return track && track.getSettings ? (track.getSettings().deviceId || null) : null;
                } catch (e) {
                    return null;
                }
            },

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
             * Grabs a still - the one the viewfinder was showing, and no more.
             *
             * This used to raise the track to the sensor's maximum for the duration of
             * the shutter and put it back afterwards, which was wrong in two ways that
             * only show up on real hardware.
             *
             * The first is what it looked like. Changing a track's format is not a
             * resolution change on a phone with three rear lenses; it is a
             * reconfiguration, and the camera comes back at its default zoom - which on
             * a modern iPhone means the ultra-wide. So the preview showed a receipt
             * filling the frame, the shutter silently switched lens, and the photograph
             * that came out was the same receipt small in the middle of a table. The
             * crop below could not save it: it crops to what the viewfinder showed, and
             * by then the viewfinder was showing something else.
             *
             * The second is what it cost. Two applyConstraints calls and up to a second
             * of waiting for frames to settle sat between the tap and the picture, on
             * the one interaction in this app where the phone is being held over a
             * receipt and the person is waiting.
             *
             * So the still is now the preview frame, taken as it is. The preview is
             * already asked for 1920 on its long edge when the camera opens (see
             * tuneCamera), which is where a receipt's QR code stops being the limiting
             * factor - and cropping it to the glass means those pixels land on what the
             * person framed instead of on the floor beside it.
             *
             * ImageCapture.takePhoto went with it, and for the same reason: it takes
             * from the sensor rather than from the preview stream, so where it exists
             * at all it has its own field of view and its own idea of the framing. A
             * photograph that does not match what was on screen is not a better
             * photograph.
             */
            async capture() {
                if (!track) throw new Error('Camera is not running.');

                // The frame after the tap, not the one before it.
                await nextFrame(video);

                var full = global.document.createElement('canvas');
                if (!drawFull(video, full)) throw new Error('No frame to capture.');

                // Cropped before it is decoded, not after, so that what the code was
                // read from is the same picture that gets filed. Decoding the wider
                // frame first would occasionally return a QR code from outside the
                // viewfinder - a poster behind the counter, the receipt underneath this
                // one - and queue it against a photograph that visibly does not contain
                // it, which is a worse answer than not reading it at all.
                var framed = cropToViewfinder(full, video);

                var text = await decodeStill(framed);
                return { text: text, blob: await toJpeg(framed, text ? null : UNDECODED_MAX_EDGE) };
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

    /*
     * The same photograph, re-encoded until it fits inside a byte budget.
     *
     * This exists because of the one failure in this app that a field user could do
     * nothing whatsoever about. A photo larger than the body limit of whatever proxy
     * sits in front of the server is refused with a 413 before Flask is ever reached,
     * and the app said so: "This photo is too large for the server to accept (HTTP
     * 413)", under a receipt that then sat in the outbox for good. Nobody standing in a
     * shop knows what an ingress body limit is, and no phone camera app offers to make
     * a picture smaller. The receipt was simply lost, and the person holding it had
     * been told it was their problem.
     *
     * It is not their problem. A photograph that will not fit is a photograph to
     * re-encode, and this is the only place that knows how. The ladder drops the long
     * edge and the quality together, because they trade differently: the first step
     * costs almost nothing visible, and by the last one the file is roughly a fifth of
     * what it was. It stops at the first step that fits, so a photo one kilobyte over
     * the line is not squeezed to the floor for it.
     *
     * The floor is deliberate. Below about 1280px a receipt's printed verification code
     * stops being legible to the vision model, and an illegible photograph that uploads
     * is worth less than a legible one that needs the server's limit raised - so the
     * smallest step is still a readable receipt, and if even that will not go through,
     * the caller is told rather than being handed a picture of nothing.
     *
     * Returns the original blob unchanged when it already fits, or when this phone
     * cannot decode it at all (an ImageBitmap failure, a browser with no canvas): a
     * photograph that might not fit still beats no photograph.
     */
    var SHRINK_LADDER = [[2400, 0.78], [2000, 0.74], [1600, 0.7], [1280, 0.62]];

    async function shrinkToFit(blob, maxBytes) {
        if (!blob || !maxBytes || blob.size <= maxBytes) return blob;

        var canvas;
        try {
            var source = await loadBitmap(blob);
            canvas = global.document.createElement('canvas');
            canvas.width = source.width;
            canvas.height = source.height;
            canvas.getContext('2d').drawImage(source, 0, 0);
            if (source.close) source.close();
        } catch (e) {
            return blob;
        }

        var smallest = blob;
        for (var i = 0; i < SHRINK_LADDER.length; i++) {
            var candidate;
            try {
                candidate = await toJpeg(canvas, SHRINK_LADDER[i][0], SHRINK_LADDER[i][1]);
            } catch (e) {
                break;
            }
            if (candidate.size < smallest.size) smallest = candidate;
            if (candidate.size <= maxBytes) return candidate;
        }
        // Everything this ladder can do, done. It may still be over the budget - that is
        // the caller's call to make, and it is a different answer from "nothing worked".
        return smallest;
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
        // Which camera, and remembering the answer. Read by the diagnostics screen,
        // and by the inline <head> script that opens the camera before this file has
        // parsed - which is why the preference lives in localStorage.
        listCameras: listCameras,
        preferredCameraId: preferredCameraId,
        rememberCamera: rememberCamera,
        CAMERA_PREF_KEY: CAMERA_PREF_KEY,
        // Exported to be testable: whether the photograph matches what was framed is
        // pure arithmetic on two aspect ratios, and not otherwise observable without a
        // phone in the room.
        cropToViewfinder: cropToViewfinder,
        getBarcodeDetector: getBarcodeDetector,
        getZXing: getZXing,
        warmDecoder: warmDecoder,
        toJpeg: toJpeg,
        // The way out of a 413. Exported because the sync loop is what discovers the
        // server's body limit, and this is what answers it.
        shrinkToFit: shrinkToFit,
        // Exported to be testable. Whether the camera is reconfigured after it has
        // opened is the decision that determines whether a photograph matches what was
        // on screen, and it is not observable from anywhere else without a phone in the
        // room.
        tuneCamera: tuneCamera,
        RECEIPT_URL_RE: RECEIPT_URL_RE,
        CAMERA_CONSTRAINTS: CAMERA_CONSTRAINTS,
    };
})(window);
