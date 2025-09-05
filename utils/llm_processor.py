# utils/llm_processor.py
import openai
import base64
import json

SYSTEM_PROMPT = """
You are an expert in Tanzanian tax compliance (Income Tax/VAT Acts). Analyze receipts using `save_extracted_receipt_data` and provide tax analysis meeting TRA audit standards.

### Mandatory Fields:
1. Supplier TIN 
2. VAT status (✅/❌)
3. Tax breakdown (VAT/WHT)
4. TRA invoice markers

### Tax Analysis Rules:
**VAT Compliance:**
- 18% VAT deductible only if supplier registered
- Flag unregistered vendors charging VAT

**WHT Requirements:**
- 5% WHT for resident individuals
- 10% WHT for corporations
- Apply on services >100k TZS/month

**Deductibility:**
- Business purpose test (Section 11 ITA)
- Valid tax invoice required
- Fuel: Commercial vehicles only

**Red Flags:**
⚠️ Missing TIN
⚠️ Unregistered vendors for taxable goods
⚠️ Services without WHT deduction

### Output Standards:
- Cite relevant laws (e.g. "VAT disallowed per Sec 17(2)")
- Separate compliance vs deductibility
- Specify when consultation needed
"""

TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "save_extracted_receipt_data",
            "description": "Saves the extracted key information from a receipt document.",
            "parameters": {
                "type": "object",
                "properties": {
                    "vendor_name": {"type": "string"},
                    "vendor_tin": {"type": "string"},
                    "vendor_phone": {"type": "string"},
                    "vrn": {"type": "string", "description": "The Vendor Registration Number (VRN)."},
                    "receipt_date": {"type": "string", "description": "YYYY-MM-DD format."},
                    "receipt_number": {"type": "string"},
                    "uin": {"type": "string"},
                    "total_amount": {"type": "number"},
                    "vat_amount": {"type": "number"},
                    "receipt_verification_code": {"type": "string"},
                    "customer_name": {"type": "string"},
                    "customer_id_type": {"type": "string"},
                    "customer_id": {"type": "string"},
                    "llm_extracted_description": {"type": "string", "description": "A concise, one-sentence summary of the purchase (e.g., 'Purchase of office supplies from ABC Ltd.')."},
                    "llm_tax_analysis": {"type": "string", "description": "Your expert analysis of potential tax obligations based on the items purchased, considering Tanzanian law. Mention if it's likely tax-deductible, any withholding tax implications, or other compliance notes. Keep it brief (1-2 sentences)."}
                },
                "required": ["vendor_name", "receipt_date", "total_amount", "llm_extracted_description", "llm_tax_analysis"]
            },
        },
    }
]

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

def extract_receipt_details(content, is_image, config):
    """
    Extracts details from receipt content using the tool-calling pattern, with a failsafe for Groq.
    """
    
    if not config.llm_api_key:
        raise ValueError("LLM API key is not configured.")

    client = get_llm_client(config)
    
    messages = [{"role": "system", "content": SYSTEM_PROMPT}]

    if is_image:
        base64_image = encode_image_to_base64(content)
        messages.append({
            "role": "user",
            "content": [
                {"type": "text", "text": "Please analyze this receipt image, extract its data, and provide a tax analysis."},
                {"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{base64_image}"}}
            ]
        })
        # Select the correct vision model for the provider.
        model = "meta-llama/llama-4-scout-17b-16e-instruct" if config.llm_provider == 'groq' else "gpt-4o"
    else:
        messages.append({
            "role": "user",
            "content": f"Please analyze this receipt text, extract its data, and provide a tax analysis:\n\n{content}"
        })
        model = "llama-3.3-70b-versatile" if config.llm_provider == 'groq' else "gpt-4o"

    try:
        print(f"[LLM] Calling model '{model}' with tool-calling enabled...")
        response = client.chat.completions.create(
            model=model,
            messages=messages,
            tools=TOOLS,
            tool_choice="auto"
        )
        response_message = response.choices[0].message
        tool_calls = response_message.tool_calls
        if not tool_calls:
            raise ValueError("LLM did not call the required tool to save data.")
        tool_call = tool_calls[0]
        if tool_call.function.name != 'save_extracted_receipt_data':
            raise ValueError(f"LLM called an unexpected tool: {tool_call.function.name}")
        print("[LLM] Model requested to call 'save_extracted_receipt_data'. Extracting arguments.")
        extracted_data = json.loads(tool_call.function.arguments)
        print(f"[LLM] Successfully parsed arguments: {json.dumps(extracted_data, indent=2)}")
        return extracted_data
    except Exception as e:
        print(f"[LLM Error] An error occurred during LLM call: {e}")
        raise
