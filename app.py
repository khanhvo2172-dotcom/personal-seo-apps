import streamlit as st
from dotenv import load_dotenv

load_dotenv()

st.set_page_config(
    page_title="Personal Tools",
    page_icon="tools",
    layout="wide",
    initial_sidebar_state="expanded",
)

if "google_creds" not in st.session_state:
    st.session_state.google_creds = None

from features import (
    autofill_column,
    check_links,
    download_gdrive_images,
    extract_optimize_images,
    humanizer,
    keyword_grouping,
    settings,
)

FEATURES = {
    "Settings": settings.render,
    "Humanizer - AI detection": humanizer.render,
    "Check Internal & External Links in Google Docs": check_links.render,
    "Extract & Optimize Images from Google Docs": extract_optimize_images.render,
    "Download Images using GDrive Links": download_gdrive_images.render,
    "Keyword Grouping": keyword_grouping.render,
    "Autofill Column": autofill_column.render,
}


def _apply_style():
    st.markdown(
        """
        <style>
        :root {
            --app-border: rgba(15, 23, 42, 0.10);
            --app-muted: #64748b;
            --app-surface: #ffffff;
        }

        .stApp {
            background: #f8fafc;
        }

        [data-testid="stSidebar"] {
            background: #ffffff;
            border-right: 1px solid var(--app-border);
        }

        [data-testid="stSidebar"] [data-testid="stMarkdownContainer"] p {
            color: var(--app-muted);
        }

        [data-testid="stSidebar"] .stRadio label {
            padding: 0.3rem 0;
        }

        .block-container {
            max-width: 1180px;
            padding-top: 2rem;
            padding-bottom: 3rem;
        }

        h1, h2, h3 {
            letter-spacing: 0;
        }

        div[data-testid="stExpander"],
        div[data-testid="stForm"] {
            background: var(--app-surface);
            border: 1px solid var(--app-border);
            border-radius: 8px;
        }

        .stButton > button,
        .stDownloadButton > button {
            border-radius: 8px;
            border: 1px solid var(--app-border);
        }
        </style>
        """,
        unsafe_allow_html=True,
    )


def _render_sidebar() -> str:
    with st.sidebar:
        st.title("Personal Tools")
        st.caption("SEO utilities")
        return st.radio(
            "Features",
            list(FEATURES.keys()),
            label_visibility="collapsed",
        )


_apply_style()
selected_feature = _render_sidebar()
FEATURES[selected_feature]()
