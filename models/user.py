# models/user.py
from flask_sqlalchemy import SQLAlchemy
from sqlalchemy import event
import uuid
from datetime import datetime

from utils.money import from_cents
from utils import fingerprint
from utils import products as product_text

db = SQLAlchemy()

# How much of a receipt's page to put in front of the person reading it.
#
# Three densities rather than a row of switches, because what is being chosen is not a
# set of panels - it is how much of the working the reader wants shown. Somebody filing
# the week's receipts wants the money and whatever is wrong with it; somebody asked
# about one receipt eighteen months later wants the provenance of every figure on it,
# down to what the model actually answered. Both are looking at the same page.
#
# Ordered, and read by rank: compact is a subset of standard is a subset of full, so
# every panel asks one question - is this instance reading at level N or higher - and
# nothing has to enumerate the levels it belongs to.
#
# Nothing exists only at a higher level. Every control and every hover card is reachable
# at compact too, on the page or one switch away; the level decides what is on screen
# without being asked for, never what the page can do.
RECEIPT_DETAIL_LEVELS = ('compact', 'standard', 'full')
DEFAULT_RECEIPT_DETAIL = 'standard'


def receipt_detail_rank(level):
    """A detail level as its 1-based rank, so a template can compare it with >=."""
    if level not in RECEIPT_DETAIL_LEVELS:
        level = DEFAULT_RECEIPT_DETAIL
    return RECEIPT_DETAIL_LEVELS.index(level) + 1


class InstanceConfig(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    admin_email = db.Column(db.String(120), unique=True, nullable=False)
    totp_secret = db.Column(db.String(100), unique=True, nullable=False)
    llm_provider = db.Column(db.String(50), nullable=True)
    llm_api_key = db.Column(db.String(200), nullable=True)
    # Which model to ask, when the defaults are not the right answer. Left blank on
    # almost every instance: utils/llm_processor holds a candidate list and walks past
    # any model the provider has retired. These exist because that is a race we cannot
    # always win - a hosted model can be decommissioned overnight, and when it is, an
    # instance owner needs a way to name a working one from the configure page rather
    # than waiting on a release. Blank means "decide for me".
    llm_text_model = db.Column(db.String(100), nullable=True)
    llm_vision_model = db.Column(db.String(100), nullable=True)

    # --- Who this instance files for ---
    # Without the TIN there is no way to tell a receipt issued to this business from
    # one issued to a walk-in customer, and that distinction decides whether the input
    # VAT on it is claimable at all. See utils/compliance.
    business_name = db.Column(db.String(200), nullable=True)
    business_tin = db.Column(db.String(50), nullable=True)
    business_vrn = db.Column(db.String(50), nullable=True)

    # How much of a receipt's page this instance wants on screen. One of
    # RECEIPT_DETAIL_LEVELS; NULL means the default, which is what every instance that
    # predates the setting has been reading all along. See receipt_detail() below.
    receipt_detail_level = db.Column(db.String(20), nullable=True)

    # --- The front door: what a stranger sees at '/' ---
    # An instance's address gets shared - with a bookkeeper, in a WhatsApp group, on a
    # business card - long before its owner necessarily wants a sales page hanging off
    # it. So the public page is a setting, not a fixture. See utils/branding.
    landing_mode = db.Column(db.String(20), nullable=True)
    brand_name = db.Column(db.String(120), nullable=True)
    brand_tagline = db.Column(db.String(200), nullable=True)
    brand_accent = db.Column(db.String(20), nullable=True)
    brand_logo_url = db.Column(db.String(500), nullable=True)
    # Where an interested visitor is sent. Deliberately a URL rather than a form: the
    # people who ask are qualified by hand, and that conversation does not belong in
    # somebody's private receipt database.
    landing_cta_label = db.Column(db.String(60), nullable=True)
    landing_cta_url = db.Column(db.String(500), nullable=True)

    # --- How hard to try to turn a photograph into a verified receipt ---
    # Whether a portal address may be rebuilt out of text the vision model read off the
    # paper, when no QR code could be decoded from it.
    #
    # On by default, because when it works it is worth more than everything else the
    # photo pipeline does: it replaces a model's reading of a crumpled print with TRA's
    # own record of the sale. But it is a guess made from a transcription, and a wrong
    # guess is expensive in a way the right one is not - a document that was never an EFD
    # receipt (a mobile-money SMS, a parking stub, a delivery note) yields a plausible
    # run of digits, becomes a portal address nobody will ever confirm, and then occupies
    # the retry schedule for two days while the one usable reading of it is discarded.
    #
    # So it is a setting on instances whose receipts do not photograph well, or that
    # collect a lot of documents that are not EFD receipts. NULL means on - the default
    # every existing instance has been running with.
    rebuild_url_from_text = db.Column(db.Boolean, nullable=True, default=True)

    post_callback_url = db.Column(db.String(500), nullable=True)
    s3_bucket_name = db.Column(db.String(200), nullable=True)
    s3_access_key_id = db.Column(db.String(200), nullable=True)
    s3_secret_access_key = db.Column(db.String(200), nullable=True)
    s3_region = db.Column(db.String(50), nullable=True)

    google_sheet_id = db.Column(db.String(200), nullable=True)
    google_service_account_json = db.Column(db.Text, nullable=True)

    def is_configured(self):
        return all([self.llm_provider, self.llm_api_key])

    def receipt_detail(self):
        """
        Which of the three densities the receipt page renders at.

        Read through here rather than off the column, because a NULL and a value written
        by a release that offered a fourth name have to mean the same thing: show what
        this instance has always been shown.
        """
        level = (self.receipt_detail_level or '').strip().lower()
        return level if level in RECEIPT_DETAIL_LEVELS else DEFAULT_RECEIPT_DETAIL

    def receipt_detail_rank(self):
        """The same answer as a rank, which is what the template actually compares."""
        return receipt_detail_rank(self.receipt_detail())

    def rebuilds_urls_from_text(self):
        """Whether the photo pipeline may guess a portal address from transcribed text.

        NULL means on: the column was added to instances that had been rebuilding all
        along, and a migration cannot know which of them wanted it off.
        """
        return self.rebuild_url_from_text is not False

class Device(db.Model):
    """
    A thing that submits receipts: a phone running the scanner, or a bot.

    Two credentials live here and they are deliberately different in kind.

    `api_key` is the original one - a permanent bearer token pasted into a server-side
    integration. It is kept unchanged because existing bots hold it.

    The scanner's credential is a *session*, held by exactly one phone at a time. An
    admin issues a single-use `enrolment_token`; the phone spends it and receives a
    session token whose hash lands in `session_token_hash`. Because that is one column
    rather than a table, activating a second phone necessarily overwrites the first,
    which is what makes "one link, one device" true by construction instead of by a
    rule somewhere that has to be remembered.
    """
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(100), nullable=False)
    api_key = db.Column(db.String(100), unique=True, nullable=False, default=lambda: str(uuid.uuid4()))

    created_at = db.Column(db.DateTime, nullable=True, default=datetime.utcnow)

    # --- Enrolment: the single-use credential an admin hands out ---
    # Held in the clear, unlike the session token, because the admin has to be able to
    # re-render its QR on screen for as long as it is outstanding. It is set to NULL
    # the moment it is spent, so at rest only unclaimed tokens exist unhashed.
    enrolment_token = db.Column(db.String(80), nullable=True, unique=True)
    enrolment_issued_at = db.Column(db.DateTime, nullable=True)

    # --- Session: the credential the activated phone holds ---
    activated_at = db.Column(db.DateTime, nullable=True)
    session_token_hash = db.Column(db.String(64), nullable=True)
    session_started_at = db.Column(db.DateTime, nullable=True)
    session_user_agent = db.Column(db.String(255), nullable=True)
    last_seen_at = db.Column(db.DateTime, nullable=True)

    # Soft kill. A device with receipts behind it cannot be deleted without orphaning
    # them, so revocation is how a device is taken out of service.
    revoked_at = db.Column(db.DateTime, nullable=True)

    @property
    def is_revoked(self):
        return self.revoked_at is not None

    @property
    def has_session(self):
        return self.session_token_hash is not None and not self.is_revoked

    @property
    def status(self):
        """One word for the admin list. Order matters: revoked outranks everything."""
        if self.is_revoked:
            return 'revoked'
        if self.session_token_hash:
            return 'active'
        if self.enrolment_token:
            return 'awaiting_activation'
        return 'signed_out'

class Submission(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    received_at = db.Column(db.DateTime, nullable=False, default=datetime.utcnow)
    status = db.Column(db.String(20), nullable=False, default='queued', index=True)
    input_type = db.Column(db.String(10), nullable=False)
    input_data = db.Column(db.String(1024), nullable=False)
    # What this submission is *about*, in one line. Written by whoever submitted it and
    # then, once a receipt lands, overwritten with the model's own one-sentence summary
    # - which is what the dashboard row and the CSV export both read.
    description = db.Column(db.Text, nullable=True)
    # What the person who submitted it said about it, kept apart from `description`
    # because that column stops being their words the moment the receipt is stored.
    #
    # It is the only thing on a submission that no automation can derive: 'diesel for
    # the generator, not the van', 'client lunch - Mwanza tender'. A photograph shows
    # what was bought and never why, and the why is what decides the category and half
    # the deductibility question. So it goes to the model with the photograph, the
    # pasted SMS and the verified facts alike (see main._sender_note), and it has to
    # still be here on the retry tomorrow and the re-analysis next month - which it was
    # not, while the LLM's summary was landing on top of it.
    user_note = db.Column(db.Text, nullable=True)
    location = db.Column(db.String(255), nullable=True)
    error_message = db.Column(db.Text, nullable=True)
    # The exception class behind the last failure, stored as its own field rather than
    # left to be picked back out of error_message. The dashboard has to say what went
    # wrong and what to do about it, and parsing that out of a sentence is how a
    # reworded message silently turns into "Processing failed".
    failure_reason = db.Column(db.String(50), nullable=True)
    # The verification code read out of the submitted TRA URL at intake, before the
    # portal is ever contacted. It is the receipt's identity, so it lets a duplicate be
    # spotted without spending a request, gives a submission that never verifies
    # something to show the admin, and ties such a submission to a vendor later if the
    # same receipt does eventually arrive. See utils/tra.parse_receipt_url.
    receipt_code = db.Column(db.String(50), nullable=True, index=True)
    # The TRA verification URL we will actually ask the portal for, when that is not the
    # one that came in. Written by the photo pipeline the moment it works out which
    # receipt an image is - by decoding its QR code on the server, or from the code and
    # time the vision model transcribed off the paper - so a retry goes straight back to
    # the portal instead of paying for the decode and the vision call again, and so a
    # photo that never verifies still says which receipt it was taken to be. Also written
    # by an admin correcting that address by hand, which is why it outranks input_data
    # for a URL submission too: a corrected address that only applied to the attempt that
    # set it would be re-broken by the next scheduled retry.
    recovered_url = db.Column(db.String(200), nullable=True)
    # When a human last re-read the code and time off the photograph and fixed them.
    # Kept because the two are otherwise indistinguishable from what a model transcribed,
    # and a page that says where an address came from is the difference between trusting
    # it and checking it again.
    corrected_at = db.Column(db.DateTime, nullable=True)
    # The photograph filed alongside a submission whose input was a URL.
    #
    # A scan used to be one thing or the other: a QR code the phone read, or a picture it
    # could not. That threw away the more useful half of the commonest case. When the
    # phone decodes the code it also has, in its hand, a photograph of the receipt that
    # produced it - and the moment the code is read that picture was being discarded, so
    # the receipt TRA confirms had no image behind it and nobody could ever look at the
    # paper again. It also meant the server-side decoder only ever ran on photographs the
    # phone had already failed on, which is why its hit rate reads as a fault rather than
    # as the selection effect it is.
    #
    # A bare filename, resolved against UPLOAD_FOLDER exactly as input_data is for a
    # photo submission - see main.submission_photo_path for why not an absolute path.
    # NULL on a URL submission with no picture behind it, and on a photo submission,
    # whose image is still input_data.
    photo_filename = db.Column(db.String(255), nullable=True)
    # What the vision model read off the photograph, as the JSON it returned.
    #
    # Kept because the pipeline used to read a photograph, rebuild a portal address out
    # of the code it transcribed, and then - when the portal would not confirm it -
    # throw the entire transcription away and book a retry. For up to two days the
    # submission then showed an admin nothing at all: no vendor, no total, no date, on a
    # receipt that had already been read and paid for. Worse where the rebuild was wrong
    # in the first place, because a document that was never an EFD receipt retries
    # against a portal that will never have heard of it, and the one usable reading of it
    # is the one that was discarded.
    #
    # So the transcription is written here the moment it exists, before anything is
    # attempted with it, and the admin can accept it as the receipt at any point. See
    # main._receipt_from_photo and main.accept_submission_extraction.
    llm_draft = db.Column(db.Text, nullable=True)
    # Set when an admin has said to stop rebuilding a portal address for this submission
    # out of transcribed text, and to keep what was read off the photograph instead.
    #
    # A submission-level answer to an instance-level setting (InstanceConfig.
    # rebuild_url_from_text): the setting decides the default for receipts nobody has
    # looked at, and this decides the one an admin is looking at now. Never cleared by a
    # retry - a person having judged this document not to be a TRA receipt outranks the
    # pipeline's guess on every later attempt, which is the whole point of recording it.
    rebuild_declined = db.Column(db.Boolean, nullable=False, default=False)
    # What the server-side QR decoder saw, as the JSON report utils.qr.scan returns.
    # Stored rather than logged because the three ways a photograph reaches the vision
    # model - the decoder is not installed, the upload was unopenable, the code is
    # genuinely unreadable - are indistinguishable from the outside and want three
    # different things done about them. NULL means the decoder was never run on this
    # submission: a URL submission, or a photo whose receipt was already identified.
    qr_scan = db.Column(db.Text, nullable=True)
    # The scanner's idempotency key, minted on the phone before the receipt has ever
    # been near a network. A queued scan is retried until it is acknowledged, and
    # without this every dropped response would leave a duplicate behind.
    client_uuid = db.Column(db.String(64), nullable=True, unique=True)
    # A hash of the evidence itself - the characters of a pasted record, the bytes of a
    # stored photograph. See utils/fingerprint.
    #
    # Deliberately not client_uuid, which answers a different question. That one is
    # minted on the phone and says 'this is the same *send* as the one you already
    # acknowledged'; this one is computed from the content and says 'this is the same
    # *thing*, however many times it has been submitted and from wherever'. The second
    # is what catches the SMS somebody pastes again on Friday having forgotten they
    # pasted it on Monday, and it catches it at intake, before a model is paid to read
    # a receipt already in the ledger.
    #
    # NULL on a URL submission, whose identity is the verification code in receipt_code,
    # and on a paste too slight to be an identity at all.
    content_hash = db.Column(db.String(80), nullable=True, index=True)
    # When the person actually scanned it, as opposed to received_at, which is when
    # the server heard about it. Offline queuing puts days between the two.
    captured_at = db.Column(db.DateTime, nullable=True)
    retry_count = db.Column(db.Integer, default=0)
    # When a queued job becomes eligible again. NULL means "now". Set instead of
    # sleeping inside the task runner when a fetch has to be retried later.
    next_attempt_at = db.Column(db.DateTime, nullable=True, index=True)
    # When a runner claimed this job; used to expire the lease on a dead runner.
    claimed_at = db.Column(db.DateTime, nullable=True)
    device_id = db.Column(db.Integer, db.ForeignKey('device.id'), nullable=False)
    device = db.relationship('Device', backref=db.backref('submissions', lazy=True))

    @property
    def photo_name(self):
        """
        The stored filename of this submission's photograph, or None if it has none.

        Two columns hold one idea, for a reason that is historical rather than designed.
        A photo submission keeps its image in input_data, because for a long time a photo
        was the *only* thing such a submission had. A submission that carries both a QR
        code and the picture it was read from keeps the picture in photo_filename,
        because input_data is the URL and there is only one of it. Every reader wants the
        same answer - "is there a photograph, and what is it called" - so it is worked
        out here, on the row itself, rather than in each of them.

        photo_filename first: a photo submission never has one, so the order only decides
        what happens to a row that somehow has both, and the explicit column is the newer
        and more specific statement.
        """
        if self.photo_filename:
            return self.photo_filename
        if self.input_type == 'photo' and self.input_data:
            return self.input_data
        return None

class EventLog(db.Model):
    """
    Every live event the app has announced recently, in order.

    The push channel used to be memory only: an event was handed to whichever SSE
    connections happened to be attached at that instant, and anything else - a phone
    with its screen off, a dashboard reconnecting after a dropped connection, a browser
    that had been backgrounded for ten minutes - simply never heard about it, and the
    screen stayed wrong until someone reloaded the page. That is what this table fixes.
    Events are written here first and read back by id, so a client that was away for a
    while asks "what happened after 412?" and is told, rather than being silently
    started from now with a gap behind it.

    Deliberately a log, not a queue: rows are not consumed, and every listener keeps
    its own cursor. It is trimmed rather than kept forever (see utils/sse_broker) -
    this is a catch-up buffer measured in minutes, and the submissions themselves are
    the record of truth.
    """
    id = db.Column(db.Integer, primary_key=True)
    created_at = db.Column(db.DateTime, nullable=False, default=datetime.utcnow, index=True)
    event_type = db.Column(db.String(50), nullable=False)
    # Denormalised out of the payload so a device's own stream is a WHERE clause rather
    # than every listener parsing every other device's events to discard them. NULL
    # means "not tied to a device", which a device stream must therefore never match.
    device_id = db.Column(db.Integer, nullable=True, index=True)
    payload = db.Column(db.Text, nullable=False)


class Vendor(db.Model):
    """
    A supplier, identified by TIN.

    Spending is grouped here rather than on the vendor name printed on the receipt:
    the same taxpayer appears as 'PLASCO LIMITED', 'Plasco Ltd' and 'PLASCO LIMITED.'
    across receipts, and grouping on that text splits one vendor into several. The TIN
    is issued by TRA and is exact.

    Receipts that carry no TIN (a photo the vendor block was cut off from) fall back
    to a normalised name so they still group with each other, which is why the unique
    key is `lookup_key` and not `tin` itself.
    """
    id = db.Column(db.Integer, primary_key=True)
    lookup_key = db.Column(db.String(220), unique=True, nullable=False, index=True)
    tin = db.Column(db.String(50), nullable=True, index=True)
    name = db.Column(db.String(200), nullable=True)
    vrn = db.Column(db.String(50), nullable=True)
    phone = db.Column(db.String(50), nullable=True)
    tax_office = db.Column(db.String(200), nullable=True)
    first_seen_at = db.Column(db.DateTime, nullable=False, default=datetime.utcnow)
    last_seen_at = db.Column(db.DateTime, nullable=False, default=datetime.utcnow)

    @property
    def is_vat_registered(self):
        return bool(self.vrn)

    @staticmethod
    def make_lookup_key(tin, name):
        """TIN when there is one, otherwise the normalised name. None if neither."""
        if tin and tin.strip():
            return f'tin:{tin.strip()}'
        if name and name.strip():
            return f'name:{" ".join(name.split()).lower()}'
        return None

    @classmethod
    def upsert(cls, tin=None, name=None, vrn=None, phone=None, tax_office=None):
        """
        Returns the vendor for these details, creating it on first sight.

        Details other than the key are refreshed from the newest receipt, so a change
        of trading name or tax office follows the vendor instead of forking it.
        """
        lookup_key = cls.make_lookup_key(tin, name)
        if lookup_key is None:
            return None

        vendor = cls.query.filter_by(lookup_key=lookup_key).first()
        if vendor is None:
            vendor = cls(lookup_key=lookup_key, tin=tin, name=name)
            db.session.add(vendor)

        vendor.tin = tin or vendor.tin
        vendor.name = name or vendor.name
        vendor.vrn = vrn or vendor.vrn
        vendor.phone = phone or vendor.phone
        vendor.tax_office = tax_office or vendor.tax_office
        vendor.last_seen_at = datetime.utcnow()
        return vendor

class Product(db.Model):
    """
    One thing the business buys, however it was written down that day.

    The catalogue exists because identity is what every question about buying needs and
    no receipt supplies. 'Mayai x 6' typed beside a mobile money line, 'EGGS TRAY' printed
    on an EFD, and 'mayay' typed by somebody in a hurry are one product bought three
    times - and only once they are one row can the app say that eggs cost 12% more this
    month, or that the shop across the road sells them cheaper.

    Modelled on Vendor, which solves the same problem for suppliers: a stable key, the
    display name refreshed from what was last seen, and the surface forms kept beside it.
    The difference is that a vendor has a TIN and a product has nothing - no barcode, no
    registry, nothing issued by anyone - so identity has to be built out of the words
    themselves. Three layers do it, cheapest first:

      1. The normalised name (utils.products.normalise), which collapses case,
         punctuation and a trailing unit.
      2. An alias, which is any other name this product has been seen under. Aliases are
         how the expensive answers become free: the model works out once that 'mayai'
         means eggs, and every later 'mayai' is a dictionary lookup.
      3. A near match on spelling, for the typo that neither of the above catches.

    An admin can always overrule all three - see the products page, where renaming one
    product onto another's name merges them, exactly as it does for categories.
    """
    id = db.Column(db.Integer, primary_key=True)
    lookup_key = db.Column(db.String(220), unique=True, nullable=False, index=True)
    name = db.Column(db.String(200), nullable=False)
    # The counting unit this is usually bought in - 'kg', 'tray', 'pcs' - remembered
    # from the first line that named one, so a later line without it still reads right.
    unit = db.Column(db.String(20), nullable=True)
    first_seen_at = db.Column(db.DateTime, nullable=False, default=datetime.utcnow)
    last_seen_at = db.Column(db.DateTime, nullable=False, default=datetime.utcnow)

    @property
    def alias_names(self):
        """Every other word this product has been called, for display."""
        return sorted({alias.alias for alias in self.aliases if alias.alias_key != self.lookup_key})

    @classmethod
    def lookup(cls, text):
        """
        The product this text names, or None. Never creates anything.

        The three layers in order. Fuzzy matching is last and is run over the keys
        already in the catalogue rather than over the seed list, because a catalogue
        that has seen a word is better evidence than a list written in advance.
        """
        key = product_text.normalise(text)
        if not key:
            return None

        found = cls.query.filter_by(lookup_key=key).first()
        if found is not None:
            return found

        alias = ProductAlias.query.filter_by(alias_key=key).first()
        if alias is not None:
            return alias.product

        near = product_text.best_match(key, [row[0] for row in db.session.query(cls.lookup_key)])
        if near:
            return cls.query.filter_by(lookup_key=near).first()

        near = product_text.best_match(
            key, [row[0] for row in db.session.query(ProductAlias.alias_key)])
        if near:
            alias = ProductAlias.query.filter_by(alias_key=near).first()
            return alias.product if alias else None
        return None

    @classmethod
    def resolve(cls, text, aliases=(), unit=None):
        """
        The product for this text, created on first sight.

        `aliases` is everything else this purchase was called - the model's other name
        for it, the words that were actually printed - and each one is written into the
        alias table pointing at whatever product this resolves to. That is the whole
        learning mechanism: it costs one row, and it turns the next occurrence of that
        word into a lookup.

        Ordinary text with nothing behind it goes through the seed list before it
        becomes a new product, so a fresh instance's first 'Mayai x 6' lands under Eggs
        rather than founding a catalogue entry the English word will never find again.
        """
        surface = product_text.display_name(text)
        if not surface:
            return None

        found = cls.lookup(surface)
        candidates = [surface, *[a for a in aliases if a]]
        if found is None:
            for candidate in candidates:
                found = cls.lookup(candidate)
                if found is not None:
                    break

        if found is None:
            # Nothing in the catalogue knows this word. The seed list may still know
            # what it means, in which case the product is created under the canonical
            # name and the typed word is remembered as an alias of it.
            seeded = next((product_text.seeded_name(candidate) for candidate in candidates
                           if product_text.seeded_name(candidate)), None)
            found = cls(
                lookup_key=product_text.normalise(seeded or surface),
                name=seeded or surface, unit=unit,
            )
            db.session.add(found)

        found.unit = found.unit or unit
        found.last_seen_at = datetime.utcnow()
        for candidate in candidates:
            found.remember(candidate)
        return found

    def remember(self, text, source='seen'):
        """Records one more word for this product, if it is new and not its own name."""
        key = product_text.normalise(text)
        if not key or key == self.lookup_key:
            return None
        existing = ProductAlias.query.filter_by(alias_key=key).first()
        if existing is not None:
            return existing
        alias = ProductAlias(
            product=self, alias_key=key, alias=product_text.display_name(text), source=source)
        db.session.add(alias)
        return alias

    def merge_into(self, other):
        """
        Folds this product into another, keeping every word both were known by.

        The losing name survives as an alias of the winner rather than being deleted,
        which is what stops the merge from being undone by the next receipt: the word
        that created this row is exactly the word that would create it again.
        """
        if other is None or other.id == self.id:
            return 0
        moved = 0
        for line in list(self.lines):
            line.product = other
            moved += 1
        for alias in list(self.aliases):
            alias.product = other
        other.remember(self.name, source='merge')
        other.first_seen_at = min(other.first_seen_at, self.first_seen_at)
        other.last_seen_at = max(other.last_seen_at, self.last_seen_at)
        db.session.delete(self)
        return moved

    @classmethod
    def catalogue(cls, limit=80):
        """
        The catalogue as the model is shown it: recent names, with what else they are
        called.

        Bounded because it is sent with every receipt read. Ordered by when each product
        was last bought, so an instance with a long tail still shows the model the names
        this week's receipts are actually going to hit.
        """
        rows = cls.query.order_by(cls.last_seen_at.desc()).limit(limit).all()
        return [
            {'name': row.name, 'also': row.alias_names[:4]} if row.alias_names else {'name': row.name}
            for row in rows
        ]


class ProductAlias(db.Model):
    """
    One word that means a product, other than the product's own name.

    Unique on the key across the whole table, so a word can only ever mean one thing -
    which is what makes resolution a lookup rather than a search, and what makes a merge
    permanent.

    `source` records who said so: 'seen' for a word that arrived on a receipt or in a
    note, 'llm' for the model's own synonym, 'merge' for the name of a product folded
    into this one, 'admin' for a word typed on the products page.
    """
    id = db.Column(db.Integer, primary_key=True)
    product_id = db.Column(db.Integer, db.ForeignKey('product.id'), nullable=False, index=True)
    product = db.relationship(
        'Product', backref=db.backref('aliases', lazy=True, cascade='all, delete-orphan'))
    alias_key = db.Column(db.String(220), unique=True, nullable=False, index=True)
    alias = db.Column(db.String(200), nullable=False)
    source = db.Column(db.String(10), nullable=True)
    first_seen_at = db.Column(db.DateTime, nullable=False, default=datetime.utcnow)


class Receipt(db.Model):
    id = db.Column(db.Integer, primary_key=True)

    # --- Vendor (denormalised for display; vendor_id is what you group by) ---
    vendor_id = db.Column(db.Integer, db.ForeignKey('vendor.id'), nullable=True, index=True)
    vendor = db.relationship('Vendor', backref=db.backref('receipts', lazy=True))
    vendor_name = db.Column(db.String(200), nullable=True)
    vendor_tin = db.Column(db.String(50), nullable=True, index=True)
    vendor_phone = db.Column(db.String(50), nullable=True)
    vrn = db.Column(db.String(50), nullable=True)
    tax_office = db.Column(db.String(200), nullable=True)

    # --- Receipt identity ---
    receipt_verification_code = db.Column(db.String(50), nullable=True, index=True, unique=True)
    receipt_number = db.Column(db.String(100), nullable=True)
    z_number = db.Column(db.String(50), nullable=True)
    efd_serial = db.Column(db.String(100), nullable=True)
    uin = db.Column(db.String(200), nullable=True)

    # --- Customer Fields ---
    customer_name = db.Column(db.String(200), nullable=True)
    customer_id_type = db.Column(db.String(100), nullable=True)
    customer_id = db.Column(db.String(100), nullable=True)
    customer_mobile = db.Column(db.String(50), nullable=True)

    # --- Money, in whole cents. See utils/money.py for why not Float. ---
    total_incl_tax_cents = db.Column(db.BigInteger, nullable=True)
    total_excl_tax_cents = db.Column(db.BigInteger, nullable=True)
    total_tax_cents = db.Column(db.BigInteger, nullable=True)
    discount_cents = db.Column(db.BigInteger, nullable=True)

    # When the money was spent. Everything that reports on spending keys off this,
    # never off processed_at, which only says when we happened to scan the receipt.
    receipt_date = db.Column(db.Date, nullable=True, index=True)
    receipt_time = db.Column(db.Time, nullable=True)

    # --- Validity ---
    # A cancelled receipt has been voided by the vendor and a test receipt was printed
    # by an EFD in test mode. Neither is an expense, both still get stored so the
    # submission has a visible outcome.
    is_cancelled = db.Column(db.Boolean, nullable=False, default=False, index=True)
    is_test = db.Column(db.Boolean, nullable=False, default=False, index=True)

    # --- Is this the same purchase we already have? ---
    # Two derived keys, both built by utils/fingerprint out of the columns above and
    # both kept current by refresh_fingerprints, which runs on every insert and update.
    # Derived rather than asked for: a receipt whose TIN or total was corrected by hand
    # is a different purchase from the one it was a minute ago, and a key written once
    # at intake would go on describing the reading somebody has just overruled.
    #
    # identity_key is an assertion - the same code, or the same reference and amount -
    # and a submission matching one already stored is filed as a duplicate without ever
    # reaching the model. near_key is an observation - the same supplier, day and total
    # - and is only ever reported to a human. See utils/fingerprint for why the line is
    # drawn there.
    identity_key = db.Column(db.String(300), nullable=True, index=True)
    near_key = db.Column(db.String(300), nullable=True, index=True)

    # --- System & Audit Fields ---
    # 'tra_html' when the facts were parsed from the verified page, 'llm_vision' when
    # a photo left no alternative to reading them out of the image.
    extraction_source = db.Column(db.String(20), nullable=True)
    # What kind of document this is, as opposed to where its numbers came from:
    # 'tra_efd_receipt', 'other_receipt' (a parking stub, a handwritten chit, a foreign
    # till slip) or 'not_a_receipt'. Only a photograph can be anything but the first,
    # and the distinction matters because a non-EFD document is not a failed EFD
    # receipt - it is a real expense with no input VAT behind it, and saying so is
    # different from saying verification did not work.
    document_type = db.Column(db.String(30), nullable=True)
    # The verified page exactly as TRA served it, so any field can be re-derived
    # later without asking the portal again.
    source_html = db.Column(db.Text, nullable=True)

    # --- What a human fixed by hand ---
    # A receipt read off a photograph is a model's best reading of a crumpled thermal
    # print, and some of those readings are wrong in ways that matter: a digit out of a
    # TIN splits one supplier into two, a wrong date moves the VAT claim window. An admin
    # with the photograph in front of them can fix that in seconds, and these two columns
    # are what stop the fix from being invisible afterwards - which fields stopped being
    # the model's reading, and when. Never set on a receipt parsed from TRA's own page:
    # those numbers are the portal's and are not edited here.
    corrected_at = db.Column(db.DateTime, nullable=True)
    # The labels of the fields a human has overwritten, as a JSON list. Cumulative: a
    # second correction adds to it rather than replacing it, because the question being
    # answered is "which of these numbers is still the model's?".
    corrected_fields = db.Column(db.Text, nullable=True)
    category = db.Column(db.String(50), nullable=True, index=True)
    # When an admin last set the category by hand.
    #
    # Kept apart from corrected_fields, which is about the printed facts on the receipt
    # panel, because the category is not one of those: it is a judgment, it is the
    # model's on every receipt including the ones TRA verified, and it is the one thing
    # here a person may overrule on any receipt at all. Its presence is also an
    # instruction - re-analysis leaves a hand-set category alone rather than replacing
    # somebody's decision with a fresh guess. See main.set_receipt_category.
    category_corrected_at = db.Column(db.DateTime, nullable=True)
    # 'ok', 'unavailable' (the model could not be reached) or 'skipped'.
    llm_status = db.Column(db.String(20), nullable=True)
    processed_at = db.Column(db.DateTime, nullable=False, default=datetime.utcnow, index=True)
    raw_llm_response = db.Column(db.Text, nullable=True)
    device_id = db.Column(db.Integer, db.ForeignKey('device.id'), nullable=False)
    device = db.relationship('Device', backref=db.backref('receipts', lazy=True))
    submission_id = db.Column(db.Integer, db.ForeignKey('submission.id'), unique=True, nullable=False)
    submission = db.relationship('Submission', backref=db.backref('receipt', uselist=False, lazy=True))

    @property
    def total_amount(self):
        """The gross amount as a Decimal. Reads and reports use this; sums use cents."""
        return from_cents(self.total_incl_tax_cents)

    @property
    def total_excl_tax(self):
        return from_cents(self.total_excl_tax_cents)

    @property
    def vat_amount(self):
        """Tax charged on the receipt, summed across every printed rate."""
        return from_cents(self.total_tax_cents)

    @property
    def discount(self):
        return from_cents(self.discount_cents)

    @property
    def is_expense(self):
        return not (self.is_cancelled or self.is_test)

    @property
    def receipt_datetime(self):
        if self.receipt_date is None:
            return None
        return datetime.combine(self.receipt_date, self.receipt_time or datetime.min.time())

    @property
    def printed_items(self):
        """
        The lines that were actually on the document.

        Every check that recomputes something the document asserts - the tax
        cross-foot, the line-item tax codes - has to run on these and not on
        `items`, which also holds what the sender said they bought. See
        ReceiptItem.source.
        """
        return [item for item in self.items if item.source != 'note']

    @property
    def note_items(self):
        """What the sender's note said was bought, when the document did not say."""
        return [item for item in self.items if item.source == 'note']

    @property
    def vendor_key(self):
        """
        Which supplier this receipt is one purchase from, as a string.

        The same key the vendor row itself is filed under, computed from the same two
        printed fields - so it is the TIN where there is one and the normalised trading
        name otherwise, and 'PLASCO LIMITED' and 'Plasco Ltd' are one supplier here for
        exactly the reason they are one row there.

        Deliberately derived rather than read off vendor_id, which says the same thing.
        A key is computed while a receipt is being flushed and also before it exists at
        all - checking a transcription against the ledger happens before any of it is
        stored - and the printed fields are the half of that pair which is always
        already there.
        """
        return Vendor.make_lookup_key(self.vendor_tin, self.vendor_name)

    def refresh_fingerprints(self):
        """Recomputes the two duplicate-matching keys from what the receipt now says."""
        vendor_key = self.vendor_key
        self.identity_key = fingerprint.identity_key(
            verification_code=self.receipt_verification_code,
            reference=self.receipt_number,
            vendor_key=vendor_key,
            total_cents=self.total_incl_tax_cents,
            on_date=self.receipt_date,
        )
        self.near_key = fingerprint.near_key(
            vendor_key=vendor_key,
            on_date=self.receipt_date,
            total_cents=self.total_incl_tax_cents,
        )
        return self

    @classmethod
    def with_identity(cls, key):
        """The stored receipt this key names, or None. The blocking duplicate check."""
        if not key:
            return None
        return cls.query.filter_by(identity_key=key).first()

    def possible_duplicates(self, limit=5):
        """
        Other receipts that look like the same purchase recorded twice.

        Matching on identity catches the same document submitted twice - the same
        verification code, or the same payment reference and amount. It cannot catch
        the same purchase submitted once as a photograph and once as a TRA link, or a
        handwritten chit photographed on two different days: neither copy carries
        anything the other can be matched against exactly, so both are stored and the
        expense is counted twice.

        What that actually looks like is the same supplier, the same day, the same
        total - which is a query, not a guess. Two genuinely separate purchases can
        match, so this reports candidates and never merges anything.
        """
        if not self.near_key:
            return []

        query = Receipt.query.filter(Receipt.near_key == self.near_key)
        if self.id is not None:
            query = query.filter(Receipt.id != self.id)
        return query.order_by(Receipt.id.asc()).limit(limit).all()


# Derived columns, kept derived. Every path that stores a receipt goes through a flush -
# the pipeline, an admin accepting a transcription, a correction typed on the receipt
# page - and putting this on the mapper is what makes 'the keys match what the row says'
# true by construction rather than by each of those paths remembering to say so.
@event.listens_for(Receipt, 'before_insert')
@event.listens_for(Receipt, 'before_update')
def _refresh_receipt_fingerprints(_mapper, _connection, receipt):
    receipt.refresh_fingerprints()


class ReceiptItem(db.Model):
    """
    One line of the purchased-items table.

    This is the level at which category, withholding-tax and capital-allowance
    questions are actually decided - a single 'total' cannot tell you that one line
    of a receipt was a laptop and the rest was stationery.
    """
    id = db.Column(db.Integer, primary_key=True)
    receipt_id = db.Column(db.Integer, db.ForeignKey('receipt.id'), nullable=False, index=True)
    receipt = db.relationship(
        'Receipt',
        backref=db.backref('items', lazy=True, cascade='all, delete-orphan', order_by='ReceiptItem.line_number'),
    )
    line_number = db.Column(db.Integer, nullable=False, default=1)
    description = db.Column(db.String(500), nullable=True)
    quantity = db.Column(db.Numeric(18, 4), nullable=True)
    amount_cents = db.Column(db.BigInteger, nullable=True)
    # TRA's per-line tax class: A, B, C, SR or EX.
    tax_code = db.Column(db.String(5), nullable=True, index=True)

    # Where this line came from, and it is the only thing separating a fact from a
    # reading of one:
    #
    #   'printed' - it was on the document. Every line the parser read off TRA's
    #               verified page, and every line a model transcribed off paper.
    #   'note'    - nothing printed it. It is what the sender said they bought, read
    #               out of the note beside the camera because the document itself said
    #               only 'LIPA JACLINE NGILISHO MOLLEL'.
    #
    # Everything that recomputes tax from the document reads printed lines only (see
    # Receipt.printed_items), because a note line has no tax code and never did - the
    # alternative was a checked receipt reporting that its tax could not be checked the
    # moment somebody typed what they bought into the box.
    source = db.Column(db.String(10), nullable=False, default='printed', index=True)

    # The catalogue entry this line is an instance of. Nullable throughout: a line
    # nothing could identify is still a line, and every reader treats a missing product
    # as 'not known' rather than as an error.
    product_id = db.Column(db.Integer, db.ForeignKey('product.id'), nullable=True, index=True)
    product = db.relationship('Product', backref=db.backref('lines', lazy=True))

    @property
    def amount(self):
        return from_cents(self.amount_cents)

    @property
    def from_note(self):
        return self.source == 'note'

    @property
    def label(self):
        """What to call this line: the catalogue's name for it, or its own text."""
        if self.product is not None:
            return self.product.name
        return self.description

class ReceiptTaxLine(db.Model):
    """
    One 'TAX RATE X (n%)' row from the totals table.

    Kept per rate rather than collapsed into a single VAT figure, because a receipt
    can carry standard-rated, special-rated and exempt amounts at once and a VAT
    return needs them apart.
    """
    id = db.Column(db.Integer, primary_key=True)
    receipt_id = db.Column(db.Integer, db.ForeignKey('receipt.id'), nullable=False, index=True)
    receipt = db.relationship(
        'Receipt',
        backref=db.backref('tax_lines', lazy=True, cascade='all, delete-orphan'),
    )
    code = db.Column(db.String(5), nullable=False)
    rate = db.Column(db.Numeric(6, 2), nullable=True)
    amount_cents = db.Column(db.BigInteger, nullable=True)

    @property
    def amount(self):
        return from_cents(self.amount_cents)
