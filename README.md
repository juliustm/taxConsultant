# TaxConsult AI Agent ✨🇹🇿

[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](https://opensource.org/licenses/MIT)

An open-source, self-hostable AI agent designed to help Tanzanian businesses automate and simplify expense reporting by intelligently processing EFD (Electronic Fiscal Device) receipts.

## The Problem

For many businesses in Tanzania, managing and recording expenses is a tedious, manual process. Collecting physical EFD receipts, manually entering data into spreadsheets, and ensuring compliance for audits is time-consuming and prone to errors. This administrative burden takes valuable time away from focusing on growing the business.

## The Solution

**TaxConsult AI Agent** transforms this process. By deploying your own private instance of this agent, you create a central hub that can:
1.  **Receive** receipts from various sources (WhatsApp, Telegram, Web App, etc.) via a secure API.
2.  **Intelligently Process** the receipt content—whether it's a photo or a URL from a QR code—using a powerful Large Language Model (LLM).
3.  **Extract Key Data** such as Vendor Name, TIN, VRN, Date, Total Amount, VAT, and more.
4.  **Automate Logging** by exporting this structured data directly to your preferred destination, like a Google Sheet, an S3 bucket, or a custom webhook.

This project empowers you to build your own internal, automated accounting assistant.

## Key Features

-   **Multi-Channel Input**: Submit receipts via URL or direct image upload through a secure API endpoint.
-   **AI-Powered Data Extraction**: Utilizes LLMs (Groq, OpenAI) to accurately read and interpret receipt data.
-   **Intelligent Text Cleaning**: Automatically cleans and simplifies messy HTML from TRA verification portals before sending it to the AI, saving costs and improving accuracy.
-   **Multiple Export Destinations**:
    -   **Google Sheets**: Automatically logs all submissions and processed data into a monthly-tabbed spreadsheet.
    -   **Webhook**: Sends real-time event notifications (`queued`, `processed`) to any URL you provide.
    -   **Amazon S3**: Backs up all event data as JSON files for auditing and safekeeping.
-   **Secure Admin Dashboard**: A clean, passwordless (TOTP-based) web UI for configuration and monitoring.
-   **Asynchronous Job Queue**: Uses a robust, database-backed queue to process submissions in the background without blocking, perfect for single-app hosting environments like [Deploy.tz](https://deploy.tz/).
-   **Receipt Deduplication**: Automatically detects and flags duplicate submissions based on the receipt verification code.
-   **Self-Hostable & Private**: You control your data. Host your own instance and ensure your financial information remains confidential.

## How It Works: The "In-App Cron" Architecture

To ensure compatibility with simple, single-app hosting platforms (like Deploy.tz) that don't support external services like Redis, this agent uses a clever database-backed queue.

1.  **Intake**: The `/receipt` endpoint is lightweight. It validates the request, saves the submission to the database with a `queued` status, and immediately responds.
2.  **Queue**: The SQLite database itself acts as the job queue.
3.  **Processing**: A secret endpoint, `/tasks/run`, acts as the "worker." When triggered, it processes all jobs in the queue.
4.  **Trigger**: You use a free external service like [cron-job.org](https://cron-job.org/) to automatically call the `/tasks/run` endpoint on a schedule (e.g., every 5 minutes), kicking off the processing.

## Getting Started

### Prerequisites

-   Git
-   Docker and Docker Compose

### 1. Local Development Setup

First, let's get the agent running on your local machine.

```bash
# 1. Clone the repository
git clone https://github.com/your-username/taxconsult-ai-agent.git
cd taxconsult-ai-agent

# 2. Create your local environment file
# Create a new file named .env and paste the content from the sample below.
# (On Linux/macOS, you can use: cp .env.sample .env)
# For now, you don't need to change anything in it.
touch .env
```

**Sample `.env` content:**
```env
# A secret key for signing Flask sessions. The default is fine for local use.
SECRET_KEY='a-super-secret-key-for-local-development'

# A secret key to protect the task runner endpoint.
TASK_RUNNER_SECRET_KEY='local-cron-job-secret-12345'
```

```bash
# 3. Build and run the application with Docker Compose
docker-compose up --build
```

Your application should now be running! You can access it at `http://localhost:5000`.

### 2. Initial Administrator Setup

The first time you visit the application, you'll be guided through a secure setup process.

1.  **Navigate** to `http://localhost:5000`.
2.  **Enter Your Email**: Provide the email address you want to use as the root administrator.
3.  **Scan the QR Code**: You will be shown a QR code. Use an authenticator app (like Google Authenticator, Authy, or Microsoft Authenticator) to scan it.
4.  **Verify**: Enter the 6-digit code from your authenticator app to complete the setup.
5.  **Log In**: You will be redirected to the login page. Enter your admin email and the new code from your authenticator app to log in.

## Configuration Guide

After logging in, navigate to the **Configuration** page. Here's how to set up each integration:

### A. LLM Provider (Required)

This is the AI brain of the agent.

1.  **LLM Provider**: Select `groq` (recommended for speed and cost) or `openai`.
2.  **API Key**:
    -   For **Groq**, get your key from the [Groq Console](https://console.groq.com/keys).
    -   For **OpenAI**, get your key from the [OpenAI Platform](https://platform.openai.com/api-keys).
3.  Paste the key into the "API Key" field.

### B. Google Sheets Export (Recommended)

This is the best way to get a clean, browsable log of your expenses.

1.  **Create a Service Account:**
    -   Go to the [Google Cloud Console Service Accounts page](https://console.cloud.google.com/iam-admin/serviceaccounts).
    -   Select or create a project.
    -   Click **"+ CREATE SERVICE ACCOUNT"**. Name it something like `taxconsult-sheet-writer`.
    -   Click "CREATE AND CONTINUE", then grant it the role of **Editor**.
    -   Click "Done". Find your new service account in the list.
    -   Click the three dots under "Actions" -> "Manage keys" -> "ADD KEY" -> "Create new key".
    -   Choose **JSON** and click "CREATE". A `credentials.json` file will be downloaded.

2.  **Share Your Google Sheet:**
    -   Open the downloaded `credentials.json` file. Copy the `client_email` value (e.g., `...gserviceaccount.com`).
    -   Create a new Google Sheet.
    -   Click the **Share** button. Paste the service account's email and give it **Editor** access.

3.  **Configure in the App:**
    -   **Google Sheet ID**: Copy the long ID from your sheet's URL (`.../d/THIS_IS_THE_ID/edit...`) and paste it here.
    -   **Google Service Account JSON**: Open the `credentials.json` file, copy its **entire content**, and paste it into this large text box.
    -   Click **Save Configuration**.

### C. Webhook Callback Export

If you want to trigger another service (like a custom notification system), use this.

-   **POST Callback URL**: Enter the URL where you want the agent to send a `POST` request with the event data (`submission.queued`, `submission.processed`).

### D. Amazon S3 Bucket Export

For a robust, long-term backup of all submission data.

-   Enter your **S3 Bucket Name**, **Region**, **Access Key ID**, and **Secret Access Key**.

## Usage

### Submitting Receipts

Interact with your agent by sending a `POST` request to the `/receipt` endpoint. You must include your device's API Key as a Bearer Token. You can find your initial API key on the **Configuration** page.

**Example using cURL:**
```bash
# Get your API Key from the dashboard's Configuration page
API_KEY="your-api-key-from-the-dashboard"

# Submit a receipt URL
curl -X POST \
  -H "Authorization: Bearer $API_KEY" \
  -F "receipturl=https://verify.tra.go.tz/A89E231255_145327" \
  -F "description=Lunch meeting with client" \
  http://localhost:5000/receipt

# Or, submit a photo of a receipt
curl -X POST \
  -H "Authorization: Bearer $API_KEY" \
  -F "receiptphoto=@/path/to/your/receipt.jpg" \
  http://localhost:5000/receipt
```

### Processing the Queue (The Cron Job)

Your submissions are now waiting in the queue. To process them, you need to trigger the task runner.

-   **Locally**: You can do this manually by visiting the **Queue** page in the admin dashboard and clicking "Process Queue Now".
-   **In Production (on Deploy.tz, etc.)**:
    1.  Set up a free cron job service like [cron-job.org](https://cron-job.org/).
    2.  Create a new job that sends a `GET` request to your secret runner URL every 5 minutes.
    3.  The URL will be: `https://your-app-name.deploy.tz/tasks/run?secret=YOUR_TASK_RUNNER_SECRET_KEY`
        -   Make sure to set the `TASK_RUNNER_SECRET_KEY` in your production environment variables!

## Contributing

Contributions are what make the open-source community such an amazing place to learn, inspire, and create. Any contributions you make are **greatly appreciated**.

If you have a suggestion that would make this better, please fork the repo and create a pull request. You can also simply open an issue with the tag "enhancement".

1.  Fork the Project
2.  Create your Feature Branch (`git checkout -b feature/AmazingFeature`)
3.  Commit your Changes (`git commit -m 'Add some AmazingFeature'`)
4.  Push to the Branch (`git push origin feature/AmazingFeature`)
5.  Open a Pull Request

## License

Distributed under the MIT License. See `LICENSE` for more information.