"""
The front door, and what it is dressed in.

An instance is one business's private ledger, and its address travels - to a
bookkeeper, into a WhatsApp group, onto a business card - long before its owner
necessarily wants a sales page hanging off it. So the page at '/' is a setting:

  story   Sells Karani. The default, and the reason a self-hosted instance is also
          the software's best advertisement.
  simple  A plain sign carrying the host business's own name and nothing else.
  off     No public page at all; '/' goes straight to the sign-in form.

Whichever is chosen, the whole page is coloured from a single hex the admin picks.
Asking for a palette is asking for a design decision most people do not want to make,
so the darker shade, the pale tint and - the one that actually breaks pages when it is
wrong - the colour text sits in on top of the accent are all derived from that one
value here, once, instead of being guessed at in a template. Pick yellow and the
buttons put black text on it; pick navy and they put white.
"""
from dataclasses import dataclass

STORY = 'story'
SIMPLE = 'simple'
OFF = 'off'
MODES = (STORY, SIMPLE, OFF)
DEFAULT_MODE = STORY

# The green of the mark on the receipt in the logo, deepened until small white text on
# top of it is legible rather than merely visible.
DEFAULT_ACCENT = '#0f8a46'
PRODUCT_NAME = 'Karani'


@dataclass(frozen=True)
class Brand:
    """Everything a public page needs to know about who is hosting it."""
    mode: str
    name: str
    tagline: str
    logo_url: str
    accent: str
    accent_dark: str
    accent_soft: str
    on_accent: str
    cta_label: str
    cta_url: str

    @property
    def is_off(self):
        return self.mode == OFF

    @property
    def initial(self):
        """The letter in the square when there is no logo image."""
        return (self.name or PRODUCT_NAME).strip()[:1].upper() or 'K'


def of(config):
    """The brand for an instance. Safe on a config that has never been near Settings."""
    name = _first(
        getattr(config, 'brand_name', None),
        getattr(config, 'business_name', None),
        PRODUCT_NAME,
    )
    accent, accent_dark, accent_soft, on_accent = palette(getattr(config, 'brand_accent', None))

    return Brand(
        mode=mode_of(config),
        name=name,
        tagline=_first(getattr(config, 'brand_tagline', None), ''),
        logo_url=_first(getattr(config, 'brand_logo_url', None), ''),
        accent=accent,
        accent_dark=accent_dark,
        accent_soft=accent_soft,
        on_accent=on_accent,
        cta_label=_first(getattr(config, 'landing_cta_label', None), 'Talk to us'),
        cta_url=_first(getattr(config, 'landing_cta_url', None), _default_cta(config)),
    )


def mode_of(config):
    """The stored mode, or the default for anything unrecognised or unset."""
    stored = (getattr(config, 'landing_mode', None) or '').strip().lower()
    return stored if stored in MODES else DEFAULT_MODE


def _default_cta(config):
    """
    An email to whoever runs the instance, so the button works before it is configured.

    A landing page whose only call to action is dead is worse than no landing page, and
    the admin address is the one contact detail every instance is guaranteed to hold.
    """
    email = (getattr(config, 'admin_email', None) or '').strip()
    if not email:
        return ''
    subject = 'I%20would%20like%20to%20use%20Karani'
    return f'mailto:{email}?subject={subject}'


def _first(*values):
    for value in values:
        if value and str(value).strip():
            return str(value).strip()
    return ''


# --- Colour -----------------------------------------------------------------

def palette(accent):
    """
    (accent, darker, tint, text-on-accent) from one hex, with the default for junk.

    The admin types this into a text box, so 'blue', '#12345' and an empty string all
    have to land somewhere sensible rather than emitting CSS that silently makes half
    the page invisible.
    """
    rgb = _parse_hex(accent) or _parse_hex(DEFAULT_ACCENT)
    return (
        _to_hex(rgb),
        _to_hex(_mix(rgb, (0, 0, 0), 0.22)),
        _to_hex(_mix(rgb, (255, 255, 255), 0.90)),
        '#101010' if _luminance(rgb) > 0.42 else '#ffffff',
    )


def _parse_hex(value):
    text = (value or '').strip().lstrip('#')
    if len(text) == 3:
        text = ''.join(character * 2 for character in text)
    if len(text) != 6:
        return None
    try:
        return tuple(int(text[index:index + 2], 16) for index in (0, 2, 4))
    except ValueError:
        return None


def _to_hex(rgb):
    return '#%02x%02x%02x' % tuple(max(0, min(255, round(channel))) for channel in rgb)


def _mix(rgb, towards, amount):
    return tuple(channel + (target - channel) * amount for channel, target in zip(rgb, towards))


def _luminance(rgb):
    """Relative luminance, sRGB. Decides whether text on the accent is black or white."""
    def channel(value):
        value /= 255
        return value / 12.92 if value <= 0.04045 else ((value + 0.055) / 1.055) ** 2.4

    red, green, blue = (channel(value) for value in rgb)
    return 0.2126 * red + 0.7152 * green + 0.0722 * blue
