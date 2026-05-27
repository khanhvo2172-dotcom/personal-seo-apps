import streamlit as st
import streamlit.components.v1 as components
from dotenv import load_dotenv

load_dotenv()

st.set_page_config(
    page_title="Personal Tools",
    page_icon="🔷",
    layout="wide",
    initial_sidebar_state="expanded",
)

if "google_creds" not in st.session_state:
    st.session_state.google_creds = None
if "google_signed_out" not in st.session_state:
    st.session_state.google_signed_out = False
if "selected_feature" not in st.session_state:
    st.session_state.selected_feature = "Settings"

from features import (
    autofill_column,
    check_links,
    download_gdrive_images,
    extract_optimize_images,
    humanizer,
    keyword_grouping,
    remove_empty_rows,
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
    "Remove Empty Rows in Google Docs": remove_empty_rows.render,
}


def _apply_style():
    st.markdown(
        """
        <style>
        @import url('https://fonts.googleapis.com/css2?family=Google+Sans:wght@400;500;700&family=Roboto:wght@300;400;500;700&display=swap');

        :root {
            --g-blue: #4285F4;
            --g-blue-dark: #1967D2;
            --g-blue-light: #E8F0FE;
            --g-text: #202124;
            --g-text-muted: #5f6368;
            --g-border: #dadce0;
            --g-surface: #ffffff;
            --g-bg: #f8f9fa;
        }

        html, body, [class*="css"] {
            font-family: 'Roboto', sans-serif !important;
        }

        /* Exclude Material Icons spans from font override */
        .material-icons, .material-symbols-rounded,
        .material-symbols-outlined, [data-icon] {
            font-family: 'Material Icons' !important;
        }

        h1, h2, h3, h4, h5, h6 {
            font-family: 'Google Sans', 'Roboto', sans-serif !important;
            font-weight: 500;
            color: var(--g-text);
            letter-spacing: 0;
        }

        .stApp {
            background: var(--g-bg);
        }

        [data-testid="stSidebar"] {
            background: var(--g-surface);
            border-right: 1px solid var(--g-border);
        }

        [data-testid="stSidebar"] [data-testid="stMarkdownContainer"] p {
            color: var(--g-text-muted);
            font-size: 12px;
        }

        .block-container {
            max-width: 1180px;
            padding-top: 2rem;
            padding-bottom: 3rem;
        }

        div[data-testid="stExpander"],
        div[data-testid="stForm"] {
            background: var(--g-surface);
            border: 1px solid var(--g-border);
            border-radius: 8px;
            box-shadow: 0 1px 2px rgba(60,64,67,.15), 0 2px 6px rgba(60,64,67,.1);
        }

        .stTextInput > div > div > input,
        .stTextArea > div > div > textarea {
            border: 1px solid var(--g-border);
            border-radius: 4px;
            color: var(--g-text);
        }

        .stTextInput > div > div > input:focus,
        .stTextArea > div > div > textarea:focus {
            border-color: var(--g-blue);
            box-shadow: 0 0 0 2px rgba(66,133,244,.2);
            outline: none;
        }

        .stButton > button,
        .stDownloadButton > button {
            font-family: 'Google Sans', 'Roboto', sans-serif !important;
            font-weight: 500;
            font-size: 14px;
            letter-spacing: 0.25px;
            border-radius: 4px;
            border: 1px solid var(--g-border);
            transition: background 0.2s, box-shadow 0.2s;
        }

        [data-testid="stSidebar"] .stButton > button {
            width: 100%;
            display: flex;
            justify-content: flex-start !important;
            align-items: center;
            background: transparent;
            border: none;
            border-radius: 24px;
            color: var(--g-text);
            font-weight: 400;
            font-size: 14px;
            padding: 0.5rem 1rem;
            text-align: left !important;
            box-shadow: none;
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
            background: #f1f3f4 !important;
            color: var(--g-text) !important;
            box-shadow: none !important;
        }

        [data-testid="stSidebar"] .stButton > button[kind="primary"] {
            background: var(--g-blue-light) !important;
            color: var(--g-blue-dark) !important;
            font-weight: 500 !important;
        }

        [data-testid="stSidebar"] .stButton > button[kind="primary"]:hover {
            background: #d2e3fc !important;
        }

        </style>
        """,
        unsafe_allow_html=True,
    )
    components.html(
        """
        <script>
        try {
            const win = window.parent;
            const doc = window.parent.document;
            if (!doc.__personalToolsCopyGuard) {
                doc.__personalToolsCopyGuard = true;
                const blockStreamlitCopyShortcut = (event) => {
                    const isCopyShortcut =
                        (event.ctrlKey || event.metaKey) &&
                        (event.key || "").toLowerCase() === "c";
                    if (isCopyShortcut) {
                        event.stopImmediatePropagation();
                    }
                };
                ["keydown", "keypress", "keyup"].forEach((eventName) => {
                    win.addEventListener(eventName, blockStreamlitCopyShortcut, true);
                    doc.addEventListener(eventName, blockStreamlitCopyShortcut, true);
                });
            }
        } catch (error) {
            // No-op: browser copy still works if the parent document is unavailable.
        }
        </script>
        """,
        height=0,
        width=0,
    )


def _render_sidebar() -> str:
    with st.sidebar:
        st.markdown(
            """
            <div style="padding:8px 0 16px 0; border-bottom:1px solid #dadce0; margin-bottom:8px;">
                <div style="font-family:'Google Sans','Roboto',sans-serif; font-size:20px; font-weight:500; color:#202124; line-height:1.3;">
                    <span style="color:#4285F4">P</span><span style="color:#DB4437">e</span><span style="color:#F4B400">r</span><span style="color:#4285F4">s</span><span style="color:#0F9D58">o</span><span style="color:#DB4437">n</span><span style="color:#F4B400">a</span><span style="color:#4285F4">l</span>&nbsp;Tools
                </div>
                <div style="color:#5f6368; font-size:12px; margin-top:4px; font-family:'Roboto',sans-serif;">SEO utilities</div>
            </div>
            """,
            unsafe_allow_html=True,
        )
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
