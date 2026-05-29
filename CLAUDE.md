# Personal SEO Apps

## Project

- **App**: https://personal-seo-apps.streamlit.app/
- **Repo**: https://github.com/khanhvo2172-dotcom/personal-seo-apps/
- **Stack**: Python, Streamlit, Google APIs (Docs/Drive/Sheets)
- **Deploy**: Streamlit Cloud (auto-deploy on push to main)

## File Structure

```
app.py                              # Entry point. Sidebar nav, global CSS, feature registry
features/
  __init__.py                       # Empty
  auth.py                           # Google OAuth (credentials, token, scopes)
  settings.py                       # Settings page (API keys, Google auth, Cloudinary)
  check_links.py                    # Check Internal & External Links in Google Docs
  extract_optimize_images.py        # Extract & Optimize Images from Google Docs
  download_gdrive_images.py         # Download Images using GDrive Links
  humanizer.py                      # Humanizer - AI detection
  keyword_grouping.py               # Keyword Grouping (uses SERP API)
  autofill_column.py                # Autofill Column (Google Sheets)
  remove_empty_rows.py              # Remove Empty Rows in Google Docs
.streamlit/config.toml              # Streamlit config
requirements.txt                    # Python dependencies
runtime.txt                         # Python version
Procfile                            # Railway deploy config
.env.example                        # Example env vars
```

## Feature Registry (app.py)

Each feature module exports `render()`. Registered in `FEATURES` dict in app.py.
Sidebar renders buttons for each feature. Selected feature stored in `st.session_state.selected_feature`.

## Patterns

- Each feature file: single `render()` entry point, private helpers prefixed `_`
- Auth: `require_auth()` guard at top of render() for features needing Google API
- Private values: `_private_value(key)` checks env vars then `st.secrets`
- Tables: `_render_selectable_table(df, key, label, copy_column=None)` — shared table renderer
- Filtering: `_render_filter()` + `_filter_dataframe()` for searchable tables
- Status codes: `_check_status_codes()` with ThreadPoolExecutor
- Google Docs API: `googleapiclient.discovery.build("docs", "v1", credentials=creds)`

## API Keys (stored in Streamlit Secrets or .env)

- GOOGLE_CLIENT_SECRET_JSON / GOOGLE_TOKEN_JSON — Google OAuth
- SERP_API_KEY — serper.dev for keyword grouping
- CLOUDINARY_CLOUD_NAME / CLOUDINARY_API_KEY / CLOUDINARY_API_SECRET — image optimization
- DEEPSEEK_API_KEY — DeepSeek AI suggestions
- ANTHROPIC_API_KEY — Claude AI suggestions
