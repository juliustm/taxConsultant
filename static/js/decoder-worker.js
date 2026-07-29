/*
 * decoder-worker.js - ZXing, off the main thread.
 *
 * On a phone with no native BarcodeDetector - which is every iPhone, and every Android
 * whose Play Services barcode module has not been downloaded - ZXing is not the
 * fallback, it is the only decoder, and it runs on every frame. With the options this
 * app needs on thermal paper (tryHarder, tryRotate, tryInvert over a LocalAverage
 * binarizer) a single frame is tens to hundreds of milliseconds of straight-line WASM.
 *
 * Run on the main thread, ten times a second, that is not "a bit of jank": it is the
 * main thread, permanently. Alpine cannot re-render, taps queue up behind a decode,
 * and - the part that actually broke things - IndexedDB delivers its success events on
 * that same thread, so a save that finished in microseconds cannot report it. The app
 * had 5-second deadlines on local database writes and was hitting them, not because
 * storage was slow but because nothing could get a word in.
 *
 * So the decode happens here instead. The main thread's remaining job per frame is one
 * drawImage and one getImageData, and the pixels are transferred rather than copied.
 *
 * The decode options are deliberately not tuned down to compensate for anything. They
 * are what makes a faded, creased, watermarked receipt readable at all, and now that
 * this runs somewhere the UI does not care about, their cost buys accuracy for free.
 */
importScripts('/static/js/vendor/zxing-reader.js');

// Loaded from our own origin. The bundled default fetches its .wasm from a CDN, which
// would make the scanner dependent on the public internet - the one thing this app
// cannot assume. Requests from here go through the service worker, so this resolves
// against the precache offline.
var ZXING_WASM_URL = '/static/js/vendor/zxing_reader.wasm';

var ready = null;

function getModule() {
    if (ready) return ready;
    ready = (function () {
        var zxing = self.ZXingWASM;
        if (!zxing) return Promise.reject(new Error('The QR reader failed to load.'));
        // Checked once, here, rather than discovered per frame. Calling a decode entry
        // point that does not exist throws on every single frame, and since a failed
        // frame is indistinguishable from a frame with no code in it, the scanner looks
        // like it is working and simply never finds anything.
        if (typeof zxing.readBarcodesFromImageData !== 'function') {
            return Promise.reject(new Error('The QR reader is the wrong version (no readBarcodesFromImageData).'));
        }
        zxing.setZXingModuleOverrides({
            locateFile: function (path, prefix) {
                return path.endsWith('.wasm') ? ZXING_WASM_URL : (prefix || '') + path;
            },
        });
        return Promise.resolve(zxing.getZXingModule()).then(function () { return zxing; });
    })();
    // A failed load must not be cached as a permanently rejected promise, or a
    // transient miss on the wasm can never be retried.
    ready.catch(function () { ready = null; });
    return ready;
}

self.onmessage = function (event) {
    var message = event.data || {};

    // Compiling the module on the frame that needs it shows up as a visible stall even
    // out here, because the first decode then waits on it. The page asks for this at
    // start, while the camera is still warming up.
    if (message.type === 'warm') {
        getModule().then(
            function () { self.postMessage({ id: message.id, ready: true }); },
            function (error) { self.postMessage({ id: message.id, error: error.message }); }
        );
        return;
    }

    getModule().then(function (zxing) {
        var image = new ImageData(
            new Uint8ClampedArray(message.buffer), message.width, message.height
        );
        return zxing.readBarcodesFromImageData(image, message.options);
    }).then(function (results) {
        var text = results && results.length && results[0].text ? results[0].text : null;
        self.postMessage({ id: message.id, text: text });
    }, function (error) {
        self.postMessage({ id: message.id, error: (error && error.message) || 'decode failed' });
    });
};
