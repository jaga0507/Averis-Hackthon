TitanPort

Master → Extraction, Classification and Comparison of SI and BL → Draft Kirtanan → Chatbot

TitanPort is a system for email/document processing, extraction, classification, and comparison of Shipping Instructions (SI) and Bills of Lading (BL).

1. Prerequisites

Install the following software before starting.

Git

Download Git: https://git-scm.com/

Verify:

git --version

PHP

TitanPort currently uses PHP for the API.

Verify:

php --version

PHP 8.x is recommended.

Composer

Composer manages the PHP dependencies.

Verify:

composer --version

If Composer is not installed: https://getcomposer.org/

Python

TitanPort uses Python for document processing.

Verify:

python --version

Python 3.10+ is recommended.

Supabase

TitanPort uses Supabase for:

PostgreSQL

File storage

Create an account at: https://supabase.com/

2. Download TitanPort

Clone the repository:

git clone <TITANPORT_REPOSITORY_URL>

Enter the project:

cd titanport

The project should look approximately like:

titanport/
├── api/
├── database/
├── frontend/
├── scripts/
├── sidecar/
├── .env
└── README.md

3. Configure Supabase

TitanPort requires a Supabase project.

Create a new project from the Supabase dashboard.

After the project has been created, you will need:

Project URL

Service Role Key

These values are available from the Supabase project settings.

4. Create the TitanPort Database

TitanPort requires its database tables before the application can run correctly.

The database schema is located in:

database/

Run the supplied SQL/migrations against the new Supabase database.

The exact database setup should be performed before starting the API.

After the schema is installed, the database should contain TitanPort tables for things such as:

cases

emails

documents

extracted_fields

activities

review_tasks

decisions

audit

The exact schema is defined by the files in database/.

Do not manually recreate the tables if the repository already contains the database schema.

5. Create Supabase Storage

TitanPort also stores email attachments.

Create a Supabase Storage bucket.

For example:

documents

The bucket name must match the value configured in the TitanPort environment.

6. Configure Environment Variables

Create the environment file expected by TitanPort.

Example:

SUPABASE_URL=https://YOUR_PROJECT.supabase.co
SUPABASE_SERVICE_KEY=YOUR_SUPABASE_SERVICE_ROLE_KEY
SUPABASE_STORAGE_BUCKET=attachments
SIDECAR_URL=http://127.0.0.1:8001

Replace the placeholder values with the actual Supabase credentials.

Important Security Rule

Never expose:

SUPABASE_SERVICE_KEY

to the browser.

Never put the service-role key inside:

frontend/

and never commit it to Git.

The service-role key is a server-side secret.

7. Install PHP Dependencies

Open a terminal in:

cd titanport/api

Run:

composer install

This installs the PHP dependencies into:

api/vendor/

8. Install Python Dependencies

Open a terminal in:

cd titanport/sidecar

Create a virtual environment:

python -m venv .venv

Windows

.venv\Scripts\activate

macOS/Linux

source .venv/bin/activate

Then install the Python dependencies:

pip install -r requirements.txt

If the repository does not yet contain requirements.txt, install the dependencies specified by sidecar/app.py and create the requirements file for the project.

9. Start the Python Processing Service

Open a terminal:

cd titanport/sidecar

Activate the virtual environment if necessary.

Then:

python app.py

The Python processor should start on:

http://127.0.0.1:8001/

Keep this terminal running.

10. Start the PHP API

Open a second terminal.

Run:

cd titanport/api
php -S localhost:8000 -t public

The API should now be available at:

http://localhost:8000/

Keep this terminal running.

11. Test the PHP API

Open:

http://localhost:8000/health

The API should return a successful health response.

Then test the database:

http://localhost:8000/health/db

The database health check should also succeed.

Finally, test the processing service:

http://localhost:8000/processing/health

This confirms that PHP can communicate with the Python processor.

12. Start the Frontend

Open a third terminal.

Run:

cd titanport/frontend
python -m http.server 5500

Open:

http://127.0.0.1:5500/

The frontend should now be available.

13. TitanPort Pages

Dashboard

http://127.0.0.1:5500/index.html

The dashboard displays case statistics and recent cases.

Case List

http://127.0.0.1:5500/cases.html

This displays the available TitanPort cases.

Case Detail

A case detail URL contains the case UUID:

http://127.0.0.1:5500/case-detail.html?id=CASE_UUID

Example:

http://127.0.0.1:5500/case-detail.html?id=f257fb18-2f76-5770-a982-ad9cc6047e22

Comparison Profile

The comparison page also uses the case UUID:

http://127.0.0.1:5500/comparison.html?id=CASE_UUID

Example:

http://127.0.0.1:5500/comparison.html?id=f257fb18-2f76-5770-a982-ad9cc6047e22

14. How a Case Is Processed

A normal case follows this general flow:

Email
  │
  ▼
Email Classification
  │
  ▼
Document Identification
  ├── Missing document
  │
  ▼
Document Extraction
  ├── Extraction problem
  │
  ▼
SI / BL Comparison
  ├── Values match
  │     │
  │     ▼
  │   Continue
  │
  └── Values differ

15. Testing a Matching Case

A matching test case should contain SI and BL with matching values.

For example:

SI gross weight: 21,577 KG
BL gross weight: 21,577 KG

The comparison should return:

{
  "field": "gross_weight_kg",
  "is_match": true
}

16. Testing a Mismatch Case

To test the comparison functionality, both documents must exist.

Example:

SI

SHIPPER: ABC PAPER LTD
CONSIGNEE: XYZ TRADING
PORT OF LOADING: PORT KLANG
PORT OF DISCHARGE: JEBEL ALI
CONTAINER COUNT: 2
GROSS WEIGHT: 21,577 KG

BL

SHIPPER: ABC PAPER LTD
CONSIGNEE: XYZ TRADING
PORT OF LOADING: PORT KLANG
PORT OF DISCHARGE: JEBEL ALI
CONTAINER COUNT: 2
GROSS WEIGHT: 20,500 KG

The expected comparison is:

{
  "field": "gross_weight_kg",
  "si_raw": "21,577 KG",
  "bl_raw": "20,500 KG",
  "is_match": false
}

The comparison page should display the two different values.

17. Testing a Missing Document Case

A missing-document case contains an email without the required SI or BL attachment.

For example:

Email
└── no attachment

The processing system should detect:

missing_attachment

and create a human-review workflow.

The case may appear with a status such as:

AWAITING_DOCUMENT

The API may return:

{
  "documents": [],
  "comparison": null
}

This is expected.

A case with no documents cannot have an SI/BL comparison.

18. Understanding comparison: null

This is important when debugging the UI.

If the API returns:

"comparison": null

it does not necessarily mean the API is broken.

It may mean that comparison was never possible.

For example:

No attachment
      ↓
No SI / BL
      ↓
No extraction
      ↓
No comparison
      ↓
comparison = null

The frontend should display the appropriate workflow state instead of treating this as a server failure.

19. Finding a Case by ID

The API endpoint is:

GET /cases/{caseId}

For example:

http://localhost:8000/cases/f257fb18-2f76-5770-a982-ad9cc6047e22

The response contains:

Case information

Documents

Comparison

Extracted fields

Activities

Review task

Decisions

Audit information

20. API Endpoints

Current endpoints include:

Method

Endpoint

GET

/health

GET

/health/db

GET

/processing/health

POST

/processing/email

GET

/cases

GET

/cases/{caseId}

POST

/processing/case/{caseId}

21. Troubleshooting

Frontend says API failed

Check that the PHP server is running:

php -S localhost:8000 -t public

Then open:

http://localhost:8000/health

Processing service unavailable

Check that Python is running:

python app.py

Then verify:

http://localhost:8001/

and:

http://localhost:8000/processing/health

Database health check fails

Check:

SUPABASE_URL=...
SUPABASE_SERVICE_KEY=...

Make sure the Supabase project exists and is accessible.

Case has no comparison

Check the case API:

GET /cases/{caseId}

Look at:

"documents"

and:

"comparison"

If:

"documents": []

then there are no documents to compare.

If:

"comparison": null

check the activities and review task for the reason comparison was blocked.

Comparison shows only matching fields

Check:

"comparison": {
  "items": []
}

A mismatch should still be represented as an item:

{
  "field": "gross_weight_kg",
  "si_raw": "21,577 KG",
  "bl_raw": "20,500 KG",
  "is_match": false
}

A mismatch should not simply disappear from the API response.

22. Local Development Summary

A developer normally needs three terminals.

Terminal 1 — Python

cd titanport/sidecar
python app.py

Terminal 2 — PHP

cd titanport/api
php -S localhost:8000 -t public

Terminal 3 — Frontend

cd titanport/frontend
python -m http.server 5500

Then open:

http://127.0.0.1:5500/index.html

23. Production Deployment

TitanPort consists of three logical services:

Frontend / API

Database / Storage

Python Processing Service

A production deployment should therefore provide:

Vercel
│
├── Frontend
└── API
    │
    ├──────────────► Supabase PostgreSQL
    │
    ├──────────────► Supabase Storage
    │
    └──────────────► Python Processing Service

The production environment must not use:

localhost:8000
localhost:8001
127.0.0.1

Those addresses only refer to the local developer's machine.

Production frontend/API URLs should be configured through environment variables or the production API routing configuration.

24. Production Secrets

The following are sensitive:

SUPABASE_SERVICE_KEY

They must only exist on trusted server-side infrastructure.

Do not put them in:

Frontend JavaScript

HTML

Public files

Git

Browser local storage

If a secret is accidentally committed to Git, rotate the secret immediately.

25. First-Time Setup Checklist

Before considering TitanPort successfully installed, verify:

Git installed

PHP installed

Composer installed

Python installed

Supabase project created

Database schema installed

Storage bucket created

Environment variables configured

PHP dependencies installed

Python dependencies installed

Python sidecar starts

PHP API starts

/health works

/health/db works

/processing/health works

Frontend starts

Case list loads

Case detail loads

Comparison page loads

Matching case tested

Mismatch case tested

Missing-document case tested

26. Quick Start

For an experienced developer who already has Supabase configured:

Terminal 1

cd titanport/sidecar
python app.py

Terminal 2

cd titanport/api
php -S localhost:8000 -t public

Terminal 3

cd titanport/frontend
python -m http.server 5500

Open:

http://127.0.0.1:5500/index.html

27. Important Note About Deployment

The local development environment and production environment are different.

Local

Browser
  ↓
localhost:5500
  ↓
localhost:8000
  ↓
localhost:8001
  ↓
Supabase

Production

Browser
  ↓
Vercel
  ↓
Production API
  ├── Supabase
  └── Python Processing Service

Do not assume that starting the three local servers is equivalent to deploying TitanPort.

Before production deployment, configure the production API routing and Python processing service separately.

28. Support / Debug Information

When reporting a problem, provide:

Operating system

PHP version

Python version

Error from the browser console

PHP API response

Python sidecar output

Case ID being tested

Result of GET /health

Result of GET /health/db

Result of GET /processing/health

Do Not Provide

Do not provide:

SUPABASE_SERVICE_KEY

or any other secret credentials.
