import json
import os
import streamlit as st
from pathlib import Path
from dotenv import load_dotenv, set_key

ENV_PATH = Path(__file__).parent.parent / ".env"


def _save(key: str, value: str):
    ENV_PATH.touch()
    set_key(str(ENV_PATH), key, value)
    os.environ[key] = value


def _is_cloud() -> bool:
    return bool(os.getenv("STREAMLIT_RUNTIME_ENV") or os.getenv("HOSTNAME"))


def _get_private_value(key: str) -> str:
    value = os.getenv(key, "").strip()
    if value:
        return value
    try:
        return str(st.secrets.get(key, "")).strip()
    except Exception:
        return ""


def _get_client_config() -> dict | None:
    """Load OAuth client config from GOOGLE_CLIENT_SECRET_JSON secret."""
    raw = _get_private_value("GOOGLE_CLIENT_SECRET_JSON")
    if not raw:
        return None
    try:
        return json.loads(raw)
    except Exception:
        return None


def _get_redirect_uri_from_config(client_config: dict | None = None) -> str | None:
    """Extract the first redirect_uri from the OAuth client config JSON."""
    if not client_config:
        client_config = _get_client_config()
    if not client_config:
        return None
    for key in ("web", "installed"):
        if key in client_config:
            uris = client_config[key].get("redirect_uris", [])
            if uris:
                return uris[0]
    return None


def _get_redirect_uri(client_config: dict | None = None) -> str:
    """Return the OAuth redirect URI.

    Prefers the URI from the client secret JSON (guaranteed to match Google
    Cloud Console).  Falls back to auto-detection from request headers.
    """
    from_config = _get_redirect_uri_from_config(client_config)
    if from_config:
        return from_config
    try:
        host = dict(st.context.headers).get("host", "localhost:8501")
        proto = "https" if "." in host and not host.startswith("localhost") else "http"
        return f"{proto}://{host}/"
    except Exception:
        return "http://localhost:8501/"


def _handle_oauth_callback() -> bool:
    """Exchange the ?code= from Google redirect for a token. Returns True if a callback was found."""
    code = st.query_params.get("code")
    if not code:
        return False

    from google_auth_oauthlib.flow import Flow
    from features.auth import SCOPES, TOKEN_PATH

    client_config = _get_client_config()
    if not client_config:
        st.error("Cannot complete sign-in: `GOOGLE_CLIENT_SECRET_JSON` not found in Secrets.")
        st.query_params.clear()
        return True

    try:
        flow = Flow.from_client_config(client_config, scopes=SCOPES, redirect_uri=_get_redirect_uri())
        flow.fetch_token(code=code)
        creds = flow.credentials
        st.session_state.google_creds = creds
        st.session_state.google_signed_out = False
        st.session_state.oauth_new_token = creds.to_json()
        try:
            TOKEN_PATH.write_text(creds.to_json())
        except Exception:
            pass
    except Exception as e:
        st.session_state.oauth_error = str(e)

    st.query_params.clear()
    return True


def render():
    _handle_oauth_callback()

    st.header("Settings")
    load_dotenv(str(ENV_PATH), override=True)
    _render_quick_guide()

    # ── Google Authentication ─────────────────────────────────
    st.subheader("🔑 Google Authentication")
    st.info(
        "Required for: **Check Links in GDocs**, **Extract & Optimize Images**, "
        "and **Autofill Column** when using Google Sheets."
    )

    # Show result from web OAuth callback
    if st.session_state.get("oauth_error"):
        st.error(f"Sign-in failed: {st.session_state.pop('oauth_error')}")

    if st.session_state.get("oauth_new_token"):
        st.success("✅ Authenticated successfully!")
        st.info(
            "**To persist after the app restarts**, copy the token below and paste it into "
            "**Streamlit Cloud → App Settings → Secrets** as `GOOGLE_TOKEN_JSON`, then reboot the app:"
        )
        st.code(st.session_state.oauth_new_token, language="json")
        if st.button("Dismiss", type="primary"):
            del st.session_state.oauth_new_token
            st.rerun()
        st.divider()

    col_path, col_status = st.columns([3, 1])
    with col_path:
        secret_path = st.text_input(
            "OAuth 2.0 Client Secret JSON path",
            value=os.getenv("GOOGLE_CLIENT_SECRET_PATH", ""),
            placeholder="C:/Users/you/Downloads/client_secret.json",
            help=(
                "Steps to get this file:\n"
                "1. Go to console.cloud.google.com\n"
                "2. Create a project (or select existing)\n"
                "3. Enable **Google Docs API** and **Google Drive API**\n"
                "4. Go to APIs & Services → Credentials\n"
                "5. Create an **OAuth 2.0 Client ID** (type: Desktop app)\n"
                "6. Download the JSON and paste the full path here"
            ),
        )
        if secret_path != os.getenv("GOOGLE_CLIENT_SECRET_PATH", ""):
            _save("GOOGLE_CLIENT_SECRET_PATH", secret_path)
        st.caption(
            "On Streamlit Cloud, do not use a Windows file path here. "
            "Add GOOGLE_CLIENT_SECRET_JSON and GOOGLE_TOKEN_JSON in App settings → Secrets instead."
        )

    with col_status:
        st.write("")
        from features.auth import get_credentials
        creds = get_credentials()
        if creds and creds.valid:
            st.success("✅ Authenticated")
        else:
            st.warning("⚠️ Not signed in")

    col_auth, col_out = st.columns(2)
    with col_auth:
        if _is_cloud():
            _render_web_auth_button()
        else:
            if st.button("🔐 Authenticate with Google", type="primary", use_container_width=True):
                _run_oauth(secret_path)

    with col_out:
        if st.button("🚪 Sign Out", use_container_width=True):
            from features.auth import TOKEN_PATH
            st.session_state.google_creds = None
            st.session_state.google_signed_out = True
            if TOKEN_PATH.exists():
                TOKEN_PATH.unlink()
            st.rerun()

    st.divider()

    # ── SERP API ──────────────────────────────────────────────
    st.subheader("🔍 SERP API (serper.dev)")
    st.info("Required for: **Keyword Grouping**")

    serp_key = st.text_input(
        "SERP API Key",
        value=os.getenv("SERP_API_KEY", ""),
        type="password",
        placeholder="Your serper.dev API key",
    )
    if st.button("Save SERP Key"):
        _save("SERP_API_KEY", serp_key)
        st.success("✅ SERP API Key saved.")

    st.divider()

    # ── Cloudinary ────────────────────────────────────────────
    st.subheader("☁️ Cloudinary API")
    st.info("Required for: **Extract & Optimize Images** (WebP format only)")

    cloud_name = st.text_input("Cloud Name", value=os.getenv("CLOUDINARY_CLOUD_NAME", ""))
    cld_key = st.text_input("API Key", value=os.getenv("CLOUDINARY_API_KEY", ""))
    cld_secret = st.text_input(
        "API Secret",
        value=os.getenv("CLOUDINARY_API_SECRET", ""),
        type="password",
    )
    if st.button("Save Cloudinary Settings"):
        _save("CLOUDINARY_CLOUD_NAME", cloud_name)
        _save("CLOUDINARY_API_KEY", cld_key)
        _save("CLOUDINARY_API_SECRET", cld_secret)
        st.success("✅ Cloudinary settings saved.")

    _auto_load_token()


def _render_web_auth_button():
    from google_auth_oauthlib.flow import Flow
    from features.auth import SCOPES

    client_config = _get_client_config()
    if not client_config:
        st.button("🔐 Authenticate with Google", type="primary", use_container_width=True, disabled=True)
        st.caption(
            "Add `GOOGLE_CLIENT_SECRET_JSON` to Streamlit Secrets to enable this. "
            "See the guide below for setup steps."
        )
        with st.expander("Setup: enable web authentication"):
            redirect_uri = _get_redirect_uri()
            st.markdown(f"""
**Step 1 — Register redirect URI in Google Cloud Console:**
1. Go to [console.cloud.google.com](https://console.cloud.google.com) → **APIs & Services** → **Credentials**
2. Click your OAuth 2.0 Client ID → **Edit**
3. Under **Authorized redirect URIs**, add: `{redirect_uri}`
4. Save

**Step 2 — Add the client secret to Streamlit Secrets:**
1. Open your `client_secret_....json` file and copy its contents
2. Go to **Streamlit Cloud** → your app → **Settings** → **Secrets**
3. Add:
```toml
GOOGLE_CLIENT_SECRET_JSON = '''<paste the JSON contents here>'''
```
4. Save and reboot the app
            """.strip())
        return

    redirect_uri = _get_redirect_uri()
    try:
        flow = Flow.from_client_config(client_config, scopes=SCOPES, redirect_uri=redirect_uri)
        auth_url, _ = flow.authorization_url(prompt="consent", access_type="offline")
        st.link_button("🔐 Authenticate with Google", auth_url, type="primary", use_container_width=True)
        st.caption(f"Redirect URI: `{redirect_uri}`")
    except Exception as e:
        st.error(f"Failed to build auth URL: {e}")


def _render_quick_guide():
    with st.expander("How this works"):
        st.markdown(
            """
Use this tab to save private API keys and Google authentication used by other tools.

1. Add Google OAuth credentials to use Google Docs, Google Drive, and Google Sheets features.
2. Add a Serper API key for **Keyword Grouping**.
3. Add Cloudinary credentials when you want WebP output in **Extract & Optimize Images from Google Docs**.
4. On Streamlit Cloud or Railway, save these values in private app secrets or environment variables instead of committing them to GitHub.

After changing Google permissions, sign out and authenticate again so Google grants the new access scope.
            """.strip()
        )


def _auto_load_token():
    if st.session_state.get("google_signed_out"):
        return
    if st.session_state.get("google_creds") is not None:
        return
    from features.auth import load_credentials
    creds = load_credentials()
    if creds:
        st.session_state.google_creds = creds


def _run_oauth(secret_path: str):
    if not secret_path or not Path(secret_path).exists():
        st.error("Please provide a valid path to your OAuth Client Secret JSON file first.")
        return
    try:
        from google_auth_oauthlib.flow import InstalledAppFlow
        from features.auth import SCOPES, TOKEN_PATH

        st.info("A browser window will open for Google sign-in. The app will resume after you authenticate.")
        flow = InstalledAppFlow.from_client_secrets_file(secret_path, SCOPES)
        creds = flow.run_local_server(port=0)
        TOKEN_PATH.write_text(creds.to_json())
        st.session_state.google_creds = creds
        st.session_state.google_signed_out = False
        st.success("✅ Authenticated successfully!")
        st.rerun()
    except Exception as e:
        st.error(f"Authentication failed: {e}")
