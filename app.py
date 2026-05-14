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
if "selected_feature" not in st.session_state:
    st.session_state.selected_feature = "Settings"

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

        [data-testid="stSidebar"] .stButton > button {
            width: 100%;
            display: flex;
            justify-content: flex-start !important;
            align-items: center;
            background: transparent;
            border: 1px solid transparent;
            color: #334155;
            font-weight: 500;
            padding: 0.55rem 0.7rem;
            text-align: left !important;
        }

        [data-testid="stSidebar"] .stButton > button div,
        [data-testid="stSidebar"] .stButton > button [data-testid="stMarkdownContainer"] {
            width: 100%;
            justify-content: flex-start !important;
            text-align: left !important;
        }

        [data-testid="stSidebar"] .stButton > button p {
            width: 100%;
            display: block;
            text-align: left !important;
        }

        [data-testid="stSidebar"] .stButton > button:hover {
            background: #f1f5f9;
            border-color: #e2e8f0;
            color: #0f172a;
        }

        </style>
        """,
        unsafe_allow_html=True,
    )


def _render_sidebar() -> str:
    with st.sidebar:
        st.title("Personal Tools")
        st.caption("SEO utilities")
        st.write("")
        for feature_name in FEATURES:
            is_active = st.session_state.selected_feature == feature_name
            if st.button(
                feature_name,
                key=f"nav_{feature_name}",
                type="primary" if is_active else "secondary",
                use_container_width=True,
            ):
                st.session_state.selected_feature = feature_name
                st.rerun()
        return st.session_state.selected_feature


_apply_style()
selected_feature = _render_sidebar()
FEATURES[selected_feature]()
