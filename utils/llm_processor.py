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
"""
import openai
import base64
import json

# Fixed set, so categories can be grouped and reported on. A free-text category is a
# category that is spelled three different ways by the end of the quarter.
EXPENSE_CATEGORIES = [
    "fuel", "vehicle_running", "travel", "accommodation", "meals_entertainment",
    "utilities", "telecom", "rent", "office_supplies", "professional_services",
    "repairs_maintenance", "insurance", "bank_charges", "marketing",
    "inventory_purchases", "capital_asset", "staff_costs", "taxes_levies", "other",
]

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
VISION_SYSTEM_PROMPT = """
You are an expert in Tanzanian tax compliance (Income Tax Act / VAT Act) reading a
photographed receipt.

Transcribe what is printed on it using `save_extracted_receipt_data`, then give a
brief tax analysis. Transcribe only what you can actually see: leave a field out
rather than guessing at it, and never round or reconstruct an amount. Amounts are in
TZS.
"""

VISION_TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "save_extracted_receipt_data",
            "description": "Saves the fields transcribed from a photographed receipt.",
            "parameters": {
                "type": "object",
                "properties": {
                    "vendor_name": {"type": "string"},
                    "vendor_tin": {"type": "string"},
                    "vendor_phone": {"type": "string"},
                    "vrn": {"type": "string", "description": "The vendor's VAT Registration Number (VRN)."},
                    "receipt_date": {"type": "string", "description": "YYYY-MM-DD format."},
                    "receipt_time": {"type": "string", "description": "HH:MM:SS, 24 hour, if printed."},
                    "receipt_number": {"type": "string"},
                    "z_number": {"type": "string"},
                    "efd_serial": {"type": "string", "description": "The EFD machine serial number, if printed."},
                    "uin": {"type": "string"},
                    "total_amount": {"type": "number", "description": "Total including tax."},
                    "total_excl_tax": {"type": "number"},
                    "vat_amount": {"type": "number", "description": "Total tax charged."},
                    "receipt_verification_code": {"type": "string"},
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
                "required": ["vendor_name", "receipt_date", "total_amount", "llm_extracted_description", "llm_tax_analysis"],
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
    with open(image_path, "rb") as image_file:
        return base64.b64encode(image_file.read()).decode('utf-8')

def get_llm_client(config):
    if config.llm_provider == 'groq' and config.llm_api_key:
        print("[LLM] Initializing Groq client.")
        return openai.OpenAI(
            api_key=config.llm_api_key,
            base_url="https://api.groq.com/openai/v1"
        )
    print(f"[LLM] Initializing {config.llm_provider} client.")
    return openai.OpenAI(api_key=config.llm_api_key)

def _model_for(config, vision=False):
    if config.llm_provider == 'groq':
        return "meta-llama/llama-4-scout-17b-16e-instruct" if vision else "llama-3.3-70b-versatile"
    return "gpt-4o"

def _call_tool(client, model, messages, tools, expected_name):
    """Runs one tool-calling round trip and returns the parsed arguments."""
    response = client.chat.completions.create(
        model=model,
        messages=messages,
        tools=tools,
        tool_choice="auto",
    )
    tool_calls = response.choices[0].message.tool_calls
    if not tool_calls:
        raise ValueError("LLM did not call the required tool to save data.")

    tool_call = tool_calls[0]
    if tool_call.function.name != expected_name:
        raise ValueError(f"LLM called an unexpected tool: {tool_call.function.name}")

    return json.loads(tool_call.function.arguments)

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
    if user_note:
        prompt += f"\n\nThe person who submitted it noted: {user_note}"

    messages = [
        {"role": "system", "content": JUDGMENT_SYSTEM_PROMPT},
        {"role": "user", "content": prompt},
    ]

    model = _model_for(config)
    try:
        print(f"[LLM] Requesting judgment from '{model}' ({len(prompt)} chars of facts).")
        judgment = _call_tool(client=get_llm_client(config), model=model, messages=messages,
                              tools=JUDGMENT_TOOLS, expected_name='save_receipt_judgment')
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

def extract_receipt_details(content, is_image, config):
    """
    Reads a photographed receipt with the vision model.

    Only for photos. Receipts submitted as a TRA URL are parsed from the verified page
    and never go through here.
    """
    if not config or not config.llm_api_key:
        raise ValueError("LLM API key is not configured.")

    if not is_image:
        raise ValueError(
            "Text receipts are parsed from the TRA verified page, not by the LLM. "
            "See utils/tra_parser.parse_receipt_html."
        )

    messages = [
        {"role": "system", "content": VISION_SYSTEM_PROMPT},
        {
            "role": "user",
            "content": [
                {"type": "text", "text": "Please transcribe this receipt image and provide a tax analysis."},
                {"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{encode_image_to_base64(content)}"}},
            ],
        },
    ]

    model = _model_for(config, vision=True)
    print(f"[LLM] Calling vision model '{model}' for a photographed receipt...")
    extracted_data = _call_tool(client=get_llm_client(config), model=model, messages=messages,
                                tools=VISION_TOOLS, expected_name='save_extracted_receipt_data')
    print(f"[LLM] Successfully parsed arguments: {json.dumps(extracted_data, indent=2)}")
    return extracted_data
