import re
import pandas as pd
import streamlit as st
from collections import Counter
from urllib.parse import urlsplit, urlunsplit
from features.auth import get_credentials, require_auth


def render():
    st.header("🔗 Check Links in Google Docs")
    st.caption(
        "Checks which of your target URLs appear in a Google Doc, "
        "and identifies missing or duplicate links."
    )

    if not require_auth():
        return

    with st.form("check_links_form"):
        doc_url = st.text_input(
            "Google Doc URL",
            placeholder="https://docs.google.com/document/d/.../edit",
        )
        urls_input = st.text_area(
            "URLs to check — one per line",
            placeholder="https://www.example.com/page-1\nhttps://www.example.com/page-2",
            height=150,
        )
        submitted = st.form_submit_button("🔍 Check Links", type="primary")

    if not submitted:
        return

    if not doc_url.strip():
        st.error("Please provide a Google Doc URL.")
        return
    if not urls_input.strip():
        st.error("Please provide at least one URL to check.")
        return

    _run_check(doc_url.strip(), urls_input)


# ── helpers ──────────────────────────────────────────────────

def _normalize(url: str) -> str:
    if not url:
        return ""
    parts = urlsplit(url.strip())
    return urlunsplit((
        parts.scheme.lower(),
        parts.netloc.lower(),
        parts.path.rstrip("/"),
        parts.query,
        parts.fragment,
    ))


def _find_links(elements: list, found: list):
    """Recursively extract (url, anchor_text) pairs from document elements."""
    for el in elements:
        if "paragraph" in el:
            for pe in el["paragraph"].get("elements", []):
                if "textRun" in pe:
                    tr = pe["textRun"]
                    link = tr.get("textStyle", {}).get("link")
                    if link and link.get("url"):
                        anchor = tr.get("content", "").replace("\n", " ").strip()
                        found.append((link["url"], anchor))
                if "inlineObjectElement" in pe:
                    link = pe["inlineObjectElement"].get("textStyle", {}).get("link")
                    if link and link.get("url"):
                        found.append((link["url"], "embedded in image"))
                if "richLink" in pe:
                    props = pe["richLink"].get("richLinkProperties", {}) or {}
                    if props.get("uri"):
                        found.append((props["uri"], (props.get("title") or "").strip()))
        elif "table" in el:
            for row in el["table"].get("tableRows", []):
                for cell in row.get("tableCells", []):
                    _find_links(cell.get("content", []), found)
        elif "tableOfContents" in el:
            _find_links(el["tableOfContents"].get("content", []), found)


def _merge_adjacent(raw: list) -> list:
    """Merge anchor text fragments that belong to the same link run."""
    if not raw:
        return []
    merged, cur_url, cur_anchor = [], raw[0][0], raw[0][1]
    for url, anchor in raw[1:]:
        if url == cur_url:
            cur_anchor = (cur_anchor + " " + anchor).strip() if anchor else cur_anchor
        else:
            merged.append((cur_url, cur_anchor.strip()))
            cur_url, cur_anchor = url, anchor
    merged.append((cur_url, cur_anchor.strip()))
    return merged


def _run_check(doc_url: str, urls_input: str):
    m = re.search(r"/document/d/([a-zA-Z0-9_-]+)", doc_url)
    if not m:
        st.error("Invalid Google Doc URL. Please use the full URL from your browser.")
        return
    doc_id = m.group(1)

    creds = get_credentials()

    with st.spinner("Fetching document…"):
        try:
            from googleapiclient.discovery import build
            from googleapiclient.errors import HttpError

            docs = build("docs", "v1", credentials=creds)
            doc = docs.documents().get(documentId=doc_id).execute()
        except Exception as e:
            err = str(e)
            if "403" in err:
                st.error("Permission denied (403). Make sure you have at least Viewer access to this Google Doc.")
            elif "404" in err:
                st.error(f"Document not found (404). Check the URL.")
            else:
                st.error(f"Failed to fetch document: {e}")
            return

    # Collect links from body, headers, footers, footnotes
    raw: list = []
    _find_links(doc.get("body", {}).get("content", []), raw)
    for part in ("footnotes", "headers", "footers"):
        for item in doc.get(part, {}).values():
            _find_links(item.get("content", []), raw)

    merged = _merge_adjacent(raw)
    normalized = [(n, a) for u, a in merged if (n := _normalize(u))]
    unique = list(dict.fromkeys(normalized))

    target_urls = {_normalize(u) for u in urls_input.strip().splitlines() if u.strip()}
    found_url_list = [u for u, _ in normalized]
    url_counts = Counter(found_url_list)
    missing = sorted(target_urls - set(found_url_list))
    duplicates = sorted([(u, c) for u, c in url_counts.items() if c > 1], key=lambda x: -x[1])

    st.success(
        f"Found **{len(unique)}** unique links in the document. "
        f"Checked **{len(target_urls)}** target URLs."
    )

    st.subheader("✅ All Links Found in Document")
    pd.set_option("display.max_colwidth", None)
    df_found = pd.DataFrame(unique, columns=["🔗 Link", "💬 Anchor Text"])
    st.dataframe(df_found, use_container_width=True)

    col1, col2 = st.columns(2)

    with col1:
        st.subheader("🚫 Missing Links")
        if missing:
            st.dataframe(pd.DataFrame(missing, columns=["URL"]), use_container_width=True)
        else:
            st.success("All target URLs are present in the document.")

    with col2:
        st.subheader("🔁 Duplicate Links (> 1 occurrence)")
        if duplicates:
            st.dataframe(
                pd.DataFrame(duplicates, columns=["URL", "Count"]),
                use_container_width=True,
            )
        else:
            st.success("No duplicate links found.")
