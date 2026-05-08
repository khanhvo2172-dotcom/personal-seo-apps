# Deploy Personal Tools to Railway

This app is a Streamlit app. It does not need Vercel unless you later rewrite the UI as a separate React frontend. For the current app, deploy the whole project to Railway.

## What is ready

- `Procfile` tells Railway how to start Streamlit.
- `runtime.txt` pins Python to 3.12.
- `.gitignore` keeps `.env`, `token.json`, and Google client secret files out of Git.
- `features/auth.py` can read Google credentials from Railway variables.

## Railway steps

1. Create a GitHub repository for this folder and push the code.
2. Go to Railway, create a new project, and choose "Deploy from GitHub repo".
3. Select this repository.
4. In Railway Variables, add:

```text
SERP_API_KEY
CLOUDINARY_CLOUD_NAME
CLOUDINARY_API_KEY
CLOUDINARY_API_SECRET
GOOGLE_TOKEN_JSON
```

For `GOOGLE_TOKEN_JSON`, paste the full contents of your local `token.json` as one value. Railway variables are private; do not put this file in GitHub.

5. Deploy. Railway should run:

```text
streamlit run app.py --server.address=0.0.0.0 --server.port=$PORT --server.headless=true
```

6. Open the public Railway URL.

## Vercel note

Vercel is best for a separate frontend. This project currently has no separate frontend folder or API backend. Using Vercel plus Railway would require rebuilding the app into two apps:

- Vercel: React/Next.js interface
- Railway: Python API

That is a larger rewrite, not a normal deploy.
