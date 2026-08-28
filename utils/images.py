# utils/images.py
"""
Getting an uploaded receipt photograph onto disk at a size worth keeping.

The app accepts a large upload (Config.MAX_CONTENT_LENGTH, 20MB) and stores a small
one, and those two numbers answer different questions. The ceiling is about never
turning a real receipt away at the door: a modern phone's 12MP frame, converted out of
HEIC by the browser and uploaded unmodified, clears 8MB without trying, and the direct
API path takes whatever a bot decides to send. What gets *kept* is a different
question, because nothing downstream can use those pixels:

  * The QR decoder bounds every photograph it opens before it does anything else
    (utils.qr.MAX_EDGE), so pixels above that line are read off disk and discarded on
    every single pass of the ladder - and the scanner's own measurements put the point
    where they stop buying a decode lower still. See STORED_MAX_EDGE.

  * The vision model is handed the file base64-encoded into a data URL
    (llm_processor.encode_image_to_base64). Every byte above what it can use is a byte
    encoded, sent over the wire and paid for, on an instance that may be processing a
    day's backlog.

  * And the file is kept forever, on the same persistent volume as the database.

So an upload is bounded on the way in, to STORED_MAX_EDGE - a number this app has
already measured on the scanner side rather than one picked here. See the note on it
below; the short version is that it is the point where extra pixels stopped buying a QR
decode, and getting that wrong is not a storage question but a verified-receipt one.

Two things this deliberately does not do.

It does not re-encode a photograph that is already within the cap. JPEG is lossy on
every generation, and the common path is already small - the scanner caps its own
uploads at 2000px, or 3000px for a frame whose code it could not read (Scanner.toJpeg),
and re-compressing those to save nothing would spend real QR modules for it. A photo
is touched only when it is genuinely too large, or when its pixels are not upright.

And it does not refuse anything. A file PIL cannot open is written to disk exactly as
it arrived and left to the pipeline, which already has a reason field and a failure
path for a photo it cannot read. Rejecting at intake would drop a receipt at the one
moment nobody is watching - the device has been told the upload succeeded - which is
the same reasoning that keeps a malformed URL acceptable at ingest.
"""
import base64
import io
import os

from PIL import Image, ImageOps

from utils import qr

# The long edge every stored photograph is bounded to.
#
# 3000 because that is the number this app has already measured, on the side that had to
# get it right first: Scanner.UNDECODED_MAX_EDGE. The scanner re-encodes a frame whose
# code it could not read to 3000px before uploading it, and the note there records why -
# a code that starts around 150px in a 12MP frame lands near 74px at 2000, which is two
# pixels a module and decodes nowhere, and near 110px at 3000, where it comes back. It
# also records why not higher: the next band down does not return at any size, and the
# answer for those is the verification code printed underneath in plain type.
#
# So 3000 is not a guess at a safe number, it is the knee of a curve somebody measured
# against real receipts. Storing above it keeps pixels that were shown not to buy a
# decode, and every photograph the scanner sends is already at or under it - which means
# this cap changes nothing for the app's own uploads and bounds only the direct API
# path, where an untouched 12MP original is exactly what arrives.
STORED_MAX_EDGE = 3000

# Belt and braces on a number that lives in two files. utils.qr bounds every photograph
# it opens to its own MAX_EDGE, so a storage cap above that would be storing pixels the
# decoder throws away unread - if this ever fails, the two have drifted and this file is
# the one that is wrong.
assert STORED_MAX_EDGE <= qr.MAX_EDGE

# What a re-encode costs in fidelity. Only ever applied to a photograph that was too
# large to keep as it arrived, so this is the second generation of JPEG on an image that
# started with far more detail than the cap keeps - not a squeeze applied to a frame
# that was already tight. High enough that the sharpening ladder in utils.qr still has
# clean module edges to work with, which is the consumer that notices first.
JPEG_QUALITY = 88

# EXIF tag 0x0112. A phone records which way up it was holding the camera here instead
# of rotating the pixels, so this is the difference between a receipt and a receipt on
# its side.
_ORIENTATION_TAG = 0x0112


def store_photo(photo, folder, filename):
    """
    Writes an uploaded photo into `folder`, bounded and upright.

    Returns the filename actually written, which is not always the one passed in: a
    re-encode produces JPEG bytes, and those must not be left sitting under a .png name.
    The name is what /uploads/<filename> serves the dashboard, and send_from_directory
    types the response off the extension - so a mismatch here is a Content-Type that
    contradicts the file, which browsers currently sniff their way past and which there
    is no reason to rely on. The caller stores what comes back.

    `photo` is a Werkzeug FileStorage. Its stream is read here and not left rewound for
    anyone else, because there is no other reader: this is the last thing that happens
    to an upload before it becomes a file on disk.
    """
    raw = photo.read()
    prepared = _bounded_jpeg(raw)
    if prepared is not None:
        filename = os.path.splitext(filename)[0] + '.jpg'

    with open(os.path.join(folder, filename), 'wb') as handle:
        handle.write(prepared if prepared is not None else raw)
    return filename


def _bounded_jpeg(raw, max_edge=STORED_MAX_EDGE, quality=JPEG_QUALITY):
    """
    The photograph as JPEG bytes within `max_edge` and the right way up, or None to say
    'what arrived is already fine, use that instead'.

    None rather than the original bytes so the caller cannot accidentally lose the
    distinction between 'unchanged' and 'changed', and so the untouched path never
    round-trips through an encoder at all.

    Two callers with two caps: what is kept on disk (STORED_MAX_EDGE, set by the QR
    decoder) and what is handed to the vision model (MODEL_MAX_EDGE, set by what it can
    read). One function because the work either way is the same three decisions -
    orientation, size, colour mode - and having two copies of them is how a photograph
    ends up upright in one path and sideways in the other.
    """
    try:
        # Deliberately not decoded yet. Size, format and EXIF all come from the header,
        # and the common answer below is 'leave it alone' - which should not cost a full
        # decode of a photograph nobody is going to re-encode.
        with Image.open(io.BytesIO(raw)) as probe:
            size, fmt = probe.size, probe.format
            rotated = _orientation_of(probe) not in (0, 1)
    except Exception:
        # Not an image, a truncated upload, or a format this Pillow was not built for.
        # See the module note: the pipeline reports this far better than a 400 here can.
        return None

    # Already small, already upright, already JPEG: the overwhelmingly common case,
    # since the scanner bounds its own uploads. Touching it would only add loss.
    if max(size) <= max_edge and not rotated and fmt == 'JPEG':
        return None

    try:
        with Image.open(io.BytesIO(raw)) as image:
            # Applied before the resize, not after: the cap is on the long edge of the
            # photograph as a person sees it, and a portrait frame recorded as landscape
            # has those two the wrong way round.
            if rotated:
                image = ImageOps.exif_transpose(image)

            longest = max(image.size)
            if longest > max_edge:
                scale = max_edge / longest
                image = image.resize(
                    (max(1, round(image.width * scale)), max(1, round(image.height * scale))),
                    Image.LANCZOS,
                )

            # RGB because JPEG has neither alpha nor a palette. A PNG of a receipt -
            # which the gallery import path does produce - would otherwise be an error
            # here rather than a receipt.
            if image.mode not in ('RGB', 'L'):
                image = image.convert('RGB')

            buffer = io.BytesIO()
            # No EXIF written back. The pixels are upright now, so an orientation tag
            # surviving the transpose would turn them a second time; and every reader
            # downstream already assumes JPEG (llm_processor labels the data URL
            # image/jpeg unconditionally), which this makes true rather than usual.
            image.save(buffer, format='JPEG', quality=quality, optimize=True,
                       progressive=True)
            return buffer.getvalue()
    except Exception:
        # A truncated file that only fails on decode, or Pillow's decompression-bomb
        # guard on something absurd. Either way the upload is still a receipt somebody
        # scanned, and the pipeline is where that gets a verdict.
        return None


# The long edge of the copy handed to the vision model.
#
# A stored photograph is bounded at STORED_MAX_EDGE, and that number is set by the QR
# decoder: 3000px is where a code that started small in a 12MP frame still has enough
# pixels a module to come back. The model is a different reader with a different need -
# it is looking at printed words, and the app's own measurements put 1600 as comfortably
# enough for those - so sending it the decoder's copy spends bytes nobody uses.
#
# It is not a small overspend. The file goes to the provider base64-encoded into a data
# URL (llm_processor), so it travels a third larger than it is stored, on an instance
# that may be working through a day's backlog on a shared line - and it is charged for
# as image tokens, which scale with the pixels.
#
# 2000 rather than 1600 because of what the model is now also asked to read: when no QR
# code can be decoded, the verification code printed underneath it in plain type is the
# route back to a receipt TRA confirms (see _receipt_from_photo). That is small print on
# thermal paper, and it is the one thing on the page where the margin is thin. 2000 is
# the size the scanner already treats as enough to read a receipt at
# (Scanner.toJpeg's default), and it halves the encoded payload against 3000.
MODEL_MAX_EDGE = 2000

# Higher than JPEG_QUALITY above, and for a different consumer. That one is a file kept
# forever; this one is a throwaway copy whose only job is to be read once, and ringing
# artefacts around small digits are exactly what costs a transcription.
MODEL_JPEG_QUALITY = 90


def encoded_for_model(path):
    """
    The photograph at `path` as base64 JPEG, bounded to MODEL_MAX_EDGE.

    Falls back to the file exactly as it sits on disk whenever Pillow cannot help -
    an unreadable header, a format this build was not compiled for, a resize that
    raises. A vision call on a larger image than necessary is a cost; a vision call
    that does not happen is a receipt that fails, and the second is much worse.
    """
    with open(path, 'rb') as handle:
        raw = handle.read()

    bounded = _bounded_jpeg(raw, MODEL_MAX_EDGE, MODEL_JPEG_QUALITY)
    return base64.b64encode(bounded if bounded is not None else raw).decode('utf-8')


def _orientation_of(image):
    """The EXIF orientation, or 0 when there is none to read."""
    try:
        return image.getexif().get(_ORIENTATION_TAG) or 0
    except Exception:
        return 0
