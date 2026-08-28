# models/user.py
from flask_sqlalchemy import SQLAlchemy
import uuid
from datetime import datetime

from utils.money import from_cents

db = SQLAlchemy()

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
    description = db.Column(db.Text, nullable=True)
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

    @property
    def amount(self):
        return from_cents(self.amount_cents)

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
