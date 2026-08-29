# tests/test_products.py
"""
What was bought, as opposed to what was paid for.

A mobile money receipt reads `LIPA JACLINE NGILISHO MOLLEL - TZS 3,500` and says
nothing about the purchase. The only record of what the money bought is the note the
payer typed beside the camera: `Mayai x 6`. These cover what happens to it.

Three things have to hold, and each of them is a way this feature could be worse than
useless rather than merely incomplete:

  * The note is read for goods, and those goods become lines of their own - so a
    payment record stops being a dead end for every question worth asking of a purchase.
  * They are never mixed into the document's own lines. A note is what somebody typed
    on a phone; a printed line is what TRA verified. Confusing the two would put
    somebody's typing into a tax return as a verified figure.
  * The same purchase written differently is one product. 'Mayai' this week, 'eggs'
    next week and 'mayay' from somebody in a hurry are one row with one price history,
    or the catalogue is a list of synonyms with no history at all.
"""

import pytest

from models.user import db, Product, ProductAlias, Receipt, ReceiptItem, Submission
from utils import products


@pytest.fixture
def admin(app, config):
    client = app.test_client()
    with client.session_transaction() as session:
        session['admin_logged_in'] = True
    return client


@pytest.fixture
def paid(app, device):
    """
    A receipt as a mobile money payment leaves one: a total, and a line naming the payee.

    The shape this whole feature exists for. Nothing on it says what was bought.
    """
    def _build(note='Mayai x 6', line='LIPA JACLINE NGILISHO MOLLEL', total_cents=350000):
        submission = Submission(
            device_id=device.id, input_type='text', input_data='0829Q29J3 Imepokelewa.',
            user_note=note,
        )
        db.session.add(submission)
        db.session.commit()

        receipt = Receipt(
            vendor_name='JACLINE NGILISHO MOLLEL', total_incl_tax_cents=total_cents,
            extraction_source='llm_text', document_type='other_receipt',
            device_id=device.id, submission_id=submission.id,
        )
        if line:
            receipt.items.append(ReceiptItem(line_number=1, description=line,
                                             amount_cents=total_cents))
        db.session.add(receipt)
        db.session.commit()
        return receipt
    return _build


# --- The reader, with no database and no model ---------------------------------------

@pytest.mark.parametrize('text, expected', [
    ('Mayai x 6', ('Mayai', 6.0)),
    ('6 x mayai', ('mayai', 6.0)),
    ('6 mayai', ('mayai', 6.0)),
    ('mayai 6pcs', ('mayai', 6.0)),
    ('2kg sukari', ('sukari', 2.0)),
    ('mayai', ('mayai', None)),
])
def test_a_count_is_read_however_it_was_written(text, expected):
    """People write the number before the noun as often as after it."""
    name, quantity, _ = products.parse_entry(text)
    assert (name, quantity) == expected


def test_a_pack_size_is_not_a_count():
    """
    'Coca cola 500ml' is one bottle of a named size, not five hundred of anything.

    The distinction matters because a quantity is divided into an amount to get a unit
    price, and reading the size as the count files the purchase at a five-hundredth of
    what it cost.
    """
    assert products.parse_entry('coca cola 500ml') == ('coca cola 500ml', None, None)


def test_a_payment_line_is_recognised_as_describing_nothing():
    """The test for when the note is the only description of the purchase there is."""
    assert products.is_opaque('LIPA JACLINE NGILISHO MOLLEL')
    assert products.is_opaque('SUMMARIZED SALE')
    assert not products.is_opaque('MAYAI TREI')


def test_a_note_listing_two_things_is_two_entries():
    assert products.parse_note('Mayai x 6, mkate 2') == [('Mayai', 6.0, None), ('mkate', 2.0, None)]


def test_a_short_word_is_never_matched_by_spelling():
    """
    'rice' and 'ride' are 75% the same string and are not the same purchase.

    Fuzzy matching earns its place on ordinary misspellings and nowhere else, so it is
    refused entirely below the length at which a near miss stops being a typo.
    """
    assert products.best_match('rice', ['ride']) is None
    assert products.best_match('mayay', ['mayai']) == 'mayai'
    # A different word that happens to score well is stopped by the first letter.
    assert products.best_match('beans', ['jeans']) is None


# --- The catalogue -------------------------------------------------------------------

def test_one_purchase_written_three_ways_is_one_product(app):
    """The whole point of the catalogue, in one assertion."""
    first = Product.resolve('Mayai')
    db.session.commit()

    assert Product.resolve('mayai') is first      # the same word
    assert Product.resolve('mayay') is first      # a slip of the thumb
    assert Product.resolve('Eggs') is first       # the other language
    assert Product.query.count() == 1


def test_the_seed_list_files_the_first_receipt_under_the_english_name(app):
    """
    A fresh instance has no catalogue to match against, so 'Mayai' would otherwise
    found a row that the English word never finds again.
    """
    product = Product.resolve('Mayai x 6'.split(' x ')[0])
    db.session.commit()

    assert product.name == 'Eggs'
    assert 'Mayai' in product.alias_names


def test_a_word_the_model_supplies_is_remembered(app):
    """
    The expensive answer is bought once.

    The model is what can tell that 'MAYAI TREI' is eggs. Storing that as an alias is
    what stops the next receipt printing the same line from having to ask again.
    """
    product = Product.resolve('Eggs', aliases=['MAYAI FRESH'])
    db.session.commit()

    assert Product.lookup('mayai fresh') is product
    assert ProductAlias.query.filter_by(alias_key='mayai fresh').count() == 1


def test_a_word_that_names_nothing_never_becomes_an_alias(app):
    """
    Alias keys are unique across the catalogue, so the first product to claim 'goods'
    would own it - and every later receipt saying 'goods' would be filed as that.
    """
    import main

    assert main._usable_aliases(['goods', 'payment', 'x', 'Mayai']) == ['Mayai']


def test_renaming_onto_an_existing_product_merges_the_two(app, admin):
    """
    The one operation the products page offers, and it is also the merge.

    The losing name survives as an alias, which is what makes the merge stick: the word
    that created the duplicate row is exactly the word that would create it again.
    """
    eggs = Product.resolve('Eggs')
    other = Product(lookup_key='mayai fresh', name='Mayai fresh')
    db.session.add(other)
    db.session.commit()

    response = admin.post('/products/rename', data={'id': other.id, 'to': 'Eggs'})

    assert response.status_code == 200
    assert response.get_json()['merged'] is True
    assert Product.query.count() == 1
    assert Product.lookup('mayai fresh') is eggs


def test_renaming_to_a_new_name_keeps_the_old_one_as_an_alias(app, admin):
    """A rename is not a way to lose the word receipts are still arriving with."""
    product = Product.resolve('Mayai')
    db.session.commit()

    response = admin.post('/products/rename', data={'id': product.id, 'to': 'Table eggs'})

    assert response.status_code == 200
    assert response.get_json()['merged'] is False
    assert Product.query.one().name == 'Table eggs'
    assert Product.lookup('eggs') is not None


# --- The pipeline --------------------------------------------------------------------

def test_the_note_itemises_a_receipt_that_itemises_nothing(app, paid):
    """
    'Mayai x 6' beside a payment line becomes six eggs, priced at what was paid.

    The amount is the case this feature is for: one product and a document that priced
    nothing else means the total is that product's price, which is what turns a payment
    record into a unit price something can be compared against later.
    """
    import main

    receipt = paid()
    main.apply_products(receipt, 'Mayai x 6')
    db.session.commit()

    bought = receipt.note_items
    assert len(bought) == 1
    assert bought[0].product.name == 'Eggs'
    assert float(bought[0].quantity) == 6
    assert bought[0].amount_cents == 350000


def test_a_note_line_is_never_one_of_the_documents_own(app, paid):
    """
    The line the model read and the line somebody typed are kept apart, permanently.

    Everything that recomputes what the paperwork asserts reads `printed_items`. A note
    line appearing there would be somebody's typing presented as a verified figure.
    """
    import main

    receipt = paid()
    main.apply_products(receipt, 'Mayai x 6')
    db.session.commit()

    assert [item.description for item in receipt.printed_items] == ['LIPA JACLINE NGILISHO MOLLEL']
    assert [item.source for item in receipt.note_items] == ['note']


def test_two_products_under_one_total_are_counted_but_not_priced(app, paid):
    """
    How the 3,500 divides between eggs and bread is not on the receipt and not in the
    note, so it is not recorded. A count with no price is honest; a split nobody
    computed is a figure somebody might file a return on.
    """
    import main

    receipt = paid(note='Mayai x 6, mkate 2')
    main.apply_products(receipt, 'Mayai x 6, mkate 2')
    db.session.commit()

    assert len(receipt.note_items) == 2
    assert all(item.amount_cents is None for item in receipt.note_items)


def test_a_note_that_names_no_goods_adds_nothing(app, paid):
    """
    'For the generator' is a reason, not a purchase.

    Without the model, a fragment has to carry a count or name something the catalogue
    already knows before it becomes a product - otherwise every note explaining why the
    money was spent would found a product nobody ever buys again.
    """
    import main

    receipt = paid(note='For the generator')
    main.apply_products(receipt, 'For the generator')
    db.session.commit()

    assert receipt.note_items == []


def test_a_document_that_itemises_itself_is_not_re_itemised_from_the_note(app, paid):
    """
    A note beside a receipt that lists its own lines is commentary, not itemisation.
    """
    import main

    receipt = paid(line='MAYAI TREI', note='bought at the market')
    main.apply_products(receipt, 'bought at the market')
    db.session.commit()

    assert receipt.note_items == []
    # The printed line is still filed against the catalogue on its own text.
    assert receipt.printed_items[0].product is not None


def test_the_model_is_told_what_the_catalogue_already_calls_things(app):
    """
    Without the list the model picks a reasonable name each time - 'Eggs', then 'Egg',
    then 'Fresh eggs' - and the catalogue grows a synonym per receipt.
    """
    from utils.llm_processor import _catalogue_block

    Product.resolve('Eggs', aliases=['mayai'])
    db.session.commit()

    block = _catalogue_block(Product.catalogue()).lower()
    assert 'eggs' in block and 'mayai' in block


def test_re_analysing_a_receipt_does_not_double_its_note_lines(app, paid):
    """Rebuilt each time rather than appended to, or a re-read doubles what was bought."""
    import main

    receipt = paid()
    main.apply_products(receipt, 'Mayai x 6')
    main.apply_products(receipt, 'Mayai x 6')
    db.session.commit()

    assert len(receipt.note_items) == 1


def test_a_verified_receipts_tax_can_still_be_recomputed_after_a_note_is_read(app, device):
    """
    The regression this feature could have caused, asserted directly.

    The tax cross-foot refuses to run unless every line carries a tax code. A note line
    has none and never will, so counted among the printed lines it would report that a
    fully verified receipt could not be checked - the moment somebody typed what was in
    the bag.
    """
    import main
    from models.user import ReceiptTaxLine

    submission = Submission(device_id=device.id, input_type='url', input_data='x',
                            user_note='Mayai x 6')
    db.session.add(submission)
    db.session.commit()

    receipt = Receipt(
        vendor_name='A SHOP', vrn='40-123456-X', total_incl_tax_cents=11800,
        total_excl_tax_cents=10000, total_tax_cents=1800, extraction_source='tra_html',
        document_type='tra_efd_receipt', device_id=device.id, submission_id=submission.id,
    )
    receipt.items.append(ReceiptItem(line_number=1, description='SUMMARIZED SALE',
                                     amount_cents=11800, tax_code='A'))
    receipt.tax_lines.append(ReceiptTaxLine(code='A', rate=18, amount_cents=1800))
    db.session.add(receipt)
    db.session.commit()

    main.apply_products(receipt, 'Mayai x 6')
    db.session.commit()

    assert receipt.note_items, 'the note should still have been read'
    check = next(c for c in main.assess_receipt(receipt).checks if c.id == 'tax_arithmetic')
    assert check.status != 'na', check.detail
