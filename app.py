import streamlit as st
import streamlit.components.v1 as components
from dotenv import load_dotenv
from streamlit_sortables import sort_items

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
if "reorder_mode" not in st.session_state:
    st.session_state.reorder_mode = False

if "code" in st.query_params:
    st.session_state.selected_feature = "Settings"

from features import (
    autofill_column,
    bulk_check_dr,
    cartesian_product,
    check_links,
    compression_checker,
    download_gdrive_images,
    extract_optimize_images,
    format_gdocs,
    humanizer,
    keyword_grouping,
    ranking_tracker,
    settings,
    watermark_remover,
    youtube_seeding_tracker,
)

FEATURES = {
    "Settings": settings.render,
    "Humanizer - AI detection": humanizer.render,
    "Check Internal & External Links in Google Docs": check_links.render,
    "Format Google Docs File": format_gdocs.render,
    "Remove Image Fingerprints & Watermarks": watermark_remover.render,
    "Extract & Optimize Images from Google Docs": extract_optimize_images.render,
    "Download Images using GDrive Links": download_gdrive_images.render,
    "Keyword Grouping": keyword_grouping.render,
    "Keyword Combiner": cartesian_product.render,
    "Bulk Check Ahrefs DR": bulk_check_dr.render,
    "Page Compression Checker": compression_checker.render,
    "Keyword Ranking Tracker": ranking_tracker.render,
    "Autofill Column": autofill_column.render,
    "YouTube Seeding Tracker": youtube_seeding_tracker.render,
}

FEATURE_ICONS = {
    "Settings": "⚙️",
    "Humanizer - AI detection": "🤖",
    "Check Internal & External Links in Google Docs": "🔗",
    "Format Google Docs File": "🛠️",
    "Remove Image Fingerprints & Watermarks": "🧹",
    "Extract & Optimize Images from Google Docs": "🖼️",
    "Download Images using GDrive Links": "⬇️",
    "Keyword Grouping": "🏷️",
    "Keyword Combiner": "🧮",
    "Bulk Check Ahrefs DR": "📈",
    "Page Compression Checker": "🗜️",
    "Keyword Ranking Tracker": "🎯",
    "Autofill Column": "📊",
    "YouTube Seeding Tracker": "🌱",
}

if "feature_order" not in st.session_state:
    st.session_state.feature_order = list(FEATURES.keys())
else:
    current = st.session_state.feature_order
    all_keys = list(FEATURES.keys())
    st.session_state.feature_order = (
        [f for f in current if f in FEATURES]
        + [f for f in all_keys if f not in current]
    )


def _apply_style():
    st.markdown(
        """
        <style>
        @import url('https://fonts.googleapis.com/css2?family=Google+Sans:wght@400;500;600;700&family=Roboto:wght@300;400;500;700&display=swap');

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

        h1, h2, h3, h4, h5, h6 {
            font-family: 'Google Sans', 'Roboto', sans-serif !important;
            font-weight: 500;
            color: var(--g-text);
            letter-spacing: 0;
        }

        .stApp {
            background: var(--g-bg);
        }

        /* ── Feature page titles ───────────────────────────── */
        .block-container h1 {
            font-size: 2.4rem !important;
            font-weight: 600 !important;
        }

        .block-container h2 {
            font-size: 2rem !important;
            font-weight: 600 !important;
        }

        .block-container h3 {
            font-size: 1.4rem !important;
            font-weight: 500 !important;
        }

        /* ── Dark sidebar shell ────────────────────────────── */
        [data-testid="stSidebar"] {
            background: linear-gradient(180deg, #141829 0%, #1a1f33 55%, #1c2238 100%) !important;
            border-right: 1px solid rgba(255,255,255,0.06) !important;
            box-shadow: 4px 0 30px rgba(0,0,0,0.4);
        }

        [data-testid="stSidebar"]::-webkit-scrollbar { width: 3px; }
        [data-testid="stSidebar"]::-webkit-scrollbar-track { background: transparent; }
        [data-testid="stSidebar"]::-webkit-scrollbar-thumb {
            background: rgba(255,255,255,0.1);
            border-radius: 4px;
        }

        [data-testid="stSidebar"] [data-testid="stMarkdownContainer"] p {
            color: #3e4a68 !important;
            font-size: 12px;
        }

        /* ── Nav buttons — base ────────────────────────────── */
        [data-testid="stSidebar"] .stButton > button {
            width: 100%;
            display: flex;
            justify-content: flex-start !important;
            align-items: center;
            background: transparent !important;
            border: 1px solid transparent !important;
            border-radius: 10px !important;
            color: #6e7a95 !important;
            font-family: 'Roboto', sans-serif !important;
            font-weight: 400 !important;
            font-size: 13.5px !important;
            padding: 9px 14px !important;
            text-align: left !important;
            box-shadow: none !important;
            transition: background 0.18s ease, color 0.18s ease,
                        border-color 0.18s ease, transform 0.14s ease,
                        box-shadow 0.18s ease !important;
            letter-spacing: 0.2px;
            margin-bottom: 2px;
        }

        [data-testid="stSidebar"] .stButton > button div,
        [data-testid="stSidebar"] .stButton > button [data-testid="stMarkdownContainer"],
        [data-testid="stSidebar"] .stButton > button p {
            width: 100% !important;
            text-align: left !important;
            color: inherit !important;
            justify-content: flex-start !important;
            display: block !important;
        }

        /* ── Nav buttons — hover ───────────────────────────── */
        [data-testid="stSidebar"] .stButton > button:hover {
            background: rgba(255,255,255,0.07) !important;
            color: #c4cde0 !important;
            border-color: rgba(255,255,255,0.08) !important;
            transform: translateX(3px) !important;
            box-shadow: none !important;
        }

        /* ── Nav buttons — active / selected ──────────────── */
        [data-testid="stSidebar"] .stButton > button[kind="primary"] {
            background: linear-gradient(135deg,
                rgba(66,133,244,0.22) 0%,
                rgba(26,115,232,0.12) 100%) !important;
            border: 1px solid rgba(66,133,244,0.32) !important;
            color: #93bbfc !important;
            font-weight: 500 !important;
            box-shadow:
                0 2px 14px rgba(66,133,244,0.18),
                inset 0 1px 0 rgba(255,255,255,0.06) !important;
        }

        [data-testid="stSidebar"] .stButton > button[kind="primary"]:hover {
            background: linear-gradient(135deg,
                rgba(66,133,244,0.32) 0%,
                rgba(26,115,232,0.2) 100%) !important;
            transform: translateX(3px) !important;
        }

        /* ── Main content area ─────────────────────────────── */
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
            // No-op
        }
        </script>
        """,
        height=0,
        width=0,
    )


def _render_sidebar() -> str:
    with st.sidebar:
        # ── Sidebar header (no icon) ─────────────────────────
        st.markdown(
            """
            <div style="padding:16px 4px 18px 4px;
                        border-bottom:1px solid rgba(255,255,255,0.08);
                        margin-bottom:10px;">
                <div style="font-family:'Google Sans','Roboto',sans-serif;
                            font-size:20px;font-weight:700;
                            color:#e2e7f0;letter-spacing:0.3px;
                            margin-bottom:5px;">
                    Personal Tools
                </div>
                <div style="color:#e2e7f0;font-size:11px;
                            font-family:'Roboto',sans-serif;
                            letter-spacing:0.3px;font-weight:400;
                            opacity:0.55;">
                    Created by Khanh (and AI)
                </div>
            </div>
            """,
            unsafe_allow_html=True,
        )

        if st.session_state.reorder_mode:
            st.markdown(
                """<div style='background:rgba(66,133,244,0.1);
                              border:1px solid rgba(66,133,244,0.25);
                              border-radius:8px;padding:8px 12px;margin-bottom:10px;
                              color:#6a9fd8;font-size:12px;letter-spacing:0.3px;'>
                    ⇕&nbsp; Drag items to reorder</div>""",
                unsafe_allow_html=True,
            )
            new_order = sort_items(
                st.session_state.feature_order,
                direction="vertical",
            )
            if new_order != st.session_state.feature_order:
                st.session_state.feature_order = new_order

            col1, col2 = st.columns(2)
            with col1:
                if st.button("Done", type="primary", use_container_width=True):
                    st.session_state.reorder_mode = False
                    st.rerun()
            with col2:
                if st.button("Reset", use_container_width=True):
                    st.session_state.feature_order = list(FEATURES.keys())
                    st.session_state.reorder_mode = False
                    st.rerun()
        else:
            for feature_name in st.session_state.feature_order:
                icon = FEATURE_ICONS.get(feature_name, "▪️")
                is_active = st.session_state.selected_feature == feature_name
                if st.button(
                    f"{icon}  {feature_name}",
                    key=f"nav_{feature_name}",
                    type="primary" if is_active else "secondary",
                    use_container_width=True,
                ):
                    st.session_state.selected_feature = feature_name
                    st.rerun()

            st.markdown(
                """<hr style='margin:12px 0 8px 0;border:none;
                               border-top:1px solid rgba(255,255,255,0.07);'>""",
                unsafe_allow_html=True,
            )
            if st.button(
                "⇕  Reorder",
                use_container_width=True,
                help="Drag and drop to change the sidebar order",
            ):
                st.session_state.reorder_mode = True
                st.rerun()

        return st.session_state.selected_feature


_apply_style()
selected_feature = _render_sidebar()
FEATURES[selected_feature]()
