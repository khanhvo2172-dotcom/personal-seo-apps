# Personal SEO Apps

A Streamlit web app with personal SEO utilities:

- Keyword grouping from top Google results via Serper
- Google Drive image downloads
- Google Docs link checking
- Google Docs image extraction and optimization
- Cloudinary-based WebP optimization

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
