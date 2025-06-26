# utils/export.py
import json
import re
import gspread
from oauth2client.service_account import ServiceAccountCredentials
import boto3
import requests
from botocore.exceptions import NoCredentialsError, ClientError
from datetime import datetime
import traceback

# Expanded headers for comprehensive logging
SHEET_HEADERS = [
    "Submission ID", "Status", "Received At", "Processed At", "Device", "Input Type", 
    "User Description", "LLM Description", "Location", "Vendor Name", "Vendor TIN", "Vendor Phone", 
    "VRN", "Receipt No.", "UIN", "Receipt Date", "Verification Code", "Total Amount", 
    "VAT Amount", "Customer Name", "Customer ID Type", "Customer ID", "Tax Analysis", "Error"
]

def dispatch_event(event_type: str, payload: dict, config):
    """
    Dispatches an event to all configured export destinations.
    """
    print(f"--- Dispatching event: {event_type} ---")
    
    if config.post_callback_url:
        send_webhook(event_type, payload, config.post_callback_url)

    if all([config.s3_bucket_name, config.s3_access_key_id, config.s3_secret_access_key, config.s3_region]):
        log_to_s3(event_type, payload, config)

    if config.google_sheet_id and config.google_service_account_json:
        log_to_gsheet(event_type, payload, config)
        
def _get_gspread_client(service_account_json: str):
    """Authorizes gspread using service account JSON content."""
    try:
        creds_dict = json.loads(service_account_json)
        scope = ['https://spreadsheets.google.com/feeds', 'https://www.googleapis.com/auth/drive']
        creds = ServiceAccountCredentials.from_json_keyfile_dict(creds_dict, scope)
        client = gspread.authorize(creds)
        return client
    except Exception as e:
        print(f"[GSheet Error] Failed to authorize gspread client: {e}")
        return None

def log_to_gsheet(event_type, payload, config):
    client = _get_gspread_client(config.google_service_account_json)
    if not client: return

    try:
        spreadsheet = client.open_by_key(config.google_sheet_id)
        sheet_name = datetime.utcnow().strftime('%B-%Y')
        
        try:
            worksheet = spreadsheet.worksheet(sheet_name)
        except gspread.exceptions.WorksheetNotFound:
            print(f"[GSheet] Worksheet '{sheet_name}' not found. Creating it.")
            worksheet = spreadsheet.add_worksheet(title=sheet_name, rows="1000", cols="30")
            worksheet.append_row(SHEET_HEADERS)
            worksheet.format(f'A1:{gspread.utils.rowcol_to_a1(1, len(SHEET_HEADERS))}', {'textFormat': {'bold': True}})

        if event_type == 'submission.queued':
            # Create the initial record with all available data
            row_data = [
                payload.get('id'), payload.get('status'), payload.get('received_at'),
                None, payload.get('device_name'), payload.get('input_type'),
                payload.get('description'), None, payload.get('location')
            ]
            worksheet.append_row(row_data)
            print(f"[GSheet] Appended 'queued' data for submission {payload.get('id')}.")

        elif event_type == 'submission.processed':
            data = payload.get('data', {})
            cell = worksheet.find(str(payload.get('submission_id')))
            if cell:
                # Construct the full list of data for the entire row
                full_row_data = [
                    payload.get('submission_id'), payload.get('status'), worksheet.cell(cell.row, 3).value, # Keep original received_at
                    payload.get('processed_at'), worksheet.cell(cell.row, 5).value, worksheet.cell(cell.row, 6).value,
                    worksheet.cell(cell.row, 7).value, data.get('llm_extracted_description'), worksheet.cell(cell.row, 9).value,
                    data.get('vendor_name'), data.get('vendor_tin'), data.get('vendor_phone'),
                    data.get('vrn'), data.get('receipt_number'), data.get('uin'),
                    data.get('receipt_date'), data.get('receipt_verification_code'), data.get('total_amount'),
                    data.get('vat_amount'), data.get('customer_name'), data.get('customer_id_type'),
                    data.get('customer_id'), data.get('llm_tax_analysis'), None # No error
                ]
                # Update the entire row in one API call
                worksheet.update(f'A{cell.row}:{gspread.utils.rowcol_to_a1(cell.row, len(full_row_data))}', [full_row_data])
                print(f"[GSheet] Updated row {cell.row} with 'processed' data for submission {payload.get('submission_id')}.")

    except Exception as e:
        print(f"[GSheet Error] Failed to write to Google Sheet: {e}")

def send_webhook(event_type, payload, url):
    headers = {'Content-Type': 'application/json'}
    data = {'event_type': event_type, 'payload': payload}
    try:
        response = requests.post(url, headers=headers, data=json.dumps(data, default=str), timeout=10)
        response.raise_for_status()
        print(f"Webhook to {url} sent successfully.")
    except requests.exceptions.RequestException as e:
        print(f"Error sending webhook to {url}: {e}")

def log_to_s3(event_type, payload, config):
    session = boto3.Session(
        aws_access_key_id=config.s3_access_key_id,
        aws_secret_access_key=config.s3_secret_access_key,
        region_name=config.s3_region
    )
    s3 = session.client('s3')
    
    timestamp = payload.get('received_at') or payload.get('processed_at')
    submission_id = payload.get('submission_id') or payload.get('id')
    object_key = f"{event_type}/{str(timestamp).split('T')[0]}/{submission_id}.json"
    
    try:
        s3.put_object(
            Bucket=config.s3_bucket_name,
            Key=object_key,
            Body=json.dumps(payload, default=str, indent=2),
            ContentType='application/json'
        )
        print(f"Successfully logged event to S3 bucket {config.s3_bucket_name} at {object_key}")
    except (NoCredentialsError, ClientError) as e:
        print(f"Error logging to S3: {e}")




