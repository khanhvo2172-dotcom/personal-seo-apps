import streamlit as st
from dotenv import load_dotenv

load_dotenv()

st.set_page_config(
    page_title="Personal Tools",
    page_icon="🛠️",
    layout="wide",
)

if "google_creds" not in st.session_state:
    st.session_state.google_creds = None

from features import settings, keyword_grouping, download_gdrive_images, check_links, extract_optimize_images

st.title("🛠️ Personal Tools")

tab_settings, tab_kw, tab_gdrive, tab_links, tab_extract = st.tabs([
    "⚙️ Settings",
    "🔑 Keyword Grouping",
    "📥 Download GDrive Images",
    "🔗 Check Links in GDocs",
    "🖼️ Extract & Optimize Images",
])

with tab_settings:
    settings.render()

with tab_kw:
    keyword_grouping.render()

with tab_gdrive:
    download_gdrive_images.render()

with tab_links:
    check_links.render()

with tab_extract:
    extract_optimize_images.render()
