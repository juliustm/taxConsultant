# tests/test_landing.py
"""
The front door.

'/' is the one address two different audiences arrive at: the admin, who gets the
dashboard, and everyone else, who gets whatever the admin decided the public should
see. The failures worth guarding against are all of the same shape - a public visitor
being shown something private, or an admin losing their way in.
"""
import pytest

from models.user import db
from utils import branding


@pytest.fixture
def visitor(app, config):
    """A browser nobody has signed in on."""
    return app.test_client()


@pytest.fixture
def admin(app, config):
    """A browser holding an admin cookie."""
    client = app.test_client()
    with client.session_transaction() as session:
        session['admin_logged_in'] = True
    return client


def set_landing(config, **fields):
    for name, value in fields.items():
        setattr(config, name, value)
    db.session.commit()


# --- Which page is served ---------------------------------------------------

def test_a_visitor_gets_the_story_page_by_default(visitor):
    """Out of the box, an instance sells the software that runs it."""
    body = visitor.get('/').get_data(as_text=True)

    assert 'Stop losing' in body
    assert 'Receipts Dashboard' not in body


def test_the_dashboard_is_still_what_an_admin_gets(admin):
    assert 'Receipts Dashboard' in admin.get('/').get_data(as_text=True)


def test_the_plain_page_carries_the_business_and_not_the_pitch(visitor, config):
    """
    Mode 'simple' is for an instance whose address belongs to one business.

    None of the sales copy may survive the switch - that is the entire point of it.
    """
    set_landing(config, landing_mode='simple', brand_name='Atana Ventures',
                brand_tagline='Quietly building things.')

    body = visitor.get('/').get_data(as_text=True)

    assert 'Atana Ventures' in body
    assert 'Quietly building things.' in body
    assert 'Stop losing' not in body
    assert 'Somewhere tonight' not in body


def test_switching_the_page_off_sends_visitors_to_sign_in(visitor, config):
    set_landing(config, landing_mode='off')

    response = visitor.get('/')

    assert response.status_code == 302
    assert '/admin/login' in response.headers['Location']


def test_an_unrecognised_mode_falls_back_rather_than_erroring(visitor, config):
    """
    A hand-edited database must not be able to 500 the front door.

    render_template('landing/<mode>.html') would happily be handed anything.
    """
    set_landing(config, landing_mode='../base')

    assert visitor.get('/').status_code == 200


def test_an_instance_that_has_never_been_set_up_goes_to_setup(app):
    """No config means no admin and no business, so there is nothing to show yet."""
    response = app.test_client().get('/')

    assert response.status_code == 302
    assert '/admin/setup' in response.headers['Location']


# --- The way in -------------------------------------------------------------

@pytest.mark.parametrize('mode', ['story', 'simple', 'off'])
def test_signing_in_is_reachable_whatever_the_page_says(visitor, config, mode):
    """
    Whatever the public sees, the admin still has to get in.

    In every mode either the page itself links to the sign-in form, or it is the
    sign-in form.
    """
    set_landing(config, landing_mode=mode)

    response = visitor.get('/', follow_redirects=True)

    assert response.status_code == 200
    assert '/admin/login' in response.get_data(as_text=True) or 'Welcome back' in response.get_data(as_text=True)


def test_every_page_on_the_way_in_renders(app, config):
    """
    The four unauthenticated pages, rendered.

    They share a template base with the public page, so a mistake in that base locks
    every admin out of every instance. Cheap to check, catastrophic to miss.
    """
    client = app.test_client()

    assert 'Welcome back' in client.get('/admin/login').get_data(as_text=True)

    with client.session_transaction() as browser:
        browser['login_email'] = config.admin_email
    assert 'authenticator' in client.get('/admin/login/verify').get_data(as_text=True)


def test_first_run_renders_before_there_is_an_instance(app):
    """Setup happens when there is no config at all, so `brand` has nothing to read."""
    client = app.test_client()

    assert 'Claim this instance' in client.get('/admin/setup').get_data(as_text=True)

    with client.session_transaction() as browser:
        browser['setup_email'] = 'admin@example.com'
        browser['setup_totp_secret'] = 'SECRET'
        browser['setup_qr_code'] = 'data:image/png;base64,x'
    assert 'Scan this once' in client.get('/admin/setup/verify').get_data(as_text=True)


def test_the_sign_in_page_wears_the_business_colours(visitor, config):
    set_landing(config, brand_name='Atana Ventures', brand_accent='#123456')

    body = visitor.get('/admin/login').get_data(as_text=True)

    assert '#123456' in body
    assert 'Atana Ventures' in body


# --- Branding ---------------------------------------------------------------

def test_the_button_points_where_the_admin_pointed_it(visitor, config):
    """The whole reason the story page exists: it has to lead somewhere useful."""
    set_landing(config, landing_cta_label='Get Karani',
                landing_cta_url='https://forms.example/qualify')

    body = visitor.get('/').get_data(as_text=True)

    assert 'https://forms.example/qualify' in body
    assert 'Get Karani' in body


def test_an_unset_button_still_reaches_someone(visitor, config):
    """A dead call to action is worse than none, so it falls back to the admin."""
    assert f'mailto:{config.admin_email}' in visitor.get('/').get_data(as_text=True)


def test_the_admin_can_look_at_either_page_without_switching_to_it(admin, config):
    """Choosing between the two modes is a choice you have to be able to see."""
    set_landing(config, landing_mode='off', brand_name='Atana Ventures')

    story = admin.get('/admin/front-page/preview?mode=story')
    plain = admin.get('/admin/front-page/preview?mode=simple')

    assert 'Stop losing' in story.get_data(as_text=True)
    assert 'Atana Ventures' in plain.get_data(as_text=True)


def test_the_preview_is_not_a_way_around_the_login(visitor):
    response = visitor.get('/admin/front-page/preview')

    assert response.status_code == 302
    assert '/admin/login' in response.headers['Location']


# --- The colour maths -------------------------------------------------------

def test_text_on_the_accent_follows_the_accent(config):
    """
    The one branding decision that breaks a page when it is wrong.

    An admin picking yellow must not get white text on it.
    """
    _, _, _, on_dark = branding.palette('#1b3a6b')
    _, _, _, on_light = branding.palette('#f7d046')

    assert on_dark == '#ffffff'
    assert on_light == '#101010'


@pytest.mark.parametrize('junk', ['', None, 'blue', '#12345', 'rgb(1,2,3)', '#zzzzzz'])
def test_a_colour_that_is_not_a_colour_lands_on_the_default(junk):
    """It is a free text box, so it will be typed into."""
    assert branding.palette(junk)[0] == branding.DEFAULT_ACCENT


def test_a_short_hex_is_understood(config):
    assert branding.palette('#0a0')[0] == '#00aa00'


def test_the_shades_are_derived_from_the_one_colour(config):
    """One input, a whole palette - the admin is asked for a colour, not a system."""
    accent, dark, soft, _ = branding.palette('#0f8a46')

    assert accent == '#0f8a46'
    assert dark != accent and soft != accent
    # Darker than the accent, and the tint lighter than both.
    assert branding._luminance(branding._parse_hex(dark)) < branding._luminance(branding._parse_hex(accent))
    assert branding._luminance(branding._parse_hex(soft)) > branding._luminance(branding._parse_hex(accent))


def test_the_brand_falls_back_through_business_name_to_the_product(config):
    assert branding.of(config).name == 'Karani'

    config.business_name = 'Atana Ventures'
    assert branding.of(config).name == 'Atana Ventures'

    config.brand_name = 'Atana'
    assert branding.of(config).name == 'Atana'


def test_branding_survives_an_instance_that_does_not_exist_yet():
    """Setup renders before there is a config row to read."""
    brand = branding.of(None)

    assert brand.name == 'Karani'
    assert brand.mode == branding.DEFAULT_MODE
    assert brand.cta_url == ''
