import base64
import json
import os
from pathlib import Path
import streamlit as st
from google.oauth2.credentials import Credentials
from google.auth.transport.requests import Request

SCOPES = [
    "https://www.googleapis.com/auth/documents.readonly",
    "https://www.googleapis.com/auth/drive.readonly",
    "https://www.googleapis.com/auth/spreadsheets",
]

TOKEN_PATH = Path("token.json")


def _get_private_value(key: str) -> str:
    value = os.getenv(key, "").strip()
    if value:
        return value
    try:
        return str(st.secrets.get(key, "")).strip()
    except Exception:
        return ""


def _load_token_info_from_env() -> dict | None:
    """Load Google OAuth token JSON from private hosting environment variables."""
    token_json = _get_private_value("GOOGLE_TOKEN_JSON")
    token_b64 = _get_private_value("GOOGLE_TOKEN_JSON_B64")

    if token_b64:
        try:
            token_json = base64.b64decode(token_b64).decode("utf-8")
        except Exception:
            return None

    if not token_json:
        return None

    try:
        return json.loads(token_json)
    except json.JSONDecodeError:
        return None


def _load_credentials_from_env() -> Credentials | None:
    token_info = _load_token_info_from_env()
    if not token_info:
        return None
    try:
        creds = Credentials.from_authorized_user_info(token_info, SCOPES)
        if creds.expired and creds.refresh_token:
            creds.refresh(Request())
        return creds if creds.valid else None
    except Exception:
        return None


def load_credentials() -> Credentials | None:
    """Load and refresh credentials from env first, then token.json for local use."""
    env_creds = _load_credentials_from_env()
    if env_creds:
        return env_creds

    if not TOKEN_PATH.exists():
        return None
    try:
        creds = Credentials.from_authorized_user_file(str(TOKEN_PATH), SCOPES)
        if creds.expired and creds.refresh_token:
            creds.refresh(Request())
            TOKEN_PATH.write_text(creds.to_json())
        return creds if creds.valid else None
    except Exception:
        return None


def get_credentials() -> Credentials | None:
    """Return valid Google credentials from session state, refreshing if needed."""
    creds: Credentials | None = st.session_state.get("google_creds")

    if creds and creds.valid:
        return creds

    if creds and creds.expired and creds.refresh_token:
        try:
            creds.refresh(Request())
            st.session_state.google_creds = creds
            if TOKEN_PATH.exists():
                TOKEN_PATH.write_text(creds.to_json())
            return creds
        except Exception:
            st.session_state.google_creds = None

    creds = load_credentials()
    if creds:
        st.session_state.google_creds = creds
    return creds


def require_auth() -> bool:
    """Show a warning and return False when the user is not authenticated."""
    if get_credentials():
        return True
    st.warning(
        "⚠️ Google authentication required. "
        "Please go to the **⚙️ Settings** tab and click **Authenticate with Google**."
    )
    return False
