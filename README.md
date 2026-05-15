# Personal SEO Apps

A Streamlit web app with personal SEO utilities:

- Keyword grouping from top Google results via Serper
- Google Drive image downloads
- Google Docs link checking
- Google Docs image extraction and optimization
- Cloudinary-based WebP optimization
- Excel / Google Sheets column autofill

## Autofill Column

Open the **Autofill Column** tab to fill blank cells in one column from the last value above it.
Autofill only continues while the selected limit column has data; when the limit column is blank,
the carry-forward value resets.

For Excel or CSV:

1. Upload a `.xlsx`, `.xls`, or `.csv` file.
2. Select the column to autofill.
3. Select the limit column.
4. Click **Autofill Column**.
5. Download the new file. The app adds a new `<column>_autofilled` column and keeps the original column unchanged.

For Google Sheets:

1. Authenticate with Google in **Settings**.
2. Open **Autofill Column** and choose **Google Sheets**.
3. Paste the Google Sheets URL or spreadsheet ID.
4. Enter the sheet tab name, then load the sheet.
5. Select the columns and click **Autofill Column**.
6. Click **Write autofilled column to Google Sheet** to add or update the `<column>_autofilled` column.

If Google Sheets auth fails after this update, sign out in **Settings** and authenticate again so the app can request Sheets permission.

## Local Run

Install dependencies:

```bash
pip install -r requirements.txt
```

Run the app:

```bash
streamlit run app.py
```

## Required Environment Variables

Copy `.env.example` to `.env` for local use and fill in the values you need:

```text
SERP_API_KEY=
CLOUDINARY_CLOUD_NAME=
CLOUDINARY_API_KEY=
CLOUDINARY_API_SECRET=
GOOGLE_CLIENT_SECRET_PATH=
```

For Railway deployment, add these as private Railway variables:

```text
SERP_API_KEY
CLOUDINARY_CLOUD_NAME
CLOUDINARY_API_KEY
CLOUDINARY_API_SECRET
GOOGLE_TOKEN_JSON
```

`GOOGLE_TOKEN_JSON` should be the full contents of your local `token.json`.

For Streamlit Community Cloud, open your app dashboard, go to **Settings -> Secrets**, and add:

```toml
SERP_API_KEY = "your_serper_key"
CLOUDINARY_CLOUD_NAME = "your_cloudinary_cloud_name"
CLOUDINARY_API_KEY = "your_cloudinary_api_key"
CLOUDINARY_API_SECRET = "your_cloudinary_api_secret"
GOOGLE_TOKEN_JSON = '''paste_the_full_token_json_here'''
DEEPSEEK_API_KEY = "your_deepseek_key"
```

Do not paste your Windows path (`C:\...`) into the deployed app. Streamlit Cloud runs on a remote Linux server and cannot read files from your computer.

## Deploy

This is a Streamlit app, so the recommended first deployment is Railway. The included `Procfile` starts the app with:

```bash
streamlit run app.py --server.address=0.0.0.0 --server.port=$PORT --server.headless=true
```

See `README_DEPLOY_RAILWAY.md` for the step-by-step Railway setup.

## Security Notes

Do not commit private credentials. The repo ignores:

- `.env`
- `token.json`
- Google OAuth client secret JSON files
- Streamlit secrets
- Notebook files

Use hosting provider environment variables for production secrets.
