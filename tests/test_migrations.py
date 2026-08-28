# tests/test_migrations.py
"""
The boot-time migration, run against a database built to the previous schema.

This is the code path every existing deployment takes on its next restart, so it is
exercised against real legacy DDL rather than trusted to review.
"""
import os
from datetime import date, datetime
from decimal import Decimal

import pytest
from sqlalchemy import text as sa_text

from models.user import db, Receipt, Vendor

# The schema as it shipped before line items, vendors and integer money: amounts in
# FLOAT columns, no scheduling columns on submission.
LEGACY_SCHEMA = [
    """
    CREATE TABLE device (
        id INTEGER NOT NULL PRIMARY KEY,
        name VARCHAR(100) NOT NULL,
        api_key VARCHAR(100) NOT NULL UNIQUE
    )
    """,
    """
    CREATE TABLE submission (
        id INTEGER NOT NULL PRIMARY KEY,
        received_at DATETIME NOT NULL,
        status VARCHAR(20) NOT NULL,
        input_type VARCHAR(10) NOT NULL,
        input_data VARCHAR(1024) NOT NULL,
        description TEXT,
        location VARCHAR(255),
        error_message TEXT,
        retry_count INTEGER,
        device_id INTEGER NOT NULL
    )
    """,
    """
    CREATE TABLE instance_config (
        id INTEGER NOT NULL PRIMARY KEY,
        admin_email VARCHAR(120) NOT NULL UNIQUE,
        totp_secret VARCHAR(100) NOT NULL UNIQUE,
        llm_provider VARCHAR(50),
        llm_api_key VARCHAR(200),
        post_callback_url VARCHAR(500),
        s3_bucket_name VARCHAR(200),
        s3_access_key_id VARCHAR(200),
        s3_secret_access_key VARCHAR(200),
        s3_region VARCHAR(50),
        google_sheet_id VARCHAR(200),
        google_service_account_json TEXT
    )
    """,
    """
    CREATE TABLE receipt (
        id INTEGER NOT NULL PRIMARY KEY,
        vendor_name VARCHAR(200),
        vendor_tin VARCHAR(50),
        vendor_phone VARCHAR(50),
        vrn VARCHAR(50),
        receipt_verification_code VARCHAR(50) UNIQUE,
        receipt_number VARCHAR(100),
        uin VARCHAR(200),
        customer_name VARCHAR(200),
        customer_id_type VARCHAR(100),
        customer_id VARCHAR(100),
        total_amount FLOAT,
        vat_amount FLOAT,
        receipt_date DATE,
        processed_at DATETIME NOT NULL,
        raw_llm_response TEXT,
        device_id INTEGER NOT NULL,
        submission_id INTEGER NOT NULL UNIQUE
    )
    """,
]


@pytest.fixture
def legacy_database(app):
    """A database at the previous schema, holding two receipts from one vendor."""
    db.drop_all()
    for statement in LEGACY_SCHEMA:
        db.session.execute(sa_text(statement))

    db.session.execute(sa_text(
        "INSERT INTO device (id, name, api_key) VALUES (1, 'Old device', 'old-key')"
    ))
    db.session.execute(sa_text(
        "INSERT INTO instance_config (id, admin_email, totp_secret, llm_provider)"
        " VALUES (1, 'admin@example.com', 'SECRET', 'groq')"
    ))
    for index, (amount, vat, name) in enumerate(
        [(1234.56, 188.32, 'PLASCO LIMITED'), (10.10, 0.0, 'Plasco Ltd')], start=1
    ):
        db.session.execute(sa_text(
            "INSERT INTO submission (id, received_at, status, input_type, input_data, device_id)"
            " VALUES (:id, :now, 'completed', 'url', 'https://verify.tra.go.tz/X_000000', 1)"
        ), {'id': index, 'now': datetime.utcnow()})
        db.session.execute(sa_text(
            "INSERT INTO receipt (id, vendor_name, vendor_tin, receipt_verification_code,"
            " total_amount, vat_amount, receipt_date, processed_at, device_id, submission_id)"
            " VALUES (:id, :name, '100147181', :code, :amount, :vat, :day, :now, 1, :id)"
        ), {
            'id': index, 'name': name, 'code': f'CODE{index}', 'amount': amount,
            'vat': vat, 'day': date(2022, 3, 8), 'now': datetime.utcnow(),
        })
    db.session.commit()


def test_migration_adds_the_new_schema(legacy_database):
    import main

    db.create_all()          # new tables only; SQLAlchemy leaves existing ones alone
    main.apply_pending_migrations()

    receipt_columns = main._table_columns('receipt')
    assert {'total_incl_tax_cents', 'is_cancelled', 'source_html', 'vendor_id',
            'efd_serial', 'z_number', 'tax_office', 'receipt_time',
            'corrected_at', 'corrected_fields', 'category_corrected_at'} <= receipt_columns
    assert {'next_attempt_at', 'claimed_at', 'client_uuid', 'captured_at',
            'corrected_at', 'user_note'} <= main._table_columns('submission')
    assert {'business_name', 'business_tin', 'business_vrn'} <= main._table_columns('instance_config')
    assert {'enrolment_token', 'session_token_hash', 'revoked_at', 'last_seen_at',
            'activated_at', 'created_at'} <= main._table_columns('device')


def test_migration_gives_existing_devices_an_activation_link(legacy_database):
    """
    A device that predates enrolment has to be activatable without being recreated.

    Recreating it would be the obvious workaround and the wrong one: device_id is on
    every submission and receipt, so a new row means the history stops following the
    device that produced it.
    """
    import main
    from models.user import Device

    db.create_all()
    main.apply_pending_migrations()

    device = Device.query.one()
    assert device.name == 'Old device'
    assert device.enrolment_token is not None
    assert device.enrolment_token.startswith(f'{device.id}.')
    assert device.status == 'awaiting_activation'
    # Its original API key is untouched: whatever bot holds it keeps working.
    assert device.api_key == 'old-key'
    assert len(device.submissions) == 2


def test_migration_does_not_disturb_an_activated_device(legacy_database):
    """The backfill must not hand out a second activation link to a live phone."""
    import main
    from models.user import Device
    from utils.device_auth import consume_enrolment_token

    db.create_all()
    main.apply_pending_migrations()

    device = Device.query.one()
    session_token, _ = consume_enrolment_token(device.enrolment_token)
    assert device.enrolment_token is None

    main.apply_pending_migrations()

    assert device.enrolment_token is None
    assert device.session_token_hash is not None


def test_migration_adds_the_unique_indexes(legacy_database):
    """
    SQLite cannot add a UNIQUE column with ALTER TABLE, so uniqueness on client_uuid
    arrives as its own index. Existing rows have NULL there, which SQLite treats as
    distinct - the index would otherwise fail to build on any populated database.
    """
    import main

    db.create_all()
    main.apply_pending_migrations()

    indexes = {row[1] for row in db.session.execute(sa_text('PRAGMA index_list(submission)'))}
    assert 'uq_submission_client_uuid' in indexes

    # Two legacy submissions, both with a NULL client_uuid, survived the index.
    from models.user import Submission
    assert Submission.query.count() == 2


def test_migration_keeps_an_existing_instance_configured(legacy_database):
    """
    Adding the business identity must not disturb the settings already there.

    The instance config is a single row holding the API keys and the TOTP secret; an
    upgrade that dropped it would lock the admin out of their own dashboard.
    """
    import main
    from models.user import InstanceConfig

    db.create_all()
    main.apply_pending_migrations()

    config = InstanceConfig.query.one()
    assert (config.admin_email, config.totp_secret, config.llm_provider) == (
        'admin@example.com', 'SECRET', 'groq',
    )
    # Nothing has been claimed about the business yet, which the checks read as "unset".
    assert config.business_tin is None


def test_migration_converts_float_amounts_to_cents(legacy_database):
    import main

    db.create_all()
    main.apply_pending_migrations()

    first = db.session.get(Receipt, 1)
    assert first.total_incl_tax_cents == 123456
    assert first.total_tax_cents == 18832
    assert first.total_amount == Decimal('1234.56')
    # 10.10 is not representable in binary floating point; rounding must not lose it.
    assert db.session.get(Receipt, 2).total_incl_tax_cents == 1010


def test_migration_reduces_absolute_photo_paths_to_filenames(legacy_database):
    """
    A photo submitted before the persistence volume moved must still resolve.

    Its stored path pointed into whatever directory the data lived in at the time,
    so leaving it alone would orphan the image the moment that directory changed -
    which is exactly what moving the volume off the code directory does.
    """
    import main
    from models.user import Submission

    db.session.execute(sa_text(
        "INSERT INTO submission (id, received_at, status, input_type, input_data, device_id)"
        " VALUES (3, :now, 'completed', 'photo', '/app/data/uploads/1699.0_receipt.jpg', 1)"
    ), {'now': datetime.utcnow()})
    db.session.commit()

    db.create_all()
    main.apply_pending_migrations()

    photo = db.session.get(Submission, 3)
    assert photo.input_data == '1699.0_receipt.jpg'
    # Only photos name a file on disk; a URL submission keeps its whole URL.
    assert db.session.get(Submission, 1).input_data == 'https://verify.tra.go.tz/X_000000'

    # And the filename resolves against wherever uploads live now.
    resolved = main.submission_photo_path(photo)
    assert resolved == os.path.join(main.app.config['UPLOAD_FOLDER'], '1699.0_receipt.jpg')


def test_migration_backfills_vendors_from_existing_receipts(legacy_database):
    import main

    db.create_all()
    main.apply_pending_migrations()

    # Two spellings of one TIN collapse into a single vendor.
    assert Vendor.query.count() == 1
    vendor = Vendor.query.one()
    assert vendor.lookup_key == 'tin:100147181'
    assert len(vendor.receipts) == 2


def test_migration_is_idempotent(legacy_database):
    import main

    db.create_all()
    main.apply_pending_migrations()
    main.apply_pending_migrations()

    assert Vendor.query.count() == 1
    assert db.session.get(Receipt, 1).total_incl_tax_cents == 123456


def test_migrated_receipts_appear_in_the_dashboard_stats(legacy_database):
    import main

    db.create_all()
    main.apply_pending_migrations()

    # Both are dated 2022-03-08, so they are last year's spending, not today's.
    stats = main.calculate_dashboard_stats()
    assert stats['today']['count'] == 0

    total = db.session.query(db.func.sum(Receipt.total_incl_tax_cents)).scalar()
    assert total == 124466


def test_migration_clears_vrns_stored_as_the_not_registered_placeholder(legacy_database):
    """
    Receipts stored before the parser recognised TRA's placeholder hold the literal
    text 'NOT REGISTERED' in the VRN column, which reads as a VRN everywhere the
    field is tested for presence - the vendor list badged those suppliers 'VAT
    registered' and compliance scored their tax as recoverable.

    The vendor row has to be cleaned too: Vendor.upsert only ever overwrites a VRN
    with a non-empty one, so a placeholder left there would never be displaced.
    """
    import main

    db.session.execute(sa_text("UPDATE receipt SET vrn = 'NOT REGISTERED' WHERE id = 1"))
    db.session.execute(sa_text("UPDATE receipt SET vrn = '10007206H' WHERE id = 2"))
    db.session.commit()

    db.create_all()
    main.apply_pending_migrations()

    assert db.session.get(Receipt, 1).vrn is None
    # A real VRN on the same vendor is untouched, and is what the vendor keeps.
    assert db.session.get(Receipt, 2).vrn == '10007206H'
    assert Vendor.query.one().is_vat_registered is True


def test_migration_leaves_a_placeholder_only_vendor_unregistered(legacy_database):
    """The badge case from the bug report: no receipt of this vendor carries a VRN."""
    import main

    db.session.execute(sa_text("UPDATE receipt SET vrn = 'NOT REGISTERED'"))
    db.session.commit()

    db.create_all()
    main.apply_pending_migrations()

    vendor = Vendor.query.one()
    assert vendor.vrn is None
    assert vendor.is_vat_registered is False


def test_migration_rescues_the_sender_notes_that_are_still_recoverable(legacy_database):
    """
    `description` held two different things by turns, and the migration can only save
    one of them.

    It starts as what the person submitting typed and is overwritten with the model's
    own summary the moment a receipt lands. So a submission that has not completed still
    holds the sender's words and they can be moved to the column that now keeps them; a
    completed one holds the model's sentence, and moving that would be worse than losing
    it - it would come back to the model later labelled as a human's note.
    """
    import main

    for index, (status, description) in enumerate([
        ('queued', 'Diesel for the site generator'),
        ('failed', 'Client lunch, Mwanza tender'),
        ('completed', 'Plastic sheeting for the workshop.'),
        ('queued', None),
    ], start=10):
        db.session.execute(sa_text(
            "INSERT INTO submission (id, received_at, status, input_type, input_data,"
            " description, device_id) VALUES (:id, :now, :status, 'photo', 'x.jpg',"
            " :description, 1)"
        ), {'id': index, 'now': datetime.utcnow(), 'status': status,
            'description': description})
    db.session.commit()

    db.create_all()
    main.apply_pending_migrations()

    from models.user import Submission
    notes = {row.id: row.user_note for row in Submission.query.filter(Submission.id >= 10)}
    assert notes == {
        10: 'Diesel for the site generator',
        11: 'Client lunch, Mwanza tender',
        12: None,
        13: None,
    }
    # And the column it came out of is left exactly as it was: the dashboard row and the
    # CSV export both read it, and this migration is not the place to change what they show.
    assert db.session.get(Submission, 10).description == 'Diesel for the site generator'
