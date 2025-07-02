# TaxConsult AI Agent ✨🇹🇿

[![License: AGPL v3](https://img.shields.io/badge/License-AGPL_v3-blue.svg)](https://www.gnu.org/licenses/agpl-3.0)
[![Commercial License](https://img.shields.io/badge/License-Commercial-green.svg)](LICENSE-COMMERCIAL.md)

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
-   **AI-Powered Data Extraction**: Utilizes LLMs (Groq, OpenAI) to accurately read and interpret receipt data, including vision support for images.
-   **Intelligent Text Cleaning**: Automatically cleans messy HTML from TRA verification portals before sending it to the AI, saving costs and improving accuracy.
-   **Multiple Export Destinations**:
    -   **Google Sheets**: Automatically logs all submissions and processed data into a monthly-tabbed spreadsheet.
    -   **Webhook**: Sends real-time event notifications (`queued`, `processed`, `failed`, `duplicate`) to any URL you provide.
    -   **Amazon S3**: Backs up all event data as JSON files for auditing and safekeeping.
-   **Real-time Admin Dashboard**: A clean, passwordless (TOTP-based) web UI for configuration and live monitoring of the submission queue.
-   **Asynchronous Job Queue**: Uses a robust, database-backed queue to process submissions in the background, perfect for single-app hosting environments like [Deploy.tz](https://deploy.tz/).
-   **Self-Hostable & Private**: You control your data. Host your own instance and ensure your financial information remains confidential.

## How It Works: The "In-App Cron" Architecture

To ensure compatibility with simple, single-app hosting platforms (like Deploy.tz) that don't support external services like Redis, this agent uses a clever database-backed queue.

1.  **Intake**: The `/receipt` endpoint is lightweight. It validates the request, saves the submission to the database with a `queued` status, and immediately responds.
2.  **Queue**: The SQLite database itself acts as the job queue.
3.  **Processing**: A secret endpoint, `/tasks/run`, acts as the "worker." When triggered, it processes all jobs in the queue.
4.  **Trigger**: A dual-trigger system provides both immediate feedback and robust reliability:
    -   **Instant Trigger**: A non-blocking background task is spawned on receipt submission to call the `/tasks/run` endpoint after a 10-second delay.
    -   **Cron Job (Fallback)**: You use a free external service like [cron-job.org](https://cron-job.org/) to call the `/tasks/run` endpoint on a schedule (e.g., every 5 minutes). This ensures any failed or missed jobs are always processed.

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
# Create a new file named .env. For now, you don't need to change anything in it.
touch .env
```

**`.env` content:**
```env
# A secret key for signing Flask sessions.
SECRET_KEY='a-super-secret-key-for-local-development'
# A secret key to protect the task runner endpoint.
TASK_RUNNER_SECRET_KEY='local-cron-job-secret-12345'
```

```bash
# 3. Build and run the application with Docker Compose
docker-compose up --build
```

Your application should now be running! You can access it at `http://localhost:80`.

### 2. Initial Administrator Setup

The first time you visit the application, you'll be guided through a secure setup process using any standard authenticator app (Google Authenticator, Authy, etc.).

## Configuration Guide

After logging in, navigate to the **Configuration** page to set up the LLM provider and your desired data export destinations (Google Sheets, Webhook, S3). Detailed instructions are provided on the page itself.

---

## License & Usage

This project is dual-licensed to balance community collaboration with sustainable development.

### 1. AGPLv3 License (for Open-Source Use)

This project is open-source under the **GNU Affero General Public License v3.0 (AGPLv3)**.

**In simple terms, this means:**
-   ✅ **You are free to use, modify, and distribute** this software for personal or internal business use.
-   ✅ **You can** self-host it on your own servers to manage your company's receipts.
-   ⚠️ **You must** also open-source any modifications you make if you provide this software as a service to others over a network.

The AGPLv3 is a "strong copyleft" license designed to ensure that derivatives of open-source software remain open-source. This is the perfect license to prevent the project from being taken and offered as a closed-source SaaS product without contributing back to the community.

You can find the full license text in the `LICENSE-AGPLv3.txt` file.

### 2. Commercial License (for Business Use without AGPLv3 Obligations)

If you wish to use this software in a commercial product, SaaS offering, or any other scenario where you do not want to be bound by the source-sharing terms of the AGPLv3, you must purchase a commercial license.

**A commercial license grants you the right to:**
-   Integrate this software into your proprietary, closed-source applications.
-   Offer a service based on this software without being required to release your own source code.
-   Receive priority support and influence the project's roadmap.

This dual-license model ensures that individual users and the open-source community can benefit freely, while businesses that profit from this work contribute back to its sustainable development.

**To inquire about a commercial license, please contact:**

**Julius Moshiro T/A Atana Ventures**
- **Email:** [julius@atana.co.tz](mailto:julius@atana.co.tz)
- **Website:** [atana.co.tz](https://atana.co.tz)

---

## Contributing

We welcome contributions from the community! Whether it's a bug fix, a new feature, or documentation improvements, your help is greatly appreciated.

If you have a suggestion, please fork the repo and create a pull request, or open an issue with the tag "enhancement". Please note that all contributions will be licensed under the AGPLv3.

1.  Fork the Project
2.  Create your Feature Branch (`git checkout -b feature/AmazingFeature`)
3.  Commit your Changes (`git commit -m 'Add some AmazingFeature'`)
4.  Push to the Branch (`git push origin feature/AmazingFeature`)
5.  Open a Pull Request
