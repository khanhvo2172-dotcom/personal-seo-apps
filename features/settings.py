import os
import streamlit as st
from pathlib import Path
from dotenv import load_dotenv, set_key

ENV_PATH = Path(__file__).parent.parent / ".env"


def _save(key: str, value: str):
    ENV_PATH.touch()
    set_key(str(ENV_PATH), key, value)
    os.environ[key] = value


def render():
    st.header("⚙️ Settings")
    load_dotenv(str(ENV_PATH), override=True)

    # ── Google Authentication ─────────────────────────────────
    st.subheader("🔑 Google Authentication")
    st.info("Required for: **Check Links in GDocs** and **Extract & Optimize Images**")

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
        if st.button("🔐 Authenticate with Google", type="primary", use_container_width=True):
            _run_oauth(secret_path)
    with col_out:
        if st.button("🚪 Sign Out", use_container_width=True):
            from features.auth import TOKEN_PATH
            st.session_state.google_creds = None
            if TOKEN_PATH.exists():
                TOKEN_PATH.unlink()
            st.success("Signed out.")
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

    # Silently restore saved credentials on first load
    _auto_load_token()


def _auto_load_token():
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
        st.success("✅ Authenticated successfully!")
        st.rerun()
    except Exception as e:
        st.error(f"Authentication failed: {e}")
