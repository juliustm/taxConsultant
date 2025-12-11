import re

def clean_amount(value):
    """
    Cleans a string amount (e.g. '10,000.00', 'TZS 5000') into a float.
    Returns None if conversion fails.
    """
    if not value:
        return None
    if isinstance(value, (int, float)):
        return float(value)
    
    # Remove everything except digits and dots
    # This handles '10,000', 'TZS 1000', etc.
    # We assume standard decimal notation.
    try:
        # Remove commas
        cleaned = str(value).replace(',', '')
        # Extract the first valid number found (simple approach)
        # Or just strip non-numeric chars except dot
        cleaned = re.sub(r'[^\d.]', '', cleaned)
        return float(cleaned)
    except (ValueError, TypeError):
        return None

test_cases = [
    ("10,000.00", 10000.0),
    ("10000", 10000.0),
    ("TZS 5,000", 5000.0),
    ("1,234,567.89", 1234567.89),
    (100, 100.0),
    (None, None),
    ("", None),
    ("invalid", None) # Should probably be None or 0.0 depending on logic, my regex might return empty string which float() hates
]

print("Running tests...")
for input_val, expected in test_cases:
    try:
        result = clean_amount(input_val)
        if result == expected:
            print(f"PASS: {input_val} -> {result}")
        else:
            print(f"FAIL: {input_val} -> {result} (Expected: {expected})")
    except Exception as e:
        print(f"ERROR: {input_val} -> {e}")
