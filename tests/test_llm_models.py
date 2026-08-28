# tests/test_llm_models.py
"""
Choosing a model, and surviving one being switched off.

This exists because of a real outage with no bug in it: Groq decommissioned
`meta-llama/llama-4-scout-17b-16e-instruct`, and every photographed receipt began
failing with a 404 on a perfectly valid API key and unchanged code. A hosted model is
retired on somebody else's schedule, so the only defence is to notice and move on.
"""
import types

import pytest

from utils import llm_processor
from utils.llm_processor import NoUsableModel


class FakeConfig:
    def __init__(self, provider='groq', **models):
        self.llm_provider = provider
        self.llm_api_key = 'test-key'
        self.llm_text_model = models.get('text')
        self.llm_vision_model = models.get('vision')


def fake_client(answers, catalogue=None):
    """
    A client that answers per model id.

    `answers` maps a model id to either an exception to raise or the JSON string the
    tool call should carry back. Anything not named raises the provider's own
    'this model is gone' error, which is the interesting case.
    """
    asked = []
    # The whole request, not just the model id: what a model is *sent* is the other
    # half of whether it answers, and the reason qwen was reachable and still useless.
    sent = []

    def create(model, messages, tools, tool_choice, extra_body=None):
        asked.append(model)
        sent.append({'model': model, 'tool_choice': tool_choice, 'extra_body': extra_body})
        answer = answers.get(model)
        if answer is None:
            raise Exception(
                f"Error code: 404 - {{'error': {{'message': 'The model `{model}` does not "
                "exist or you do not have access to it.', 'code': 'model_not_found'}}}}"
            )
        if isinstance(answer, Exception):
            raise answer

        call = types.SimpleNamespace(
            function=types.SimpleNamespace(name='save_it', arguments=answer))
        return types.SimpleNamespace(
            choices=[types.SimpleNamespace(message=types.SimpleNamespace(tool_calls=[call]))])

    client = types.SimpleNamespace(
        chat=types.SimpleNamespace(completions=types.SimpleNamespace(create=create)),
        models=types.SimpleNamespace(list=lambda: types.SimpleNamespace(
            data=[types.SimpleNamespace(id=model_id) for model_id in (catalogue or [])])),
        asked=asked,
        sent=sent,
    )
    return client


@pytest.fixture(autouse=True)
def forget_proven_models():
    """The remembered model is process-wide state; no test may inherit another's."""
    llm_processor._PROVEN_MODEL.clear()
    yield
    llm_processor._PROVEN_MODEL.clear()


def call(client, config, kind='vision'):
    return llm_processor._call_with_fallback(
        client, config, kind, messages=[], tools=[], expected_name='save_it')


@pytest.fixture
def ladder(monkeypatch):
    """
    A two-model ladder of invented names, for the tests about walking down one.

    Invented rather than borrowed from MODEL_CANDIDATES because these tests are about
    the mechanism and the real list is about the market: Groq is currently down to a
    single model that can see, and a test that reads the first two entries of it starts
    failing on the day a provider retires something - which is the one day this file
    needs to still run.
    """
    names = ['first-model', 'second-model']
    monkeypatch.setitem(llm_processor.MODEL_CANDIDATES, ('groq', 'vision'), names)
    return names


def test_a_retired_model_is_skipped_for_the_next_candidate(ladder):
    """The outage this whole mechanism exists for."""
    working = ladder[-1]
    client = fake_client({working: '{"ok": true}'})

    assert call(client, FakeConfig()) == {'ok': True}
    # Everything ahead of it was tried and found gone, in order.
    assert client.asked[-1] == working
    assert len(client.asked) > 1


def test_the_model_that_worked_is_used_first_next_time(ladder):
    """One wasted round trip per restart, not one per receipt."""
    working = ladder[-1]
    config = FakeConfig()

    first = fake_client({working: '{"ok": true}'})
    call(first, config)

    second = fake_client({working: '{"ok": true}'})
    call(second, config)
    assert second.asked == [working]


def test_a_pinned_model_is_asked_before_anything_else():
    """What an admin typed on the configure page outranks our guesses."""
    client = fake_client({'some-new-model': '{"ok": true}'})

    assert call(client, FakeConfig(vision='some-new-model')) == {'ok': True}
    assert client.asked == ['some-new-model']


def test_an_environment_variable_can_pin_a_model_when_the_dashboard_cannot(monkeypatch):
    monkeypatch.setenv('LLM_VISION_MODEL', 'model-from-the-environment')
    client = fake_client({'model-from-the-environment': '{"ok": true}'})

    assert call(client, FakeConfig()) == {'ok': True}
    assert client.asked == ['model-from-the-environment']


def test_the_providers_catalogue_is_consulted_once_every_known_name_is_gone():
    """
    The self-healing case: a provider renames its vision models and every constant in
    this repo is wrong at once. Asking what it actually serves beats waiting for a
    release.
    """
    client = fake_client(
        {'meta-llama/llama-5-vision': '{"ok": true}'},
        catalogue=['whisper-large-v3', 'meta-llama/llama-5-vision', 'llama-guard-4'],
    )

    assert call(client, FakeConfig()) == {'ok': True}
    assert client.asked[-1] == 'meta-llama/llama-5-vision'
    # Speech and moderation models are in the catalogue and must never be tried.
    assert 'whisper-large-v3' not in client.asked
    assert 'llama-guard-4' not in client.asked


def test_a_catalogue_pick_that_turns_out_to_be_text_only_is_stepped_over():
    """
    Nothing in a model catalogue says which models can see, so the name is a guess.
    Being able to survive a wrong guess is what lets the guess be a generous one.
    """
    client = fake_client(
        {
            'gemma-2-9b-it': Exception('400 - this model does not support image input'),
            'gemma-3-27b-it': '{"ok": true}',
        },
        catalogue=['gemma-2-9b-it', 'gemma-3-27b-it'],
    )

    assert call(client, FakeConfig()) == {'ok': True}
    assert client.asked[-1] == 'gemma-3-27b-it'


def test_a_text_only_complaint_does_not_derail_the_analysis_job():
    """
    The same words from the text model mean something else - it is not being sent an
    image - so they must not send us wandering down the candidate list.
    """
    first = llm_processor.MODEL_CANDIDATES[('groq', 'text')][0]
    client = fake_client({first: Exception('400 - unsupported content in message')})

    with pytest.raises(Exception, match='unsupported content'):
        call(client, FakeConfig(), kind='text')
    assert client.asked == [first]


def test_nothing_left_to_try_says_so_and_names_what_it_tried():
    client = fake_client({}, catalogue=[])

    with pytest.raises(NoUsableModel) as raised:
        call(client, FakeConfig())

    assert 'groq' in str(raised.value)
    assert llm_processor.MODEL_CANDIDATES[('groq', 'vision')][0] in str(raised.value)


def test_an_ordinary_failure_is_not_treated_as_a_missing_model():
    """
    A rate limit or a timeout would be answered identically by every other model, so
    walking the candidate list would just spend the quota it is already out of.
    """
    first = llm_processor.MODEL_CANDIDATES[('groq', 'vision')][0]
    client = fake_client({first: Exception('429 rate_limit_exceeded')})

    with pytest.raises(Exception, match='rate_limit_exceeded'):
        call(client, FakeConfig())

    assert client.asked == [first]


@pytest.mark.parametrize('message', [
    "The model `x` does not exist or you do not have access to it.",
    "model_not_found",
    "This model has been decommissioned. Please use another model.",
    "Your API key does not have access to this model.",
])
def test_the_providers_ways_of_saying_the_model_is_gone(message):
    assert llm_processor._is_missing_model(Exception(message))


@pytest.mark.parametrize('message', [
    '429 rate_limit_exceeded',
    'Connection error.',
    'context_length_exceeded',
])
def test_other_failures_are_not_mistaken_for_it(message):
    assert not llm_processor._is_missing_model(Exception(message))


# --- A tool call the model could not finish ----------------------------------
#
# The other way a photographed receipt is lost, and the one that leaves no trace of a
# bug: the model is there, it can see, it starts filling the tool in and stops partway,
# and the provider answers 400 with the half-written call attached. A fuel receipt with
# a long item list did exactly this. Nothing about it is permanent, so nothing about it
# should end the submission on the first try.

GARBLED = Exception(
    "Error code: 400 - {'error': {'message': \"Failed to call a function. Please adjust "
    "your prompt. See 'failed_generation' for more details.\", 'type': "
    "'invalid_request_error', 'code': 'tool_use_failed', 'failed_generation': "
    "'<tool_call>\\n<function=save_it>\\n<parameter=receipt_number>'}}"
)


class Flaky:
    """Raises the given error for the first `times` calls, then answers properly."""

    def __init__(self, error, times=1, answer='{"ok": true}'):
        self.error, self.times, self.answer = error, times, answer
        self.calls = 0

    def __call__(self):
        self.calls += 1
        if self.calls <= self.times:
            raise self.error
        return self.answer


def flaky_client(behaviours, catalogue=None):
    """fake_client, but a model's answer may depend on how often it has been asked."""
    client = fake_client({}, catalogue=catalogue)
    original = client.chat.completions.create

    def create(model, messages, tools, tool_choice, extra_body=None):
        behaviour = behaviours.get(model)
        if behaviour is None:
            return original(model=model, messages=messages, tools=tools,
                            tool_choice=tool_choice)
        client.asked.append(model)
        answer = behaviour() if callable(behaviour) else behaviour
        call_ = types.SimpleNamespace(
            function=types.SimpleNamespace(name='save_it', arguments=answer))
        return types.SimpleNamespace(
            choices=[types.SimpleNamespace(message=types.SimpleNamespace(tool_calls=[call_]))])

    client.chat.completions.create = create
    return client


def test_a_mangled_tool_call_is_simply_asked_again():
    """
    The cheap fix, and the right one: it is a sampling accident, not a broken model.
    """
    first = llm_processor.MODEL_CANDIDATES[('groq', 'vision')][0]
    client = flaky_client({first: Flaky(GARBLED, times=1)})

    assert call(client, FakeConfig()) == {'ok': True}
    assert client.asked == [first, first]


def test_a_model_that_mangles_it_twice_hands_the_receipt_to_the_next_one(ladder):
    """Two failures is enough to suspect the pairing of this model and this photo."""
    first, second = ladder
    client = flaky_client({first: Flaky(GARBLED, times=99), second: '{"ok": true}'})

    assert call(client, FakeConfig()) == {'ok': True}
    assert client.asked == [first, first, second]


def test_a_model_that_mangled_one_receipt_is_not_crossed_off_for_the_next():
    """
    It exists and it works, so a stumble on one document must not cost it its place.

    The distinction from a retired model, which is where this could quietly go wrong:
    that one is forgotten as the proven model on the way past, because it is gone and
    every receipt after it would pay a 404 to rediscover that. This one is neither
    forgotten nor skipped - it answers the retry and stays the model we reach for.
    """
    first = llm_processor.MODEL_CANDIDATES[('groq', 'vision')][0]
    config = FakeConfig()

    call(flaky_client({first: Flaky(GARBLED, times=1)}), config)
    assert llm_processor._PROVEN_MODEL[('groq', 'vision')] == first

    # The next receipt goes straight to it, with no rediscovery.
    again = flaky_client({first: '{"ok": true}'})
    assert call(again, config) == {'ok': True}
    assert again.asked == [first]


def test_nothing_could_finish_the_call_is_reported_as_this_document_not_this_instance():
    """
    Every model answered, none of them usably. Sending someone to the configure page to
    fix a provider that is working is worse than saying what actually happened.
    """
    from utils.llm_processor import LlmUnavailable

    client = flaky_client(
        {model: Flaky(GARBLED, times=99)
         for model in llm_processor.MODEL_CANDIDATES[('groq', 'vision')]},
        catalogue=[])

    with pytest.raises(LlmUnavailable) as raised:
        call(client, FakeConfig())

    assert 'retried' in str(raised.value)


@pytest.mark.parametrize('message', [
    "Failed to call a function. Please adjust your prompt.",
    "'code': 'tool_use_failed'",
    "see 'failed_generation' for more details",
])
def test_the_ways_a_provider_reports_an_unusable_tool_call(message):
    assert llm_processor._is_malformed_tool_call(Exception(message))


# --- A model that answers without calling the tool ---------------------------
#
# The other half of the same failure, and the one the user actually saw: a 200, no
# 'tool_use_failed' anywhere, just a model that wrote about the receipt instead of
# filling the form in. It used to end the submission with "LLM did not call the
# required tool to save data" on the first try.

TOOLS = [{
    "type": "function",
    "function": {
        "name": "save_it",
        "parameters": {
            "type": "object",
            "properties": {},
            "required": ["vendor_name", "total_amount"],
        },
    },
}]


def talking_client(replies):
    """A client whose models reply with message content and no tool call."""
    asked = []

    def create(model, messages, tools, tool_choice, extra_body=None):
        asked.append((model, tool_choice))
        return types.SimpleNamespace(choices=[types.SimpleNamespace(
            message=types.SimpleNamespace(tool_calls=None, content=replies.get(model, '')))])

    return types.SimpleNamespace(
        chat=types.SimpleNamespace(completions=types.SimpleNamespace(create=create)),
        models=types.SimpleNamespace(list=lambda: types.SimpleNamespace(data=[])),
        asked=asked,
    )


def test_the_tool_is_named_in_the_request_rather_than_merely_offered():
    """
    Both jobs here are structured extraction; a prose answer is never the right one, so
    'auto' was inviting the failure this fixes.
    """
    first = llm_processor.MODEL_CANDIDATES[('groq', 'vision')][0]
    client = fake_client({first: '{"ok": true}'})
    captured = []

    original = client.chat.completions.create
    client.chat.completions.create = lambda **kwargs: (
        captured.append(kwargs['tool_choice']) or original(**kwargs))

    llm_processor._call_with_fallback(
        client, FakeConfig(), 'vision', messages=[], tools=[], expected_name='save_it')
    assert captured == [{'type': 'function', 'function': {'name': 'save_it'}}]


def test_a_reasoning_model_is_told_to_keep_its_thinking_out_of_the_answer():
    """
    The second half of the outage, and the half that is not about names at all.

    Groq's remaining vision model is a hybrid reasoning model, and there
    `reasoning_format` must be 'parsed' or 'hidden' whenever tools are in the request.
    Left at its default the thinking shares a channel with the answer, the tool call
    comes back unparseable, and this code reads that as a model that cannot fill the
    form in - so it walks the whole ladder and reports that no model is available while
    a perfectly good one sits at the top of it answering every time.
    """
    client = fake_client({'qwen/qwen3.6-27b': '{"ok": true}'})

    assert call(client, FakeConfig(vision='qwen/qwen3.6-27b')) == {'ok': True}
    assert client.sent[0]['extra_body'] == {
        'reasoning_format': 'hidden', 'reasoning_effort': 'none', 'temperature': 0,
    }


def test_a_transcription_is_not_sampled_for_variety():
    """
    A receipt is transcribed, not written. The verification code has to come back
    character for character or it names a different receipt on the portal.
    """
    client = fake_client({'some-model': '{"ok": true}'})

    call(client, FakeConfig(vision='some-model'))
    assert client.sent[0]['extra_body'] == {'temperature': 0}

    # The judgment job is reasoning about a receipt whose facts are already settled,
    # and is left at the provider's own default.
    client = fake_client({'some-model': '{"ok": true}'})
    call(client, FakeConfig(text='some-model'), kind='text')
    assert client.sent[0]['extra_body'] is None


def test_a_provider_that_will_not_take_the_reasoning_parameters_is_asked_without_them():
    """
    The parameters above are chosen from a fragment of a model id, so sooner or later
    they will be sent to something that has never heard of them. One more round trip is
    the right price for that; losing the receipt is not.
    """
    asked = []

    def create(model, messages, tools, tool_choice, extra_body=None):
        asked.append(extra_body)
        if extra_body and 'reasoning_format' in extra_body:
            raise Exception("400 - unrecognized request argument supplied: reasoning_format")
        call_ = types.SimpleNamespace(
            function=types.SimpleNamespace(name='save_it', arguments='{"ok": true}'))
        return types.SimpleNamespace(choices=[types.SimpleNamespace(
            message=types.SimpleNamespace(tool_calls=[call_]))])

    client = types.SimpleNamespace(
        chat=types.SimpleNamespace(completions=types.SimpleNamespace(create=create)),
        models=types.SimpleNamespace(list=lambda: types.SimpleNamespace(data=[])))

    assert call(client, FakeConfig(vision='qwen-on-some-other-host')) == {'ok': True}
    # Only the parameter it named. The temperature was never in dispute and a receipt
    # transcribed at the provider's default is the bug this would quietly reintroduce.
    assert len(asked) == 2
    assert asked[1] == {'reasoning_effort': 'none', 'temperature': 0}


def test_a_provider_that_will_not_be_told_which_tool_to_call_is_asked_loosely():
    """Not every OpenAI-compatible endpoint implements the named form."""
    asked = []

    def create(model, messages, tools, tool_choice, extra_body=None):
        asked.append(tool_choice)
        if tool_choice != 'auto':
            raise Exception('400 - tool_choice of type function is not supported')
        call = types.SimpleNamespace(
            function=types.SimpleNamespace(name='save_it', arguments='{"ok": true}'))
        return types.SimpleNamespace(choices=[types.SimpleNamespace(
            message=types.SimpleNamespace(tool_calls=[call]))])

    client = types.SimpleNamespace(
        chat=types.SimpleNamespace(completions=types.SimpleNamespace(create=create)),
        models=types.SimpleNamespace(list=lambda: types.SimpleNamespace(data=[])))

    assert llm_processor._call_with_fallback(
        client, FakeConfig(vision='some-model'), 'vision',
        messages=[], tools=[], expected_name='save_it') == {'ok': True}
    assert asked[-1] == 'auto'


def test_a_receipt_written_out_as_json_instead_of_called_is_read_anyway():
    """
    The model did the work and got the envelope wrong. Throwing that away to ask again
    costs a second generation to be told the same thing.
    """
    first = llm_processor.MODEL_CANDIDATES[('groq', 'vision')][0]
    client = talking_client({first: 'Here is what I read:\n```json\n'
                                    '{"vendor_name": "PLASCO", "total_amount": 118000}\n```'})

    assert llm_processor._call_with_fallback(
        client, FakeConfig(), 'vision', messages=[], tools=TOOLS,
        expected_name='save_it') == {'vendor_name': 'PLASCO', 'total_amount': 118000}
    # One round trip, not two: nothing about this needed asking again.
    assert len(client.asked) == 1


def test_the_llama_families_own_broken_tool_syntax_is_understood():
    """
    What Groq attaches to a 'tool_use_failed' as failed_generation, and what these
    models emit in the message body when the same slip does not trip the validator.
    """
    first = llm_processor.MODEL_CANDIDATES[('groq', 'vision')][0]
    client = talking_client({first: (
        '<function=save_it>\n'
        '<parameter=vendor_name>\nPLASCO LIMITED\n</parameter>\n'
        '<parameter=total_amount>\n118000\n</parameter>\n'
        '<parameter=items>\n[{"description": "Sheeting"}]\n</parameter>\n'
    )})

    assert llm_processor._call_with_fallback(
        client, FakeConfig(), 'vision', messages=[], tools=TOOLS, expected_name='save_it') == {
            'vendor_name': 'PLASCO LIMITED',
            'total_amount': 118000,
            'items': [{'description': 'Sheeting'}],
        }


def test_half_a_receipt_is_not_salvaged(ladder):
    """
    The safety on the whole salvage. A generation that ran out of room stops mid-field,
    and what is left parses perfectly well into a receipt missing its verification code
    and half its total - which is worse than the failure it would be replacing, because
    it gets stored. Anything short of complete goes back to the model.
    """
    first, second = ladder
    client = talking_client({
        first: '<function=save_it>\n<parameter=vendor_name>\nPLASCO\n</parameter>\n'
               '<parameter=total_amount>',
        second: '{"vendor_name": "PLASCO", "total_amount": 118000}',
    })

    assert llm_processor._call_with_fallback(
        client, FakeConfig(), 'vision', messages=[], tools=TOOLS,
        expected_name='save_it') == {'vendor_name': 'PLASCO', 'total_amount': 118000}
    # Twice on the first model, then handed on - the ordinary garbled-call path.
    assert [model for model, _ in client.asked] == [first, first, second]


def test_a_model_that_only_talks_is_reported_as_the_document_not_the_instance():
    first = llm_processor.MODEL_CANDIDATES[('groq', 'vision')][0]
    client = talking_client({first: 'I am not able to read this receipt clearly.'})

    with pytest.raises(llm_processor.LlmUnavailable) as raised:
        llm_processor._call_with_fallback(
            client, FakeConfig(vision=first), 'vision', messages=[], tools=TOOLS,
            expected_name='save_it')
    assert 'retried' in str(raised.value)


@pytest.mark.parametrize('message', [
    '429 rate_limit_exceeded',
    'The model `x` does not exist or you do not have access to it.',
    'context_length_exceeded',
])
def test_an_unusable_tool_call_is_not_confused_with_the_other_failures(message):
    assert not llm_processor._is_malformed_tool_call(Exception(message))


# --- A receipt read perfectly and rejected on a type -------------------------
#
# Submission 19, and the reason "these keep failing": qwen read an AT CLIX MOTORCYCLES
# receipt without a single mistake - every digit of the TIN, the verification code, the
# item line - wrote the call out longhand, and Groq refused to hand it over because
# `114605836` is a number and the schema calls vendor_tin a string. Nothing about that
# is random, so the retry produced the identical rejection, and the submission failed
# with the whole receipt sitting inside the error the entire time.

FAILED_GENERATION = (
    '<tool_call>\n<function=save_extracted_receipt_data>\n'
    '<parameter=vendor_name>\nAT CLIX MOTORCYCLES CO. LTD\n</parameter>\n'
    '<parameter=vendor_tin>\n114605836\n</parameter>\n'
    '<parameter=vendor_phone>\n07144629795\n</parameter>\n'
    '<parameter=receipt_date>\n2026-08-01\n</parameter>\n'
    '<parameter=receipt_time>\n09:54:33\n</parameter>\n'
    '<parameter=receipt_number>\n250\n</parameter>\n'
    '<parameter=receipt_verification_code>\n66FF047250\n</parameter>\n'
    '<parameter=total_amount>\n170000.0\n</parameter>\n'
    '<parameter=total_excl_tax>\n170000.0\n</parameter>\n'
    '<parameter=vat_amount>\n0.0\n</parameter>\n'
    '<parameter=document_type>\ntra_efd_receipt\n</parameter>\n'
    '<parameter=efd_serial>\n03T2443013629\n</parameter>\n'
    '<parameter=uin>\n-10126414911460583603T2443013629\n</parameter>\n'
    '<parameter=z_number>\n01181M\n</parameter>\n'
    '<parameter=items>\n[{"description": "SPARE PARTS", "amount": 170000.0, '
    '"quantity": 1, "tax_code": "A"}]\n</parameter>\n'
    '<parameter=llm_extracted_description>\nPurchase of motorcycle spare parts.\n</parameter>\n'
    '<parameter=llm_tax_analysis>\nThe receipt is a valid TRA EFD receipt.\n</parameter>\n'
    '<parameter=is_cancelled>\nFalse\n</parameter>\n'
    '</function>\n</tool_call>'
)

TYPE_REJECTION = (
    "Error code: 400 - {'error': {'message': 'tool call validation failed: parameters "
    "for tool save_extracted_receipt_data did not match schema: errors: "
    "[`/is_cancelled`: expected boolean, but got string, `/receipt_number`: expected "
    "string, but got number, `/vendor_tin`: expected string, but got number]', 'type': "
    "'invalid_request_error', 'code': 'tool_use_failed', 'failed_generation': "
    + repr(FAILED_GENERATION) + "}}"
)


def rejecting_client(error):
    """A provider that refuses the model's generation, for every model asked."""
    asked = []

    def create(model, messages, tools, tool_choice, extra_body=None):
        asked.append(model)
        raise error

    return types.SimpleNamespace(
        chat=types.SimpleNamespace(completions=types.SimpleNamespace(create=create)),
        models=types.SimpleNamespace(list=lambda: types.SimpleNamespace(data=[])),
        asked=asked,
    )


def salvage(client):
    return llm_processor._call_with_fallback(
        client, FakeConfig(vision='the-model'), 'vision', messages=[],
        tools=llm_processor.VISION_TOOLS,
        expected_name='save_extracted_receipt_data')


def test_a_receipt_the_provider_rejected_on_a_type_is_read_out_of_the_error():
    """The submission 19 failure, end to end, against the real tool schema."""
    client = rejecting_client(Exception(TYPE_REJECTION))
    data = salvage(client)

    assert data['vendor_name'] == 'AT CLIX MOTORCYCLES CO. LTD'
    assert data['receipt_verification_code'] == '66FF047250'
    assert data['receipt_time'] == '09:54:33'
    assert data['total_amount'] == 170000.0
    assert data['items'] == [{'description': 'SPARE PARTS', 'amount': 170000.0,
                              'quantity': 1, 'tax_code': 'A'}]
    # Read out of the error rather than asked for again: the model was not being
    # random, so a second identical generation would have bought nothing.
    assert client.asked == ['the-model']


def test_the_three_fields_the_provider_named_arrive_as_the_schema_declares_them():
    data = salvage(rejecting_client(Exception(TYPE_REJECTION)))

    # Text, not integers - a TIN that reaches the database as a number is a different
    # vendor the moment one has a leading zero.
    assert data['vendor_tin'] == '114605836'
    assert data['receipt_number'] == '250'
    # The dangerous one. `False` is not valid JSON, so it survives as a five-character
    # string, and every non-empty string is true: this is a live receipt being filed
    # as cancelled.
    assert data['is_cancelled'] is False


def test_a_verification_code_of_pure_digits_survives_the_salvage():
    """
    The failure this would have had next. Nothing makes a verification code unreadable
    as a number, and one that gets read as one loses its leading zero on the way to
    TRA's portal - which is a different receipt, or no receipt.
    """
    generation = FAILED_GENERATION.replace('66FF047250', '0660472501')
    data = salvage(rejecting_client(Exception(
        TYPE_REJECTION.replace(repr(FAILED_GENERATION), repr(generation)))))

    assert data['receipt_verification_code'] == '0660472501'
    assert data['z_number'] == '01181M'
    assert data['uin'] == '-10126414911460583603T2443013629'


def test_the_attached_generation_is_read_off_the_exception_when_the_sdk_provides_it():
    """The SDK's own exceptions carry the parsed body; not every path here stringifies."""
    error = Exception('Error code: 400 - tool_use_failed')
    error.body = {'error': {'code': 'tool_use_failed',
                            'failed_generation': FAILED_GENERATION}}

    assert salvage(rejecting_client(error))['vendor_tin'] == '114605836'


def test_a_generation_that_ran_out_of_room_is_still_not_salvaged():
    """
    The safety has to survive the new path. A 400 whose attached generation stops
    mid-receipt must go back to the model, not into the database - reading it would
    store a receipt with no verification code and a plausible total.
    """
    truncated = FAILED_GENERATION.split('<parameter=receipt_verification_code>')[0]
    error = Exception('Error code: 400 - tool_use_failed')
    error.body = {'error': {'failed_generation': truncated}}
    client = rejecting_client(error)

    with pytest.raises(llm_processor.LlmUnavailable):
        salvage(client)
    # Asked twice, salvaged neither time, then handed on to the next candidate - the
    # old behaviour, intact for the case where it is still the right one.
    assert client.asked[:2] == ['the-model', 'the-model']


def test_an_error_with_nothing_attached_is_left_alone():
    assert llm_processor._failed_generation(Exception('429 rate_limit_exceeded')) is None
    assert llm_processor._failed_generation(Exception("Error code: 400 - {'x': 1}")) is None


def test_the_salvaged_receipt_still_names_itself_on_tras_portal():
    """
    The whole point of the salvage, end to end.

    A rescued transcription is not the goal - a verified receipt is. Submission 19's
    generation carried both halves of the portal address, so once it is read out of the
    error the pipeline can go and fetch TRA's own figures and throw the transcription
    away. That is the difference between a receipt the model believes and a receipt the
    revenue authority confirms, and it was being lost to a type mismatch.
    """
    data = salvage(rejecting_client(Exception(TYPE_REJECTION)))

    assert llm_processor.reconstructed_receipt_url(data) == (
        'https://verify.tra.go.tz/66FF047250_095433')


# --- The note the sender typed ----------------------------------------------

def test_the_senders_note_is_labelled_rather_than_run_into_the_document():
    """
    The note is context, and the model has to be able to tell it from the paper.

    Run together with the receipt it is transcribable text, and a model that reads
    'about 40,000 of diesel' out of somebody's aside has invented an amount nothing was
    printed with. Labelled, with the instruction attached, it can still decide the
    category - which is the whole reason it is sent.
    """
    block = llm_processor._note_block('Diesel for the site generator')

    assert block.startswith('\n\n')
    assert 'do not transcribe anything out of it' in block
    assert block.rstrip().endswith('Diesel for the site generator')


def test_no_note_adds_nothing_at_all():
    """An absent note is not an empty heading; nothing is appended."""
    for empty in (None, '', '   ', '\n\t'):
        assert llm_processor._note_block(empty) == ''


def test_a_note_is_bounded_like_every_other_thing_a_phone_can_paste():
    """
    The box accepts a paste of any size, and every retry pays for it again.

    Whitespace is squashed on the way in too: a note pasted out of a chat arrives with
    the line breaks of wherever it was written, and those buy nothing here.
    """
    block = llm_processor._note_block('word  \n  word ' * 400)

    assert 'word word' in block
    assert len(block) < 1200
