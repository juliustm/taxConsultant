# utils/qr.py
"""
Reading the QR code off a photographed receipt, on the server.

The phone tries first and is better placed to: it can move, refocus, turn the torch on
and try ten frames a second. But it gets one look at a still image and gives up, and a
photo that reaches us undecoded is not necessarily undecodable. This is the second
look.

It is worth being exact about what that second look has to work with, because the
obvious assumption is wrong twice over. The scanner does not hand us the frame it
failed on; it re-encodes the capture down to a bounded JPEG before uploading
(Scanner.toJpeg), and it runs this same engine on that capture first. So we are looking
at no more pixels than the decoder that already said no, through the same library, and
a pass that merely asks the whole frame again in the same way cannot come back with a
different answer. That is the constraint the ladder below is built around.

The second wrong assumption is about how many pixels that is, and it is the one that
kept this file from ever succeeding. The capture is only a full-resolution photograph
where `ImageCapture.takePhoto` exists, which on iOS it does not; there the scanner used
to fall back to grabbing the viewfinder frame, so an iPhone holding a twelve-megapixel
sensor uploaded a 720x1280 preview in which the receipt's QR was about sixty pixels
across - under two pixels a module, which no preprocessing in any library recovers.
Every server-side scan on such an instance returns 'no code found', and does so
truthfully. Scanner.capture now raises the track to the camera's own maximum before it
takes the still, which is what makes the passes below worth running at all; the
`image` field in every report is what to check first when they stop paying off again.

It matters more than a second look normally would, because of what a decode is worth
here: a receipt whose QR reads becomes a *verified* receipt, with its numbers parsed
from TRA's own page, instead of a set of figures a vision model read off paper. Those
are not the same receipt, and one round trip through zxing is a cheap way to find out
which one we have.

Three decisions shape the rest:

  * Preprocessing, not a second decoder. The engine here is the same ZXing-C++ the
    scanner runs in the browser, so a code it could not read will not suddenly read
    just because it is being asked again. What changes the answer is what the pixels
    look like when it is asked. Each pass in the ladder below is a different guess at
    what is wrong with the picture, cheapest first, and we stop at the first one that
    decodes.

    Only guesses the library does not already make, which is a sharper constraint than
    it sounds and is what the first version of this file got wrong. ZXing inverts,
    rotates and downscales the image it is handed, all on by default, and its
    LocalAverage binarizer already thresholds every region of the frame against its own
    neighbourhood. So a pass that stretches the contrast, or crops the frame into tiles
    to give each one its own threshold, is asking a question the binarizer has already
    answered - and measurably: over a corpus of 84 photographed receipts built to
    straddle the readable/unreadable line, the tile passes read exactly nothing the
    whole-frame passes had not already read. Twenty-four passes, none of them a rescue.

    What does change the answer is sharpening. A code that fails here fails because it
    is soft - a hand-held frame, a shallow focus, a JPEG - and softness is the one
    defect none of the above addresses. Resampling does not fix it either: LANCZOS
    interpolates smoothly across a module edge, so an upscaled blur is a larger blur.
    An unsharp mask puts the edge back, and the binarizer can then find it. On that same
    corpus, sharpening lifts the read rate from 41/84 to 56/84 - fifteen receipts now
    verified against TRA rather than transcribed by a model - with not one frame the old
    ladder read lost.

    It is slower, and worth being plain about that: a frame that fails now costs about
    770ms against 475ms, because doubling a photograph is a larger allocation than
    cropping nine pieces out of it. That is the right way to spend the time. The pass
    only runs on photographs the fast pass has already failed, and what stands at the
    end of that queue is a vision-model call costing several seconds and real money, to
    produce the weaker of the two receipts.

  * Every attempt is on the record. `scan` returns what it tried, what came back and
    how long it took, and the caller stores it on the submission. Without that, a photo
    that ends up read by the vision model is indistinguishable from a photo the decoder
    was never able to run on at all - a missing wheel, a truncated upload and a genuinely
    unreadable code all look identical from the outside, and they need three different
    things done about them.

  * Optional at import. zxing-cpp is a compiled wheel; if it is missing from an
    instance nothing here may take the app down with it. The functions degrade to
    'no code found', which is exactly the state the pipeline handled before this
    module existed - but they say so in the report rather than silently.
"""
import time

from PIL import Image, ImageFilter, ImageOps

try:
    import zxingcpp
except Exception as e:  # pragma: no cover - depends on the wheel being installed
    zxingcpp = None
    _IMPORT_ERROR = e
else:
    _IMPORT_ERROR = None

# A phone camera photograph, not a scanner's output. Anything past this is either a
# panorama or something trying to exhaust the worker's memory, and downscaling it to
# here costs no QR module that was ever going to be read.
MAX_EDGE = 4000

# The unsharp mask every pass past the first is built on.
#
# A large radius and a hard hand, both on purpose. The defect being undone is not
# sensor noise but a genuine loss of edge - a module boundary smeared across two or
# three pixels by focus, motion and JPEG together - so the radius has to be about the
# width of the smear rather than the single pixel a photographer would use, and the
# amount has to be enough to drive the two sides of that boundary back apart. The
# threshold keeps the mask off flat paper, so thermal-print speckle and the security
# watermark are not amplified into modules that were never there.
SHARPEN_RADIUS = 2
SHARPEN_PERCENT = 220
SHARPEN_THRESHOLD = 2

# The upscale passes, and what they are allowed to cost.
#
# Upscaling is only ever worth it in company with the sharpen above - alone it is a
# bigger blur - but together they are what reads a code printed small in a large frame,
# because the sampler needs somewhere to land inside a module and the mask needs room
# to put an edge there. Two factors rather than one because the right amount depends on
# how small the code is, which is exactly what we do not know.
#
# The budget is what keeps this honest at photograph sizes: tripling a 4000px frame is
# a 75-megapixel allocation to answer a question doubling it has usually already
# answered. What it works out to is a phone-sized frame getting both passes, an upload
# at the largest size the scanner sends getting the first, and a frame larger still -
# which only a direct API upload produces - getting neither. That is the right way
# round: each step up the scale is a code with more pixels of its own to start with, and
# a code at 4000px that these passes would have rescued is not one this decoder is
# losing receipts to.
UPSCALES = (2, 3)
MAX_UPSCALED_PIXELS = 40_000_000


def is_available() -> bool:
    """Whether a decode can be attempted at all on this instance."""
    return zxingcpp is not None


def unavailable_reason() -> str:
    """Why not, for the diagnostics screen. Empty when the decoder is present."""
    return '' if zxingcpp is not None else f'zxing-cpp is not installed ({_IMPORT_ERROR}).'


def scan(path) -> dict:
    """
    Everything the server-side decoder can say about the photograph at `path`.

    Returns a plain JSON-serialisable dict, because the caller stores it on the
    submission and an admin reads it back weeks later:

        outcome   one of 'decoded', 'not_a_receipt', 'no_code', 'unreadable',
                  'unavailable'
        url       the TRA receipt URL, when one was found - the only field the
                  pipeline itself acts on
        texts     every distinct string decoded, TRA receipt or not
        ignored   the decoded strings that were not TRA receipts
        pass      which rung of the ladder read it
        attempts  how many decode attempts were spent
        ms        how long the whole thing took
        image     the pixel dimensions actually decoded, after EXIF rotation and the
                  MAX_EDGE clamp - which is how a starved upload gets noticed
        detail    the failure in words, when there is one

    Never raises. This runs inside receipt processing, where a missing QR code is an
    ordinary outcome and not a failure.
    """
    report = {
        'outcome': 'unavailable', 'url': None, 'texts': [], 'ignored': [],
        'pass': None, 'attempts': 0, 'ms': 0, 'image': None,
        'detail': unavailable_reason(),
    }

    if zxingcpp is None:
        print(f'[QR] Skipping server-side decode: {report["detail"]}')
        return report

    started = time.monotonic()
    try:
        base = _load(path)
    except Exception as e:
        report.update(outcome='unreadable', detail=f'The file could not be opened as an image: {e}',
                      ms=_elapsed(started))
        print(f'[QR] Could not open {path}: {e}')
        return report

    report.update(detail='', image=f'{base.width}x{base.height}')
    texts, name, attempts = _decode(base)
    report.update(texts=texts, attempts=attempts, ms=_elapsed(started))

    if not texts:
        report['outcome'] = 'no_code'
        print(f'[QR] No QR code found in the photograph '
              f'({report["image"]}, {attempts} passes, {report["ms"]}ms).')
        return report

    report['pass'] = name
    print(f'[QR] Decoded {len(texts)} code(s) on the {name} pass ({report["ms"]}ms).')

    # Imported here rather than at module scope: utils.tra pulls in requests, and this
    # module is also read by the diagnostics endpoint purely to ask whether a decoder
    # exists.
    from utils.tra import parse_receipt_url

    for text in texts:
        try:
            parse_receipt_url(text)
        except (ValueError, TypeError):
            print(f'[QR] Ignoring a code that is not a TRA receipt: {text[:60]}')
            report['ignored'].append(text)
            continue
        report.update(outcome='decoded', url=text)
        return report

    report['outcome'] = 'not_a_receipt'
    return report


def summarise(report) -> dict:
    """
    A scan report as a sentence and a next step, for the submission page.

    Kept beside the decoder rather than in a template because the interesting part is
    the difference between the outcomes, and the difference is about QR codes: a photo
    the decoder never ran on is an install to fix, a photo it ran on and found nothing
    in is a photo to retake, and a photo carrying somebody else's barcode is neither.
    """
    if not report:
        return None

    outcome = report.get('outcome')
    if outcome == 'decoded':
        return {
            'tone': 'good',
            'title': 'The QR code was read on the server.',
            'detail': 'Its verification URL was taken to TRA, so the numbers on this '
                      'receipt come from the portal rather than from the photograph.',
        }
    if outcome == 'not_a_receipt':
        return {
            'tone': 'warn',
            'title': 'A QR code was read, but it was not a TRA receipt.',
            'detail': 'Something else in the frame carried a code - a product barcode, a '
                      'poster, a wifi label. The receipt itself was read by the vision '
                      'model instead.',
        }
    if outcome == 'no_code':
        return {
            'tone': 'warn',
            'title': 'No QR code could be read from the photograph.',
            'detail': 'Every pass ran and none of them found a code. Usually the code is '
                      'too small in the frame, too creased, or lost to glare; a closer, '
                      'flatter, more evenly lit retake normally decodes.',
        }
    if outcome == 'unreadable':
        return {
            'tone': 'bad',
            'title': 'The uploaded file could not be opened as an image.',
            'detail': report.get('detail') or 'The upload is truncated or is not a photo.',
        }
    if outcome == 'unavailable':
        return {
            'tone': 'bad',
            'title': 'The server-side QR decoder is not installed on this instance.',
            'detail': (report.get('detail') or unavailable_reason()) +
                      ' Every photographed receipt is being read by the vision model '
                      'instead of verified against TRA. Rebuilding the image fixes it.',
        }
    return {
        'tone': 'warn',
        'title': 'The photograph was not scanned.',
        'detail': 'This receipt had already been identified, so the decoder was not run '
                  'again.',
    }


def decode_texts(path) -> list:
    """Every distinct string decoded from the image at `path`. Never raises."""
    return scan(path)['texts']


def find_receipt_url(path):
    """
    The TRA receipt URL printed on the photographed receipt, or None.

    Every decoded string is put through the portal client's own parser, so a code that
    decodes to a wifi password, a product barcode or an advert is rejected rather than
    queued and failed hours later. Returns the raw decoded text, not a rebuilt URL:
    `utils.tra` rebuilds the request from the parsed code regardless, so passing the
    original through keeps what the paper actually said on the record.
    """
    return scan(path)['url']


def _elapsed(started):
    return int((time.monotonic() - started) * 1000)


def _decode(base):
    """
    (texts, pass name, attempts) for the first rung of the ladder that reads anything.

    Two sweeps of the same ladder, one per binarizer. LocalAverage first because it is
    the right answer for a photograph - it judges each part of the print against its own
    neighbourhood, which is what survives a shadow falling across the paper. The whole
    ladder is re-run under GlobalHistogram only once that has failed everywhere: one
    threshold for the frame is the wrong model for a photo, except on the evenly lit
    ones where it is strictly better, and a second sweep costs a fifth of a second
    against a vision call we are otherwise about to spend.
    """
    attempts = 0
    for binarizer, suffix in _binarizers():
        for name, image in _variants(base):
            attempts += 1
            try:
                results = zxingcpp.read_barcodes(
                    image, formats=zxingcpp.BarcodeFormat.QRCode, binarizer=binarizer)
            except Exception as e:
                print(f'[QR] Decoder failed on the {name}{suffix} pass: {e}')
                continue

            texts = []
            for result in results or []:
                text = (getattr(result, 'text', '') or '').strip()
                if text and text not in texts:
                    texts.append(text)

            if texts:
                return texts, f'{name}{suffix}', attempts

    return [], None, attempts


def _binarizers():
    """(binarizer, name suffix) for each sweep, in the order they are worth trying."""
    return (
        (zxingcpp.Binarizer.LocalAverage, ''),
        (zxingcpp.Binarizer.GlobalHistogram, ', global threshold'),
    )


def _load(path):
    """The photograph, upright and bounded, as an 8-bit greyscale image."""
    image = Image.open(path)
    # Phones record orientation in EXIF rather than rotating the pixels; a receipt
    # photographed in portrait arrives on its side without this.
    image = ImageOps.exif_transpose(image)
    image = image.convert('L')

    longest = max(image.size)
    if longest > MAX_EDGE:
        scale = MAX_EDGE / longest
        image = image.resize(
            (max(1, round(image.width * scale)), max(1, round(image.height * scale))),
            Image.LANCZOS,
        )
    return image


def _variants(base):
    """
    (name, image) pairs to try, in increasing order of cost and desperation.

    Ordered so the common case - a decent photograph of a decent print - is answered by
    the first pass and the rest never run. Every pass past the first sharpens, because
    on the evidence that is the only preprocessing that recovers a code this decoder
    would otherwise miss; see the module docstring for what was tried instead and what
    it was worth.
    """
    yield 'plain', base

    # The cheapest real guess, and the most productive: the code is there and in focus
    # enough, but its edges have been softened past what the binarizer will commit to.
    yield 'sharpened', _sharpen(base)

    # A tired thermal head prints grey on white rather than black on white. The stretch
    # alone is nearly a no-op against a local-average binarizer, but it gives the mask a
    # fuller range to work across, so the two together read prints that neither does.
    yield 'stretched and sharpened', _sharpen(ImageOps.autocontrast(base, cutoff=1))

    # For the code that is simply small in the frame: give the sampler room to land
    # inside a module, then put the module's edges back at the larger size. The order
    # matters - sharpening after the resize works on the smear the resize produced,
    # rather than having its work interpolated away.
    for factor in UPSCALES:
        if base.width * base.height * factor * factor > MAX_UPSCALED_PIXELS:
            break
        scaled = base.resize((base.width * factor, base.height * factor), Image.LANCZOS)
        yield f'{factor}x and sharpened', _sharpen(scaled, radius=factor)


def _sharpen(image, radius=SHARPEN_RADIUS):
    """
    `image` with an unsharp mask over it - the one pass that recovers a soft code.

    The radius follows the upscale factor for the scaled passes, because a resize
    spreads each module edge across proportionally more pixels and a mask narrower than
    the smear it is correcting sharpens the inside of the blur instead of its edge.
    """
    return image.filter(ImageFilter.UnsharpMask(
        radius=radius, percent=SHARPEN_PERCENT, threshold=SHARPEN_THRESHOLD))
