# utils/llm_processor.py
"""
The judgment layer.

The model is not asked what the receipt says - `utils/tra_parser` reads that from the
verified page exactly. It is asked only what the purchase *means*: which expense
category it belongs to, and how it looks under Tanzanian tax law. Facts go in as
structured JSON and come back out unchanged.

The one exception is a photographed receipt, which has no machine-readable source, so
`extract_receipt_details` still reads the fields out of the image. Those receipts are
recorded with extraction_source='llm_vision' to keep them distinguishable from the
ones whose numbers are exact.

Even there the model is asked to work towards being overruled: it must transcribe the
receipt verification code and the time of sale, which together rebuild the receipt's
address on TRA's portal (see `reconstructed_receipt_url`). When they come back readable
the caller fetches the verified page and the transcription is thrown away in favour of
the real numbers - so a photo whose QR code was creased, faded or half under a thumb
still ends up as an exact receipt rather than a plausible one.
"""
import openai
import ast
import base64
import json
import os
import re

# The fixed category set lives in utils.classify, which decides the same question
# from the item text alone. Imported rather than restated so the model can never
# return a category the deterministic classifier has no bucket for.
from utils.classify import EXPENSE_CATEGORIES
# The portal address is built in one place for every caller - see reconstructed_receipt_url.
from utils import images
from utils.tra import build_receipt_url

JUDGMENT_SYSTEM_PROMPT = """
You are an expert in Tanzanian tax compliance (Income Tax Act / VAT Act) reviewing a
business expense.

The receipt's facts have already been read from TRA's verified receipt page and are
given to you as JSON. They are exact. Never restate, recompute or correct them - do
not repeat amounts, dates, TINs or receipt numbers back as if you had established
them. Your job is the judgment the numbers do not contain.

Decide, from the line items and the vendor:
- Which expense category it belongs to.
- Whether it is deductible under the business purpose test (Section 11 ITA), and what
  would disqualify it.
- Input VAT: recoverable only where the supplier is VAT registered (a VRN is present)
  and tax was actually charged. Flag a vendor charging tax with no VRN.
- Withholding tax: 5% for resident individuals, 10% for corporations, on services
  above 100,000 TZS per month. Say when the items look like services that should have
  had WHT deducted.
- Whether an item looks like a capital asset rather than a running cost, since that
  changes it from a deduction to a capital allowance.

Cite the rule you are relying on (e.g. "input VAT disallowed per Sec 17(2)"). Say when
a human tax adviser should look at it. Be brief and specific; if the item description
is too vague to judge (for example "SUMMARIZED SALE"), say so rather than inventing a
purpose for it.
"""

JUDGMENT_TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "save_receipt_judgment",
            "description": "Saves the expense categorisation and tax analysis for a receipt whose facts are already known.",
            "parameters": {
                "type": "object",
                "properties": {
                    "category": {
                        "type": "string",
                        "enum": EXPENSE_CATEGORIES,
                        "description": "The expense category that best fits the line items.",
                    },
                    "llm_extracted_description": {
                        "type": "string",
                        "description": "One sentence describing what was bought, e.g. 'Diesel for the delivery van.' No amounts or dates.",
                    },
                    "llm_tax_analysis": {
                        "type": "string",
                        "description": "Deductibility, input VAT and withholding tax implications under Tanzanian law, with the rule relied on. 1-3 sentences.",
                    },
                    "requires_review": {
                        "type": "boolean",
                        "description": "True when a human tax adviser should look at this receipt before it is claimed.",
                    },
                },
                "required": ["category", "llm_extracted_description", "llm_tax_analysis"],
            },
        },
    }
]

# Photographed receipts only: there is no verified page to parse, so the model has to
# read the fields off the image.
#
# The paragraph about the verification code and the time is doing more work than the
# rest of this prompt put together. Those two fields are not just data: together they
# rebuild https://verify.tra.go.tz/<CODE>_<HHMMSS>, which is the address of the receipt
# on TRA's own portal - so reading them off the paper turns a photograph the model
# guessed at into a receipt the revenue authority confirms. They are printed in large
# type at the foot of every EFD receipt, right above the QR code, which means they
# survive exactly the conditions that defeat the QR: a crease through the code, a
# thumb over one corner, a fading print, a photo taken at too shallow an angle for the
# finder patterns to resolve.
VISION_SYSTEM_PROMPT = """
You are an expert in Tanzanian tax compliance (Income Tax Act / VAT Act) reading a
photographed receipt.

Transcribe what is printed on it using `save_extracted_receipt_data`, then give a
brief tax analysis. Transcribe only what you can actually see: leave a field out
rather than guessing at it, and never round or reconstruct an amount. Amounts are in
TZS.

Two fields matter more than the others, so read them character by character before you
read anything else:

  * RECEIPT VERIFICATION CODE - printed near the bottom, above or beside the QR code,
    typically 8-14 letters and digits with no spaces.
  * TIME - the time of sale printed near the receipt date, as HH:MM:SS.

Those two identify the receipt on TRA's verification portal, so getting them exactly
right is what lets the sale be confirmed against TRA rather than taken on trust. Read
them from the printed characters. Do not infer either of them from the QR code, from
the receipt number, or from any other field, and if the print is genuinely unreadable
leave the field out rather than offering a plausible guess - a wrong code is worse
than a missing one, because it sends us to a different receipt.

Not every photograph is a Tanzanian EFD receipt. A genuine one carries a TIN, a
receipt verification code and usually a Z number and an EFD serial. A handwritten
chit, a parking stub, a proforma invoice, a delivery note or a foreign till slip is
still a business document worth recording, but it is not an EFD receipt and must not
be described as one - say which it is in `document_type`.

The photograph sometimes arrives with a note from the person who took it. Treat it the
way you would treat them standing beside you: it says what the paper cannot - which
vehicle the diesel went into, whose lunch it was, what the job was for - and it is
often the only thing that settles the category and the business purpose. It is not part
of the receipt. Never transcribe a figure, a date, a TIN or a code out of it, and where
it contradicts what is printed, what is printed wins.
"""

TEXT_RECORD_SYSTEM_PROMPT = """
You are an expert in Tanzanian tax compliance (Income Tax Act / VAT Act) reading a
written record of a purchase - not a receipt, but the text somebody was given instead
of one.

This is what a great many real expenses look like here. A LUKU electricity purchase
comes back as an SMS carrying a meter number, a token and an amount. Water, DAWASCO
bills, mobile money transfers, bank alerts, airtime top-ups and government payment
confirmations all arrive the same way: a few lines of text, and nothing else will ever
be issued for them.

Record what the text actually says using `save_extracted_receipt_data`, then give a
brief tax analysis. Transcribe only what is written: leave a field out rather than
guessing at it, and never round or reconstruct an amount. Amounts are in TZS unless
the text names another currency.

Reading notes for the formats this sees most:

  * The vendor is whoever was paid - TANESCO for a LUKU token, DAWASA for water, the
    merchant named in a mobile money confirmation. The sender ID of an SMS is often the
    best name available; use it rather than leaving the vendor blank.
  * A LUKU token, a mobile money transaction ID, a bank reference or a control number
    is the identifier for this payment. Put it in `receipt_number`.
  * Do not put any of those in `receipt_verification_code`. That field means one thing
    only: the code TRA prints beside the QR square on an EFD receipt. A transaction
    reference is not one, and offering it as one sends us to look up a receipt that
    does not exist.
  * If the text is a pasted TRA verification code and time - and only then - transcribe
    them into `receipt_verification_code` and `receipt_time` exactly as written.
  * Units bought (kWh on a LUKU token), the meter or account number, and any service
    charge or VAT line are worth recording as items.

`document_type` is almost always 'other_receipt' here: a real proof of purchase, just
not an EFD receipt. Use 'tra_efd_receipt' only if the text is a transcription of one,
and 'not_a_receipt' if it records no purchase at all.

The paste sometimes arrives with a note from the person who sent it, given separately
from the record itself. It says what the text cannot - which meter the token was for,
what the transfer was paying - and it is often the only thing that settles the category
and the business purpose. It is not part of the record: never transcribe a figure, a
date or a reference out of it, and where it contradicts the record, the record wins.
"""

VISION_TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "save_extracted_receipt_data",
            "description": "Saves the fields transcribed from a receipt - photographed, or written out as text.",
            "parameters": {
                "type": "object",
                "properties": {
                    "vendor_name": {"type": "string"},
                    "vendor_tin": {"type": "string"},
                    "vendor_phone": {"type": "string"},
                    "vrn": {"type": "string", "description": "The vendor's VAT Registration Number (VRN). "
                                                            "Omit it if the receipt prints 'NOT REGISTERED' or "
                                                            "any other placeholder in place of a number."},
                    "receipt_date": {"type": "string", "description": "YYYY-MM-DD format."},
                    "receipt_time": {
                        "type": "string",
                        "description": "The time of sale printed on the receipt, HH:MM:SS in 24 hour "
                                       "form. Half of the TRA verification address, so transcribe it "
                                       "exactly; omit it only if no time is printed at all.",
                    },
                    "receipt_number": {"type": "string"},
                    "z_number": {"type": "string"},
                    "efd_serial": {"type": "string", "description": "The EFD machine serial number, if printed."},
                    "uin": {"type": "string"},
                    "total_amount": {"type": "number", "description": "Total including tax."},
                    "total_excl_tax": {"type": "number"},
                    "vat_amount": {"type": "number", "description": "Total tax charged."},
                    "receipt_verification_code": {
                        "type": "string",
                        "description": "The RECEIPT VERIFICATION CODE printed near the QR code, "
                                       "letters and digits only. The other half of the TRA "
                                       "verification address. Omit it rather than guessing.",
                    },
                    "document_type": {
                        "type": "string",
                        "enum": ["tra_efd_receipt", "other_receipt", "not_a_receipt"],
                        "description": "'tra_efd_receipt' for a Tanzanian EFD receipt (it will carry a "
                                       "TIN and a receipt verification code); 'other_receipt' for any "
                                       "other proof of purchase - a parking stub, a handwritten chit, a "
                                       "proforma invoice, a foreign till slip; 'not_a_receipt' if the "
                                       "photograph is not a purchase document at all.",
                    },
                    "customer_name": {"type": "string"},
                    "customer_id_type": {"type": "string"},
                    "customer_id": {"type": "string"},
                    "items": {
                        "type": "array",
                        "description": "The purchased items exactly as listed on the receipt.",
                        "items": {
                            "type": "object",
                            "properties": {
                                "description": {"type": "string"},
                                "quantity": {"type": "number"},
                                "amount": {"type": "number"},
                                "tax_code": {"type": "string", "description": "A, B, C, SR or EX, if printed."},
                            },
                            "required": ["description"],
                        },
                    },
                    "is_cancelled": {"type": "boolean", "description": "True if the receipt is marked cancelled or void."},
                    "category": {"type": "string", "enum": EXPENSE_CATEGORIES},
                    "llm_extracted_description": {"type": "string", "description": "A concise, one-sentence summary of the purchase."},
                    "llm_tax_analysis": {"type": "string", "description": "Deductibility and withholding tax implications under Tanzanian law. Keep it brief."},
                },
                "required": ["vendor_name", "receipt_date", "total_amount", "document_type",
                             "llm_extracted_description", "llm_tax_analysis"],
            },
        },
    }
]


class LlmUnavailable(Exception):
    """
    The judgment call could not be completed.

    Raised so the caller can record the receipt anyway: the facts do not depend on the
    model, so an outage should cost the analysis and nothing else.
    """


def encode_image_to_base64(image_path):
    """
    The photograph as base64, bounded to what the model can actually use.

    Not the file on disk. A stored receipt is kept at utils.images.STORED_MAX_EDGE,
    which is set by the QR decoder rather than by this reader, and every pixel above
    what the model needs is one encoded a third larger into a data URL, pushed over the
    line, and charged for as image tokens - on an instance that may be working through
    a day's backlog. See utils.images.encoded_for_model for the size and why.
    """
    return images.encoded_for_model(image_path)

def get_llm_client(config):
    if config.llm_provider == 'groq' and config.llm_api_key:
        print("[LLM] Initializing Groq client.")
        return openai.OpenAI(
            api_key=config.llm_api_key,
            base_url="https://api.groq.com/openai/v1"
        )
    print(f"[LLM] Initializing {config.llm_provider} client.")
    return openai.OpenAI(api_key=config.llm_api_key)


# --- Which model, and what to do when it stops existing -----------------------------
#
# Hosted models are retired on the provider's schedule, not ours, and the failure is
# not gentle: every photographed receipt began failing with "The model
# `meta-llama/llama-4-scout-17b-16e-instruct` does not exist or you do not have access
# to it" the day Groq decommissioned it, with a working API key and nothing on our side
# changed. A single hard-coded model name is a deployment that breaks on somebody
# else's calendar, so this file no longer holds one.
#
# Three layers, in order:
#   1. What the admin pinned on the configure page (or an env var), if anything.
#   2. A short list of known-good candidates, tried in turn and skipped when the
#      provider says the model is gone.
#   3. For providers that publish a catalogue, the catalogue itself - so an instance
#      recovers from a rename nobody has heard of yet without a redeploy.
#
# Whatever ends up working is remembered for the life of the process, so the cost of a
# retired model is one wasted round trip after a restart rather than one per receipt.

MODEL_CANDIDATES = {
    ('groq', 'text'): [
        'llama-3.3-70b-versatile',
        'openai/gpt-oss-120b',
        'llama-3.1-8b-instant',
    ],
    # Groq serves exactly one model that can see, and it has changed hands twice: the
    # Llama 3.2 vision pair went in April 2025, their replacements Scout and Maverick
    # went in March and July 2026. Both of those were still first in this list a month
    # after the second one was switched off, which is how every photographed receipt on
    # this instance ended up at the bottom of the ladder with nothing left to try.
    # The llama-4 names are not kept as a tail: they are gone for good, and leaving them
    # in buys one guaranteed 404 per process start in exchange for nothing.
    ('groq', 'vision'): [
        'qwen/qwen3.6-27b',
    ],
    ('openai', 'text'): ['gpt-4o-mini', 'gpt-4o'],
    ('openai', 'vision'): ['gpt-4o', 'gpt-4o-mini'],
}

# Extra request parameters a model needs before it will answer with a tool call.
#
# Matched on a fragment of the model id rather than listed exactly, because the whole
# point of the ladder below is to survive a name we have never seen - a candidate found
# in the catalogue needs these as much as a pinned one does.
#
# Qwen is the case that forces this. It is a hybrid reasoning model, and on Groq
# `reasoning_format` *must* be 'parsed' or 'hidden' when tools are in the request;
# leaving it at the default puts the model's thinking in the same channel as the answer
# and the tool call comes back unparseable, which reads from here as a model that
# cannot fill the form in. `reasoning_effort: none` on top of that because transcribing
# a receipt is not a reasoning problem - the thinking is pure latency, and on a dense
# receipt it eats into a 16k output budget the item list needs.
#
# Anything not listed is sent nothing extra, which is the right default: an unknown
# parameter is a 400 from most providers, and `_rejects_parameter` below is a safety
# net rather than a plan.
MODEL_EXTRAS = (
    ('qwen', {'reasoning_format': 'hidden', 'reasoning_effort': 'none'}),
    ('minimax', {'reasoning_format': 'hidden'}),
)

# Substrings that mark a catalogue entry as worth trying for each job, used only when
# every known candidate has been retired. Deliberately generous rather than precise:
# nothing in a model catalogue says which models can see, so the alternative to
# guessing widely is guessing narrowly and finding nothing at all. A wrong guess is
# cheap because `_cannot_see_images` catches it and moves on.
CATALOGUE_HINTS = {
    'vision': ('vision', 'scout', 'maverick', 'llama-4', '-vl', 'omni', '4o',
               'gemma', 'pixtral', 'llava', 'multimodal',
               # Not decoration. When Scout and Maverick were retired this list was
               # consulted, matched nothing in Groq's catalogue, and reported that no
               # model was available - while the model that replaced them, qwen3.6, was
               # sitting in the very list being filtered. A hint set that only knows the
               # names of the models it has already lost is a self-healing layer that
               # heals nothing.
               'qwen', 'minimax', 'moondream', 'intern'),
    'text': ('versatile', 'instruct', 'gpt-oss', 'instant', 'gpt-4', 'qwen', 'kimi'),
}

# Never useful here whatever their name suggests: speech, moderation and embeddings.
CATALOGUE_EXCLUDE = ('whisper', 'tts', 'guard', 'embed', 'moderation', 'rerank')

# (provider, kind) -> the model id that last worked in this process.
_PROVEN_MODEL = {}


class NoUsableModel(Exception):
    """Every model we know how to ask for has been retired or is not accessible."""


def _is_missing_model(error):
    """
    Whether this failure means 'that model is gone', as opposed to a real outage.

    Matched on the provider's own words rather than the status code: Groq answers 404
    with code 'model_not_found', OpenAI answers 404 for a retired model and 403 for one
    the key has no access to, and both read the same to us - try the next name.
    """
    text = str(error).lower()
    return any(marker in text for marker in (
        'model_not_found', 'does not exist', 'decommissioned', 'deprecated',
        'has been retired', 'no access to',
        # Covers both 'you do not have access' and 'your key does not have access'.
        'not have access',
    ))


def _cannot_see_images(error):
    """
    Whether this model exists but cannot read a photograph.

    Only consulted for the vision job, and only to decide whether to try the next
    candidate. It is what makes guessing from the catalogue safe: a name that looked
    multimodal and is not costs one round trip, not the receipt.
    """
    text = str(error).lower()
    return any(marker in text for marker in (
        'does not support image', 'does not support vision', 'no vision support',
        'image input', 'image_url', 'multimodal', 'unsupported content',
    ))


def _is_malformed_tool_call(error):
    """
    Whether the model tried to fill the tool in and produced something unparseable.

    This is not an outage and not a bad request on our side - the provider is reporting
    that the text the model generated did not come out as a tool call it could read.
    Groq answers 400 'tool_use_failed' with the half-written call attached, and what is
    attached is usually a call that simply stops mid-argument: a receipt with a long
    item list and a long analysis can run the generation out of room before the closing
    tag, and one that does is a receipt this app dropped on the floor.

    The other half of it never reaches the provider's validator at all: a 200 carrying
    a chatty paragraph about the receipt and no tool call, which `_call_tool` raises as
    MalformedToolCall once it has failed to find the fields in the prose. Same event
    from this function's point of view - the model meant to answer and did not manage
    the shape.

    Worth separating from the failures around it because the response to it is
    different. A retired model is retired for good and gets crossed off; this is a roll
    of the dice, so the same model is asked once more, and only a second failure moves
    on to the next candidate - which is often enough a different size of model that
    does not have the problem.
    """
    if isinstance(error, MalformedToolCall):
        return True
    text = str(error).lower()
    return any(marker in text for marker in (
        'tool_use_failed', 'failed to call a function', 'failed_generation',
    ))


def _pinned_model(config, kind):
    """The model this instance was told to use, if it was told anything."""
    attribute = 'llm_vision_model' if kind == 'vision' else 'llm_text_model'
    pinned = (getattr(config, attribute, None) or '').strip()
    if pinned:
        return pinned
    return (os.environ.get(f'LLM_{kind.upper()}_MODEL') or '').strip() or None


def _model_candidates(config, kind):
    """Every model id to try for this job, best first, without duplicates."""
    ordered = []
    for model in (_pinned_model(config, kind), _PROVEN_MODEL.get((config.llm_provider, kind))):
        if model and model not in ordered:
            ordered.append(model)

    for model in MODEL_CANDIDATES.get((config.llm_provider, kind), []):
        if model not in ordered:
            ordered.append(model)

    # An unknown provider speaking the OpenAI protocol gets whatever it was pinned to,
    # and gpt-4o as the one guess worth making.
    if not ordered:
        ordered.append('gpt-4o')
    return ordered


def _catalogue_candidates(client, kind):
    """
    What the provider says it currently serves, filtered to plausible picks.

    Only consulted once every known name has come back retired, which is the moment an
    instance would otherwise be dead until someone shipped a new constant.
    """
    try:
        listed = [model.id for model in client.models.list().data]
    except Exception as e:
        print(f"[LLM] Could not read the provider's model catalogue: {e}")
        return []

    hints = CATALOGUE_HINTS[kind]
    picks = [
        model_id for model_id in listed
        if any(hint in model_id.lower() for hint in hints)
        and not any(bad in model_id.lower() for bad in CATALOGUE_EXCLUDE)
    ]
    print(f"[LLM] Catalogue offers {len(listed)} model(s); {len(picks)} plausible for {kind}: {picks}")
    return picks


class MalformedToolCall(Exception):
    """
    The model answered, but not with a tool call this code could use.

    Deliberately the same class of event as the provider's own 'tool_use_failed': the
    model exists, it can see, it simply did not produce the structured answer this time.
    Recoverable by asking again, so it must never look like an outage.
    """


def _tool_choice(expected_name, force):
    """
    What to put in `tool_choice`. Naming the function is the honest request.

    Both jobs here are structured extraction - there is no reply that is not a tool
    call, and 'auto' invites the model to write a paragraph about the receipt instead,
    which is precisely the failure this is fixing. Some OpenAI-compatible endpoints do
    not implement the named form, which is what `force=False` is for: `_call_tool`
    drops back to it when a provider says it cannot honour the request.
    """
    if not force:
        return "auto"
    return {"type": "function", "function": {"name": expected_name}}


def _rejects_tool_choice(error):
    """Whether the provider is refusing the request itself rather than answering it."""
    text = str(error).lower()
    return 'tool_choice' in text and any(
        marker in text for marker in ('not support', 'unsupported', 'invalid', 'unknown'))


def _request_extras(model, kind):
    """
    The extra body parameters this model needs for this job, if any.

    Two sources, and they answer different questions. MODEL_EXTRAS is about the model -
    what it needs before a tool call comes back in one piece. The temperature is about
    the job: a receipt is transcribed, not composed, and the fields that matter most
    here are a verification code and a time that have to come back character for
    character. Sampling at a provider's chatty default is how a 6 becomes a 5 in a code
    that then names somebody else's receipt on the portal.
    """
    extras = {}
    for fragment, params in MODEL_EXTRAS:
        if fragment in model.lower():
            extras.update(params)
    if kind == 'vision':
        extras['temperature'] = 0
    return extras


def _rejected_parameters(error, extras):
    """
    Which of the extras the provider is refusing, by name. Empty when it is refusing
    something else entirely.

    Sibling of `_rejects_tool_choice`, and there for the same reason: the parameters
    above are worked out from a fragment of a model id, so a model named 'qwen-
    something' on a provider that has never heard of `reasoning_format` is a matter of
    time. Asking again without it costs a round trip; not asking again costs the
    receipt.

    Only the parameter the provider actually named is dropped. Clearing the lot would
    take the temperature down with it, and a retry that quietly starts sampling the
    verification code for variety is a worse answer than the error it replaced.
    """
    text = str(error).lower()
    if not any(marker in text for marker in
               ('unsupported', 'not support', 'unrecognized', 'unknown', 'invalid', 'must be')):
        return set()
    return {name for name in extras if name in text}


def _tool_parameters(tools, expected_name):
    """The JSON Schema for the tool's arguments, or an empty schema."""
    for tool in tools or []:
        function = tool.get('function', {}) if isinstance(tool, dict) else {}
        if function.get('name') == expected_name:
            return function.get('parameters') or {}
    return {}


def _required_fields(tools, expected_name):
    """The tool's required argument names, used to judge a salvage attempt."""
    return list(_tool_parameters(tools, expected_name).get('required') or [])


def _declared_types(schema):
    """The type names a schema allows, as a tuple. JSON Schema permits a list of them."""
    declared = schema.get('type') if isinstance(schema, dict) else None
    if isinstance(declared, str):
        return (declared,)
    if isinstance(declared, (list, tuple)):
        return tuple(name for name in declared if isinstance(name, str))
    return ()


# What a model writes when it means yes or no but the field it is filling in is text.
# 'none'/'null'/'' are here because an omitted boolean often arrives as an empty
# parameter block rather than as no block at all.
_TRUTHY_WORDS = ('true', 'yes', 'y', '1')
_FALSY_WORDS = ('false', 'no', 'n', '0', 'none', 'null', '')


def _as_string(value):
    """A scalar as the text it was printed as, without picking up a float's tail."""
    if isinstance(value, bool):
        return 'true' if value else 'false'
    if isinstance(value, float) and value.is_integer():
        return str(int(value))
    return str(value)


def _as_boolean(value):
    """A written-out yes or no as a boolean, or None if it is neither."""
    if isinstance(value, str):
        word = value.strip().lower()
        if word in _TRUTHY_WORDS:
            return True
        if word in _FALSY_WORDS:
            return False
        return None
    if isinstance(value, (int, float)):
        return bool(value)
    return None


def _as_number(value):
    """A printed amount as a number, or None. Thousands separators are tolerated."""
    if not isinstance(value, str):
        return None
    try:
        parsed = json.loads(value.strip().replace(',', '').lstrip('+'))
    except ValueError:
        return None
    return parsed if isinstance(parsed, (int, float)) and not isinstance(parsed, bool) else None


def _coerced(value, schema):
    """
    One value pushed into the type its schema declares, where that is lossless.

    Only ever applied to a salvaged call, where every value started life as the text
    between two tags and the types are guesses made after the fact. Two of those guesses
    are actively dangerous. A TIN or a receipt number read as JSON comes back an integer,
    and turning it back into text here is what stops `114605836` reaching the database as
    a number and a leading zero being dropped from the vendor it names. And a model that
    writes Python rather than JSON emits `False` for a boolean, which is not valid JSON,
    stays the five-character string it was printed as, and is therefore *true* - quietly
    marking a live receipt cancelled. Anything that will not convert cleanly is left
    exactly as it came, for the required-field check to judge.
    """
    types = _declared_types(schema)
    if not types:
        return value

    if 'string' in types and isinstance(value, (int, float, bool)):
        return _as_string(value)
    if 'boolean' in types and not isinstance(value, bool):
        decided = _as_boolean(value)
        return value if decided is None else decided
    if ('number' in types or 'integer' in types) and isinstance(value, str):
        number = _as_number(value)
        return value if number is None else number
    if 'array' in types and isinstance(value, list):
        item_schema = schema.get('items') or {}
        return [_coerced(item, item_schema) for item in value]
    if isinstance(value, dict) and (schema.get('properties') or 'object' in types):
        return _coerced_object(value, schema)
    return value


def _coerced_object(arguments, schema):
    """Every value in a salvaged call put through `_coerced` against its own field."""
    if not isinstance(arguments, dict):
        return arguments
    properties = (schema or {}).get('properties') or {}
    return {key: _coerced(value, properties[key]) if key in properties else value
            for key, value in arguments.items()}


def _error_body_from_text(error_text):
    """
    The provider's JSON body read back out of a stringified error.

    The SDK puts the parsed body on the exception, but not every path here holds the
    SDK's own exception class - a re-raise, a wrapper, a test double - and the body is
    printed into the message either way. `ast.literal_eval` rather than `json.loads`
    because what is printed is a Python dict repr, single quotes and all.
    """
    start = error_text.find('{')
    if start == -1:
        return None
    try:
        parsed = ast.literal_eval(error_text[start:])
    except (ValueError, SyntaxError, MemoryError, RecursionError):
        return None
    return parsed if isinstance(parsed, dict) else None


def _failed_generation(error):
    """
    The tool call the provider rejected, as the model actually wrote it, or None.

    This is the most valuable thing in an error this app ever receives and it was being
    thrown away. Groq validates the model's generation against the tool's schema before
    it will hand it over, and answers 400 `tool_use_failed` when the two disagree - with
    the entire generation attached. The disagreement is routinely cosmetic: a model that
    writes its call as `<parameter=vendor_tin>114605836</parameter>` has printed every
    digit of a perfectly read receipt, and the only thing wrong is that the schema calls
    that field a string and a bare number is not one.

    Asking again does not help, because nothing about it was random - the same model
    reading the same photograph writes the same number the same way, which is exactly
    the loop this app was stuck in: two generations, two identical rejections, one lost
    receipt. The generation is right there. Read it.
    """
    candidates = (getattr(error, 'body', None), _error_body_from_text(str(error)))
    for body in candidates:
        if not isinstance(body, dict):
            continue
        detail = body.get('error') if isinstance(body.get('error'), dict) else body
        attached = detail.get('failed_generation')
        if isinstance(attached, str) and attached.strip():
            return attached
    return None


def _call_tool(client, model, messages, tools, expected_name, kind='text', force=True,
               extras=None):
    """Runs one tool-calling round trip and returns the parsed arguments."""
    extras = _request_extras(model, kind) if extras is None else extras
    parameters = _tool_parameters(tools, expected_name)
    try:
        response = client.chat.completions.create(
            model=model,
            messages=messages,
            tools=tools,
            tool_choice=_tool_choice(expected_name, force),
            # Through extra_body rather than as named arguments: `reasoning_format` is
            # Groq's, not the OpenAI schema's, and the SDK would drop it on the floor.
            **({'extra_body': extras} if extras else {}),
        )
    except Exception as e:
        if force and _rejects_tool_choice(e):
            print(f"[LLM] '{model}' will not be told which tool to call; asking loosely.")
            return _call_tool(client, model, messages, tools, expected_name, kind,
                              force=False, extras=extras)
        rejected = _rejected_parameters(e, extras) if extras else set()
        if rejected:
            print(f"[LLM] '{model}' will not take {sorted(rejected)}; asking without.")
            return _call_tool(
                client, model, messages, tools, expected_name, kind, force=force,
                extras={k: v for k, v in extras.items() if k not in rejected})

        # The provider rejected the model's generation rather than the request, and
        # attached the generation. Usually the whole receipt, refused over a type.
        attached = _failed_generation(e)
        if attached:
            recovered = _arguments_from_text(attached, parameters)
            if recovered is not None:
                print(f"[LLM] '{model}' wrote a tool call the provider would not "
                      "accept; read the receipt out of it.")
                return recovered
        raise

    message = response.choices[0].message

    for tool_call in getattr(message, 'tool_calls', None) or []:
        if tool_call.function.name != expected_name:
            raise MalformedToolCall(f"The model called '{tool_call.function.name}' rather "
                                    f"than '{expected_name}'.")
        try:
            return json.loads(tool_call.function.arguments)
        except ValueError:
            # The arguments came back as something other than JSON. Same salvage as
            # below: a model that writes its call out longhand still said everything.
            recovered = _arguments_from_text(tool_call.function.arguments, parameters)
            if recovered is not None:
                print(f"[LLM] '{model}' sent arguments that were not JSON; read them anyway.")
                return recovered
            raise MalformedToolCall("The tool call's arguments were not readable JSON.")

    # No tool call at all. Before spending another generation on it, look at what the
    # model did send: the common shape of this failure is not a refusal but a model
    # writing the call out as text - as JSON in a code fence, or in the pseudo-XML the
    # Llama family emits when its tool syntax does not survive the sampler. All of the
    # receipt is usually there. Reading it is free; asking again is not.
    recovered = _arguments_from_text(getattr(message, 'content', '') or '', parameters)
    if recovered is not None:
        print(f"[LLM] '{model}' answered in text rather than calling {expected_name}; "
              "recovered the fields from the reply.")
        return recovered

    raise MalformedToolCall(
        f"The model answered without calling {expected_name}, and the reply did not "
        "contain the fields either."
    )


def _arguments_from_text(text, parameters):
    """
    The tool's arguments dug out of a reply that was not a tool call, or None.

    Three shapes, all seen in the wild from models that mean to call the tool and miss:
    a JSON object on its own or in a code fence, a `<tool_call>` wrapper around one, and
    the pseudo-XML `<function=name><parameter=key>value</parameter>` form the Llama
    models fall back into - which is also exactly what Groq attaches to its 400s as
    `failed_generation`.

    Returns None unless every required field is present, and that condition is the
    whole safety of this function. A generation that ran out of room stops mid-argument,
    so a partial parse is not a partial receipt but a plausible-looking one missing the
    verification code, and storing that is worse than the failure it replaces. Anything
    short of complete is left to the retry.

    Whatever comes back is put through the tool's own schema on the way out: every value
    in these shapes was text a moment ago, and the type it lands on is a guess made after
    the fact. See `_coerced`.
    """
    if not text:
        return None

    parameters = parameters or {}
    required = list(parameters.get('required') or [])
    properties = parameters.get('properties') or {}

    for candidate in (_json_objects(text), _pseudo_xml_arguments(text, properties)):
        for arguments in candidate:
            if isinstance(arguments, dict) and all(key in arguments for key in required):
                return _coerced_object(arguments, parameters)
    return None


def _json_objects(text):
    """Every parseable JSON object in `text`, outermost first, unwrapped if wrapped."""
    found = []
    # Scanning for balanced braces rather than a regex: the arguments contain nested
    # objects (the item list), which is exactly what a regex cannot count.
    for start, character in enumerate(text):
        if character != '{':
            continue
        depth = 0
        for end in range(start, len(text)):
            depth += (text[end] == '{') - (text[end] == '}')
            if depth == 0:
                try:
                    parsed = json.loads(text[start:end + 1])
                except ValueError:
                    break
                # `{"name": ..., "arguments": {...}}` is a tool call written out in
                # full; what we want is one level in.
                if isinstance(parsed, dict) and isinstance(parsed.get('arguments'), dict):
                    parsed = parsed['arguments']
                found.append(parsed)
                break
    return found


def _pseudo_xml_arguments(text, properties=None):
    """
    Arguments read out of `<parameter=key>value</parameter>` blocks, as a one-item list.

    Values are put through json.loads where they will take it, so a number arrives as a
    number and the item list as a list; anything else stays the string it was printed
    as. An unterminated final parameter - the signature of a generation that ran out of
    room - is dropped rather than half-read.

    A field the tool declares as text skips json.loads entirely rather than being
    converted and converted back. The round trip is not free: `01181` is a Z number and
    also very nearly a JSON integer, and a TIN long enough to be read as a float would
    come back out of one rounded. What was printed on the receipt is what is wanted.
    """
    pairs = re.findall(r'<parameter=([^>\s]+)>(.*?)</parameter>', text, re.DOTALL)
    if not pairs:
        return []

    properties = properties or {}
    arguments = {}
    for key, value in pairs:
        value = value.strip()
        if 'string' in _declared_types(properties.get(key) or {}):
            arguments[key] = value
            continue
        try:
            arguments[key] = json.loads(value)
        except ValueError:
            arguments[key] = value
    return [arguments]


def _call_tool_with_retry(client, model, kind, messages, tools, expected_name):
    """
    One model, asked up to twice, and only when the first answer was garbled.

    A mangled tool call is a sampling accident rather than a property of the model, so
    the cheapest correct response is to ask the same model the same question again -
    and it is genuinely cheap, because the only way to get here is a failure that
    already cost a generation. Anything else propagates on the first attempt.
    """
    for attempt in (1, 2):
        try:
            print(f"[LLM] Asking '{model}' ({kind})" + (" - second attempt." if attempt == 2 else "."))
            return _call_tool(client, model, messages, tools, expected_name, kind)
        except Exception as e:
            if attempt == 2 or not _is_malformed_tool_call(e):
                raise
            print(f"[LLM] '{model}' returned a tool call that would not parse; asking once more.")


def _call_with_fallback(client, config, kind, messages, tools, expected_name):
    """
    Runs the tool call against the first model that still exists.

    Only a model that is gone - or, for the vision job, one that turns out not to be
    able to see, or one that could not produce a usable tool call twice running - moves
    us on to the next name. Every other failure (a rate limit, a timeout, a refusal) is
    raised immediately, because trying a different model would not fix it and would
    charge for the attempt.
    """
    retired = []
    garbled_by = []
    candidates = _model_candidates(config, kind)

    while candidates:
        model = candidates.pop(0)
        try:
            result = _call_tool_with_retry(client, model, kind, messages, tools, expected_name)
        except Exception as e:
            blind = kind == 'vision' and _cannot_see_images(e)
            garbled = _is_malformed_tool_call(e)
            if not blind and not garbled and not _is_missing_model(e):
                raise

            if garbled:
                # Not crossed off, and not forgotten as the proven model the way a
                # retired one is below: this model exists and works, it just could not
                # get this particular receipt out in one piece. Nothing about that is
                # worth remembering past the receipt that caused it.
                print(f"[LLM] Model '{model}' could not produce a usable tool call: {e}")
                garbled_by.append(model)
                continue

            print(f"[LLM] Model '{model}' "
                  f"{'cannot read images' if blind else 'is no longer available'}: {e}")
            retired.append(model)
            _PROVEN_MODEL.pop((config.llm_provider, kind), None)
            # Last known name just went; ask the provider what it actually serves.
            if not candidates:
                candidates = [m for m in _catalogue_candidates(client, kind) if m not in retired]
            continue

        if _PROVEN_MODEL.get((config.llm_provider, kind)) != model:
            print(f"[LLM] Using '{model}' for {kind} from now on.")
            _PROVEN_MODEL[(config.llm_provider, kind)] = model
        return result

    # Two different endings, because they need two different things done about them. A
    # list of retired models is a configuration problem: nothing this instance knows how
    # to ask for still exists. Models that answered but garbled the call are a document
    # problem - they are all still there, and the next receipt will very likely go
    # through - so the message says so rather than sending someone to the settings page
    # to fix something that is not broken.
    if garbled_by and not retired:
        raise LlmUnavailable(
            f"No {kind} model could produce a usable answer for this one. Tried "
            f"{', '.join(garbled_by)}, twice each. This is usually a single awkward "
            f"document rather than a broken instance - the submission can be retried."
        )

    raise NoUsableModel(
        f"No {kind} model is available from {config.llm_provider}. Tried: "
        f"{', '.join(retired + garbled_by) or 'nothing'}. Set the model on the configure page."
    )

def analyse_receipt(facts: dict, config, user_note: str = None) -> dict:
    """
    Asks the model to categorise a receipt whose facts are already established.

    `facts` is the compact JSON from ParsedReceipt.as_llm_facts() - roughly a tenth of
    the tokens the scraped page used to cost, and with nothing in it the model could
    misread. Raises LlmUnavailable on any failure; the receipt is still recordable.
    """
    if not config or not config.llm_api_key:
        raise LlmUnavailable("LLM API key is not configured.")

    prompt = (
        "Categorise this receipt and give your tax analysis. The facts below are "
        "already verified - use them, do not restate them.\n\n"
        f"{json.dumps(facts, indent=None, separators=(',', ':'))}"
    )
    prompt += _note_block(user_note)

    messages = [
        {"role": "system", "content": JUDGMENT_SYSTEM_PROMPT},
        {"role": "user", "content": prompt},
    ]

    try:
        print(f"[LLM] Requesting judgment ({len(prompt)} chars of facts).")
        judgment = _call_with_fallback(
            get_llm_client(config), config, 'text', messages,
            tools=JUDGMENT_TOOLS, expected_name='save_receipt_judgment',
        )
    except Exception as e:
        print(f"[LLM Error] Judgment call failed: {e}")
        raise LlmUnavailable(str(e)) from e

    # The model has no business returning facts; drop anything that looks like one so
    # a stray field can never reach the ledger.
    allowed = {'category', 'llm_extracted_description', 'llm_tax_analysis', 'requires_review'}
    judgment = {key: value for key, value in judgment.items() if key in allowed}

    if judgment.get('category') not in EXPENSE_CATEGORIES:
        judgment['category'] = 'other'

    print(f"[LLM] Judgment received: category={judgment.get('category')}")
    return judgment

# How much pasted text is worth sending to the model.
#
# Everything this path is for - a LUKU SMS, a mobile money confirmation, a bank alert -
# is a few hundred characters. A cap exists because the box it arrives in accepts a
# paste of any size, and a whole email thread or a copied web page would otherwise be
# billed as input tokens on every retry. Generous enough that nothing real is cut.
TEXT_RECORD_MAX_CHARS = 4000

# And how much of the sender's note. It is one line typed on a phone with a receipt in
# the other hand; anything past this is a paste that belongs in the record itself.
USER_NOTE_MAX_CHARS = 600


def _note_block(user_note):
    """
    The sender's note as a labelled block, or '' when there is none.

    Labelled every time it is sent, and always after the document, because the thing to
    avoid is the model reading it as more of the receipt. What somebody typed on a phone
    is not evidence of what was printed - it is the reason the purchase happened, which
    is exactly the half of the categorisation question the paper never answers.
    """
    note = ' '.join((user_note or '').split())[:USER_NOTE_MAX_CHARS]
    if not note:
        return ''
    return (
        '\n\nThe person who submitted it added this note. It is context for the category '
        'and the business purpose, not part of the document - do not transcribe anything '
        f'out of it:\n{note}'
    )


def extract_receipt_details(content, is_image, config, user_note=None):
    """
    Reads a receipt the only way left: by having a model transcribe it.

    Two inputs, one shape of answer. `is_image` True means `content` is a path to a
    photograph and the vision model reads it. False means `content` is the text
    somebody was given instead of a receipt - the LUKU SMS, the mobile money
    confirmation - and the ordinary text model reads that. Both fill in the same tool
    schema, so everything downstream (_store_llm_draft, _receipt_from_transcription,
    the admin's field-by-field correction) has exactly one shape to handle.

    `user_note` is what the person submitting it typed in the box beside the camera. It
    reaches the model with the document because it answers the question the document
    cannot - 'diesel for the generator, not the van' is the whole difference between two
    categories, and no amount of looking at the paper will produce it. It is labelled as
    a note rather than run together with the receipt, and the prompts above say plainly
    that nothing may be transcribed out of it: it decides the judgment, never the facts.

    Receipts submitted as a TRA URL never come through here at all: their facts are
    parsed from the verified page. See utils/tra_parser.parse_receipt_html.
    """
    if not config or not config.llm_api_key:
        raise ValueError("LLM API key is not configured.")

    note = _note_block(user_note)
    if is_image:
        messages = [
            {"role": "system", "content": VISION_SYSTEM_PROMPT},
            {
                "role": "user",
                "content": [
                    {"type": "text",
                     "text": "Please transcribe this receipt image and provide a tax analysis." + note},
                    {"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{encode_image_to_base64(content)}"}},
                ],
            },
        ]
        kind = 'vision'
        print(f"[LLM] Reading a photographed receipt with the vision model{' (with a note)' if note else ''}...")
    else:
        record = (content or '').strip()
        if not record:
            raise ValueError("There is no text to read.")
        record = record[:TEXT_RECORD_MAX_CHARS]
        messages = [
            {"role": "system", "content": TEXT_RECORD_SYSTEM_PROMPT},
            {
                "role": "user",
                "content": ("Record this purchase and provide a tax analysis. The text is "
                            f"reproduced exactly as it was received:\n\n{record}{note}"),
            },
        ]
        kind = 'text'
        print(f"[LLM] Reading a written purchase record ({len(record)} chars"
              f"{', with a note' if note else ''})...")

    extracted_data = _call_with_fallback(
        get_llm_client(config), config, kind, messages,
        tools=VISION_TOOLS, expected_name='save_extracted_receipt_data',
    )
    print(f"[LLM] Successfully parsed arguments: {json.dumps(extracted_data, indent=2)}")
    return extracted_data


def reconstructed_receipt_url(data):
    """
    The TRA verification URL rebuilt from what the model read off the paper, or None.

    This is the point of insisting on the verification code and the time in the vision
    prompt: with both, a photograph that no QR reader could decode still names a receipt
    on TRA's portal, and the pipeline can go and fetch the real numbers instead of
    keeping the ones a model read off a crumpled thermal print.

    Deliberately strict, and strict in the same way an admin correcting the same two
    fields by hand is: the construction itself belongs to utils.tra, and a transcription
    that will not build one is simply a transcription that stands on its own.
    """
    try:
        return build_receipt_url(
            data.get('receipt_verification_code'), data.get('receipt_time'),
        )
    except ValueError:
        return None
