# tests/test_categories.py
"""
Categorising by hand, and keeping the set of categories from breeding.

A category is the one thing on a receipt that no source settles. The facts come from
TRA's verified page or, failing that, from a model reading the paper; the category comes
from a keyword classifier and a language model, either of which can be wrong and both of
which can return nothing at all. So an admin can set it - on any receipt, verified or
not, because the portal supplied those numbers and never the reason for them.

The cost of allowing that is the thing these tests are mostly about. A free-text
category field ends the quarter with 'fuel', 'Fuel', 'fuel ' and 'Fuel Costs' as four
lines in one report, each holding a quarter of the diesel. Two rules stop it: a typed
name is resolved against the categories that already exist before anything is stored,
and the list of categories is read off the receipts rather than kept in a table that can
accumulate names nothing uses.
"""
import json
import shutil
import subprocess
from datetime import date

import pytest

from models.user import db, InstanceConfig, Receipt, Submission
from tests.test_dashboard_javascript import ScriptCollector
from utils.classify import EXPENSE_CATEGORIES, resolve_category


@pytest.fixture
def config(app):
    config = InstanceConfig(admin_email='admin@example.com', totp_secret='SECRET')
    db.session.add(config)
    db.session.commit()
    return config


@pytest.fixture
def admin(app, config):
    client = app.test_client()
    with client.session_transaction() as session:
        session['admin_logged_in'] = True
    return client


@pytest.fixture
def receipt(app, device):
    """A stored receipt, made with whatever category the test is about."""
    codes = iter(range(1, 999))

    def _make(category=None, **overrides):
        submission = Submission(
            device_id=device.id, input_type='photo', input_data='x.jpg', status='completed',
        )
        db.session.add(submission)
        db.session.flush()

        fields = {
            'vendor_name': 'PLASCO LIMITED', 'vendor_tin': '100147181',
            'receipt_verification_code': f'CODE{next(codes)}',
            'receipt_date': date(2026, 7, 1), 'total_incl_tax_cents': 118_00,
            'extraction_source': 'llm_vision', 'category': category,
            'device_id': device.id, 'submission_id': submission.id,
        }
        fields.update(overrides)
        stored = Receipt(**fields)
        db.session.add(stored)
        db.session.commit()
        return stored
    return _make


# --- Naming one ---------------------------------------------------------------

@pytest.mark.parametrize('typed', [
    'Office Supplies', 'office supplies', 'OFFICE-SUPPLIES', '  office_supplies  ',
    'office supply',
])
def test_one_category_typed_five_ways_is_one_category(typed):
    """
    Case, punctuation and an English plural are not distinctions anybody means.

    This is the whole defence against a duplicate: by the time a name reaches the
    database it has been matched against what is already there, so the ordinary result
    of naming a category is landing on the one already in use.
    """
    assert resolve_category(typed) == ('office_supplies', True)


def test_a_genuinely_new_name_is_allowed_and_says_it_is_new():
    """
    The fixed set does not cover everything, and refusing what it misses would send
    people back to filing real expenses under 'other'.
    """
    assert resolve_category('Site allowances') == ('site_allowances', False)


def test_a_name_already_in_use_beats_a_new_slug_that_matches_it():
    """A second spelling of a category this instance invented collapses onto the first."""
    assert resolve_category('site allowance', {'site_allowances': 4}) == ('site_allowances', True)


@pytest.mark.parametrize('typed', ['', '   ', '!!!', '---', 'uncategorised', 'Uncategorized'])
def test_a_name_that_is_not_a_category_is_refused(typed):
    """
    Nothing usable, or the word this app already uses for having no category at all -
    which would filter to the exact opposite of itself.
    """
    assert resolve_category(typed) == (None, False)


# --- Setting one on a receipt -------------------------------------------------

def test_a_receipt_nothing_categorised_can_be_filed_by_hand(app, admin, receipt):
    """The case this exists for: the model returned no category and the receipt is real."""
    stored = receipt(category=None)

    response = admin.post(f'/receipts/{stored.id}/category', data={'category': 'Fuel'})

    assert response.status_code == 200
    assert response.get_json()['category'] == 'fuel'
    assert stored.category == 'fuel'
    assert stored.category_corrected_at is not None


def test_filing_under_a_differently_spelled_name_lands_on_the_existing_category(app, admin, receipt):
    """What the picker offers and what the server stores have to be the same category."""
    receipt(category='site_allowances')
    stored = receipt(category=None)

    response = admin.post(f'/receipts/{stored.id}/category', data={'category': 'Site Allowance'})

    assert response.get_json()['existed'] is True
    assert stored.category == 'site_allowances'
    # Still one category, not two spellings of it.
    assert set(Receipt.query.with_entities(Receipt.category).distinct().all()) == {
        ('site_allowances',),
    }


def test_a_verified_receipt_can_still_be_recategorised(app, admin, receipt):
    """
    The numbers on a TRA receipt are the portal's and are not edited here. The category
    is not one of them - it is a judgment a model made, on a purchase whose purpose is
    not printed anywhere.
    """
    stored = receipt(category='other', extraction_source='tra_html')

    assert admin.post(f'/receipts/{stored.id}/category', data={'category': 'rent'}).status_code == 200
    assert stored.category == 'rent'


def test_clearing_a_category_hands_the_receipt_back_to_the_model(app, admin, receipt):
    """
    Clearing is not the same as choosing nothing: it also takes off the mark that tells
    re-analysis to leave the category alone.
    """
    stored = receipt(category='fuel')
    admin.post(f'/receipts/{stored.id}/category', data={'category': 'travel'})
    assert stored.category_corrected_at is not None

    admin.post(f'/receipts/{stored.id}/category', data={'category': ''})

    assert stored.category is None
    assert stored.category_corrected_at is None


def test_a_junk_name_is_refused_with_a_sentence(app, admin, receipt):
    stored = receipt(category=None)

    response = admin.post(f'/receipts/{stored.id}/category', data={'category': '!!!'})

    assert response.status_code == 400
    assert 'cannot be used as a category name' in response.get_json()['error']
    assert stored.category is None


def test_the_word_for_having_no_category_cannot_become_one(app, admin, receipt):
    """It is a filter value; a real category by that name would select its own opposite."""
    stored = receipt(category=None)

    response = admin.post(f'/receipts/{stored.id}/category', data={'category': 'Uncategorised'})

    assert response.status_code == 400
    assert 'Clear the category instead' in response.get_json()['error']


def test_re_analysis_leaves_a_hand_set_category_alone(app, admin, config, receipt, monkeypatch):
    """
    Re-analysis is for a prompt change or an outage. Neither is a reason to replace a
    person's decision with a fresh guess - so the narrative is rewritten and the category
    a human chose is not.
    """
    import main

    stored = receipt(category=None, extraction_source='tra_html', source_html='<html></html>')
    admin.post(f'/receipts/{stored.id}/category', data={'category': 'staff costs'})

    monkeypatch.setattr(config.__class__, 'is_configured', lambda self: True)
    monkeypatch.setattr(main, 'parse_receipt_html', lambda html: _parsed())
    monkeypatch.setattr(main, 'analyse_receipt', lambda *a, **k: {
        'category': 'meals_entertainment', 'llm_tax_analysis': 'Fresh wording.',
    })

    payload = admin.post(f'/receipts/{stored.id}/reanalyse').get_json()

    assert payload['category_kept'] is True
    assert stored.category == 'staff_costs'
    assert 'Fresh wording.' in stored.raw_llm_response


def _parsed():
    """The smallest thing reanalyse_receipt needs back from the parser."""
    class _Parsed:
        def as_llm_facts(self):
            return {'vendor_name': 'PLASCO LIMITED'}
    return _Parsed()


# --- Amending the ones already there ------------------------------------------

def test_renaming_moves_every_receipt_under_it(app, admin, receipt):
    receipt(category='site_allowance')
    receipt(category='site_allowance')

    payload = admin.post('/categories/rename',
                         data={'from': 'site_allowance', 'to': 'Site Money'}).get_json()

    assert payload['moved'] == 2
    assert payload['merged'] is False
    assert Receipt.query.filter_by(category='site_money').count() == 2


def test_renaming_onto_a_name_in_use_merges_the_two(app, admin, receipt):
    """
    There is no separate merge, because renaming is one. An admin who has just noticed
    two spellings of one category should not have to work out which operation that is.
    """
    receipt(category='site_allowance')
    receipt(category='site_allowances')
    receipt(category='site_allowances')

    payload = admin.post('/categories/rename',
                         data={'from': 'site_allowance', 'to': 'site_allowances'}).get_json()

    assert payload['merged'] is True
    assert payload['moved'] == 1
    assert Receipt.query.filter_by(category='site_allowances').count() == 3
    assert Receipt.query.filter_by(category='site_allowance').count() == 0


def test_a_rename_marks_the_receipts_as_decided_by_a_human(app, admin, receipt):
    """It is the same decision as setting one by hand, made about a group at once."""
    stored = receipt(category='site_allowance')

    admin.post('/categories/rename', data={'from': 'site_allowance', 'to': 'rent'})

    assert stored.category_corrected_at is not None


def test_renaming_a_category_onto_itself_is_refused(app, admin, receipt):
    """However it is spelled. Nothing would move, and the message would be a lie."""
    receipt(category='fuel')

    response = admin.post('/categories/rename', data={'from': 'fuel', 'to': 'FUEL'})

    assert response.status_code == 409
    assert 'already has that name' in response.get_json()['error']


def test_renaming_a_category_nothing_carries_is_refused(app, admin, receipt):
    response = admin.post('/categories/rename', data={'from': 'fuel', 'to': 'travel'})

    assert response.status_code == 409
    assert 'No receipts carry that category' in response.get_json()['error']


def test_a_rename_with_no_target_is_refused_rather_than_emptying_the_category(app, admin, receipt):
    """
    Blank would have to mean 'uncategorise all of these', which is a different and much
    more destructive request than the one this route takes.
    """
    receipt(category='fuel')

    response = admin.post('/categories/rename', data={'from': 'fuel', 'to': '  '})

    assert response.status_code == 400
    assert Receipt.query.filter_by(category='fuel').count() == 1


# --- What there is to choose from ---------------------------------------------

def test_the_offered_categories_are_the_fixed_set_plus_what_is_in_use(app, receipt):
    """
    Read off the receipts rather than kept in a table beside them, so a category that
    nothing carries any more simply stops being offered.
    """
    import main

    receipt(category='site_allowances')
    keys = [option['key'] for option in main.category_options()]

    assert 'site_allowances' in keys
    assert set(EXPENSE_CATEGORIES) <= set(keys)
    # Most-used first: the category you are about to want is one you use a lot of.
    assert keys[0] == 'site_allowances'


def test_a_category_stops_being_offered_once_nothing_carries_it(app, admin, receipt):
    import main

    stored = receipt(category='site_allowances')
    admin.post(f'/receipts/{stored.id}/category', data={'category': 'fuel'})

    assert 'site_allowances' not in [option['key'] for option in main.category_options()]


# --- Finding the ones with none -----------------------------------------------

def test_the_dashboard_can_select_the_receipts_with_no_category(app, admin, receipt, device):
    """
    Otherwise they are invisible. What an uncategorised receipt shows on the dashboard
    is one fewer chip than its neighbours, which is not something anybody spots.
    """
    receipt(category='fuel')
    blank = receipt(category=None)
    # A submission still queued also has a null category, and is not what is being
    # asked for: it has not been looked at, rather than looked at and come back blank.
    db.session.add(Submission(device_id=device.id, input_type='url', input_data='x'))
    db.session.commit()

    payload = admin.get('/api/submissions?tab=all&category=uncategorised').get_json()

    assert payload['total'] == 1
    assert payload['submissions'][0]['receipt']['receipt_id'] == blank.id


def test_a_cancelled_receipt_is_not_a_receipt_waiting_to_be_categorised(app, admin, receipt):
    """
    It is meant to have no category. Nothing was bought, the model is not asked about it
    (see main._judge_receipt) and filing it under anything would put a voided sale in a
    spending total - so counting it as outstanding work leaves a backlog that cannot be
    cleared however much filing gets done.
    """
    receipt(category=None, is_cancelled=True)
    receipt(category=None, is_test=True)
    real = receipt(category=None)

    payload = admin.get('/api/submissions?tab=all&category=uncategorised').get_json()
    assert [row['receipt']['receipt_id'] for row in payload['submissions']] == [real.id]

    body = admin.get('/categories').get_data(as_text=True)
    assert '1 receipt' in body


def test_the_uncategorised_option_is_offered_only_when_there_is_one(app, admin, receipt):
    """A picker offering a selection that matches nothing is a dead end to click on."""
    receipt(category='fuel')
    facets = admin.get('/api/submissions?tab=all').get_json()['facets']
    assert 'uncategorised' not in [option['key'] for option in facets['categories']]

    receipt(category=None)
    facets = admin.get('/api/submissions?tab=all').get_json()['facets']
    assert ('uncategorised', 1) in [
        (option['key'], option['count']) for option in facets['categories']
    ]


def test_the_uncategorised_selection_stays_offered_when_it_is_the_one_applied(app, admin, receipt):
    """
    Counted under everything except the pickers themselves, like every other facet: a
    selection that vanished the moment it was applied could never be taken off again.
    """
    receipt(category='fuel')

    facets = admin.get('/api/submissions?tab=all&category=uncategorised').get_json()['facets']

    assert 'uncategorised' in [option['key'] for option in facets['categories']]


def test_the_categories_page_shows_the_backlog_and_the_book(app, admin, receipt):
    receipt(category='fuel')
    receipt(category=None)

    body = admin.get('/categories').get_data(as_text=True)

    assert '1 receipt' in body and 'carries' in body
    assert 'fuel' in body
    # And a way to go and fix them.
    assert 'category=uncategorised' in body


# --- The picker, executed ------------------------------------------------------

@pytest.mark.skipif(not shutil.which('node'), reason='node is not installed')
def test_the_picker_offers_an_existing_category_rather_than_a_near_duplicate(
        app, admin, receipt, tmp_path):
    """
    The list is the defence, and the server is the backstop.

    An admin who has to be careful about spelling will eventually not be, so typing a
    name that differs from one already in use only by case or an English plural must
    show that category as a thing to click - not an offer to create a second one. The
    matching is mirrored in the page for that reason, and mirrored logic is worth
    running rather than reading: this evaluates the real component out of the real HTML.
    """
    receipt(category='site_allowances')
    stored = receipt(category='fuel')
    body = admin.get(f'/receipts/{stored.id}').get_data(as_text=True)

    collector = ScriptCollector()
    collector.feed(body)

    driver = """
    const component = receiptPage();
    const offered = query => {
        component.categoryQuery = query;
        return { keys: component.matchingCategories.map(o => o.key), isNew: component.isNewName };
    };
    console.log(JSON.stringify({
        current: component.category,
        plural: offered('Site Allowance'),
        cased: offered('FUEL'),
        genuinelyNew: offered('Borehole levy'),
        blank: offered('   '),
    }));
    """
    bundle = tmp_path / 'receipt.js'
    bundle.write_text('\n'.join(collector.scripts) + driver)

    result = subprocess.run(['node', str(bundle)], capture_output=True, text=True, timeout=30)
    assert result.returncode == 0, result.stderr
    state = json.loads(result.stdout)

    assert state['current'] == 'fuel'
    # A plural or a capital is not a new category, whichever way round it is typed.
    assert state['plural']['isNew'] is False
    assert state['cased']['isNew'] is False
    assert 'fuel' in state['cased']['keys']
    # Something we really do not hold is offered as new, because refusing it would send
    # this receipt back to 'other'.
    assert state['genuinelyNew']['isNew'] is True
    # And an empty box is not an invitation to create a category called nothing.
    assert state['blank']['isNew'] is False


def test_the_classifiers_own_reading_is_offered_as_one_press(app, admin, receipt):
    """
    On a receipt the model left blank, the keyword classifier has usually read the line
    items perfectly well. Printing that as a remark and making somebody type it back in
    is the difference between a backlog that gets cleared and one that does not.
    """
    from models.user import ReceiptItem

    stored = receipt(category=None)
    stored.items.append(ReceiptItem(line_number=1, description='DIESEL AGO', amount_cents=118_00))
    db.session.commit()

    body = admin.get(f'/receipts/{stored.id}').get_data(as_text=True)

    assert 'File as fuel' in body
    assert "saveCategory('fuel')" in body


def test_a_receipt_already_filed_is_only_told_what_the_items_read_as(app, admin, receipt):
    """
    The same disagreement, on a receipt somebody has already decided about, is a remark
    and not an offer - a one-press button there would undo their decision by accident.
    """
    from models.user import ReceiptItem

    stored = receipt(category='travel')
    stored.items.append(ReceiptItem(line_number=1, description='DIESEL AGO', amount_cents=118_00))
    db.session.commit()

    body = admin.get(f'/receipts/{stored.id}').get_data(as_text=True)

    assert 'items read as fuel' in body
    assert 'File as fuel' not in body


# --- Where the page lives ------------------------------------------------------

def test_categories_is_reached_from_settings_rather_than_the_top_bar(app, admin, receipt):
    """
    The top bar is for the four daily views of the book. This page is somewhere you go
    when something needs tidying - a receipt nothing categorised, two names that turned
    out to mean one thing - which is the same kind of errand as Devices, and it sits in
    the same place.
    """
    receipt(category='fuel')
    receipt(category=None)

    settings = admin.get('/admin/configure?tab=categories').get_data(as_text=True)

    assert "switchTab('categories')" in settings
    assert 'Manage categories' in settings
    # And it says whether the page is worth opening without opening it.
    assert '1 category in use' in settings
    assert '1 receipt' in settings

    # Not a sixth name in the top bar; Settings lights up for it instead.
    dashboard = admin.get('/').get_data(as_text=True)
    assert 'href="/categories"' not in dashboard
    assert admin.get('/categories').status_code == 200


def test_the_settings_form_is_not_shown_on_a_tab_that_only_links_out(app, admin):
    """
    Both link-out tabs are listed in one place. A second one added as another '!==' is
    how a Save button ends up on a page with nothing on it to save.
    """
    body = admin.get('/admin/configure').get_data(as_text=True)

    assert "linksOut: ['devices', 'categories']" in body
    assert 'x-show="!linksOut.includes(activeTab)"' in body
