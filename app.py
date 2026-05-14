import streamlit as st
from dotenv import load_dotenv

load_dotenv()

st.set_page_config(
    page_title="Personal Tools",
    page_icon="tools",
    layout="wide",
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

st.title("Personal Tools")

tab_settings, tab_humanizer, tab_links, tab_extract, tab_gdrive, tab_kw, tab_autofill = st.tabs([
    "Settings",
    "Humanizer - AI detection",
    "Check Internal & External Links in Google Docs",
    "Extract & Optimize Images from Google Docs",
    "Download Images using GDrive Links",
    "Keyword Grouping",
    "Autofill Column",
])

with tab_settings:
    settings.render()

with tab_humanizer:
    humanizer.render()

with tab_links:
    check_links.render()

with tab_extract:
    extract_optimize_images.render()

with tab_gdrive:
    download_gdrive_images.render()

with tab_kw:
    keyword_grouping.render()

with tab_autofill:
    autofill_column.render()
