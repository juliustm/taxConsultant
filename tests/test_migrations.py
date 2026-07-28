# tests/test_migrations.py
"""
The boot-time migration, run against a database built to the previous schema.

This is the code path every existing deployment takes on its next restart, so it is
exercised against real legacy DDL rather than trusted to review.
"""
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
            'efd_serial', 'z_number', 'tax_office', 'receipt_time'} <= receipt_columns
    assert {'next_attempt_at', 'claimed_at'} <= main._table_columns('submission')
    assert {'business_name', 'business_tin', 'business_vrn'} <= main._table_columns('instance_config')


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
