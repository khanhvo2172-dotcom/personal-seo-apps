import re
import pandas as pd
import requests
import streamlit as st
from collections import Counter
from concurrent.futures import ThreadPoolExecutor, as_completed
from urllib.parse import urlsplit, urlunsplit
from features.auth import get_credentials, require_auth

STATUS_CHECK_TIMEOUT = 8
STATUS_CHECK_WORKERS = 10


def render():
    st.header("Check Internal & External Links in Google Docs")
    st.caption(
        "Checks which of your target URLs appear in a Google Doc, "
        "and identifies missing or duplicate links."
    )
    _render_quick_guide()

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

    if submitted:
        if not doc_url.strip():
            st.error("Please provide a Google Doc URL.")
            return
        if not urls_input.strip():
            st.error("Please provide at least one URL to check.")
            return

        results = _run_check(doc_url.strip(), urls_input)
        if results:
            st.session_state["check_links_results"] = results

    if "check_links_results" in st.session_state:
        _render_results(st.session_state["check_links_results"])


def _render_quick_guide():
    with st.expander("How this works"):
        st.markdown(
            """
1. Authenticate with Google in **Settings**.
2. Paste the Google Doc URL you want to inspect.
3. Paste the internal or external URLs you expect to find, one URL per line.
4. Click **Check Links**.
5. The app reads links from the document body, tables, headers, footers, footnotes, linked images, and rich links.
6. Review three outputs: all links found, target URLs missing from the document, and duplicate links used more than once.

Use this before publishing or updating SEO content to confirm important internal links and external citations are present.
            """.strip()
        )


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
            return None

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
    status_urls = sorted({u for u, _ in unique} | target_urls)

    with st.spinner("Checking URL status codes..."):
        status_codes = _check_status_codes(status_urls)

    return {
        "unique": [(u, a, status_codes.get(u, "N/A")) for u, a in unique],
        "target_count": len(target_urls),
        "missing": [(u, status_codes.get(u, "N/A")) for u in missing],
        "duplicates": [(u, c, status_codes.get(u, "N/A")) for u, c in duplicates],
    }


def _render_results(results: dict):
    unique = _with_status(results["unique"], "unique")
    missing = _with_status(results["missing"], "missing")
    duplicates = _with_status(results["duplicates"], "duplicates")

    st.success(
        f"Found **{len(unique)}** unique links in the document. "
        f"Checked **{results['target_count']}** target URLs."
    )

    st.subheader("✅ All Links Found in Document")
    pd.set_option("display.max_colwidth", None)
    df_found = pd.DataFrame(unique, columns=["🔗 Link", "💬 Anchor Text", "Status Code"])
    df_found = _filter_dataframe(
        df_found,
        _render_filter(
            "Filter all links",
            "check_links_filter_found",
            "Type part of a URL, anchor text, or status code...",
        ),
    )
    _render_selectable_table(
        df_found,
        "check_links_table_found",
    )

    col1, col2 = st.columns(2)

    with col1:
        st.subheader("🚫 Missing Links")
        if missing:
            df_missing = pd.DataFrame(missing, columns=["URL", "Status Code"])
            df_missing = _filter_dataframe(
                df_missing,
                _render_filter(
                    "Filter missing links",
                    "check_links_filter_missing",
                    "Type part of a URL or status code...",
                ),
            )
            _render_selectable_table(
                df_missing,
                "check_links_table_missing",
            )
        else:
            st.success("All target URLs are present in the document.")

    with col2:
        st.subheader("🔁 Duplicate Links (> 1 occurrence)")
        if duplicates:
            df_duplicates = _filter_dataframe(
                pd.DataFrame(duplicates, columns=["URL", "Count", "Status Code"]),
                _render_filter(
                    "Filter duplicate links",
                    "check_links_filter_duplicates",
                    "Type part of a URL, count, or status code...",
                ),
            )
            _render_selectable_table(
                df_duplicates,
                "check_links_table_duplicates",
            )
        else:
            st.success("No duplicate links found.")


def _render_filter(label: str, key: str, placeholder: str) -> str:
    st.markdown(
        f"""
        <div style="
            margin: 0.35rem 0 0.25rem 0;
            padding: 0.65rem 0.8rem;
            border: 2px solid #f59e0b;
            border-radius: 8px;
            background: #fffbeb;
            color: #78350f;
            font-weight: 700;
        ">
            FILTER TABLE: {label}
        </div>
        """,
        unsafe_allow_html=True,
    )
    return st.text_input(label, key=key, placeholder=placeholder, label_visibility="collapsed")


def _with_status(rows: list, table_type: str) -> list:
    normalized = []
    for row in rows:
        if table_type == "missing":
            if isinstance(row, str):
                normalized.append((row, "N/A"))
            else:
                normalized.append(row if len(row) >= 2 else (row[0], "N/A"))
        elif table_type == "duplicates":
            normalized.append(row if len(row) >= 3 else (row[0], row[1], "N/A"))
        else:
            normalized.append(row if len(row) >= 3 else (row[0], row[1], "N/A"))
    return normalized


def _render_selectable_table(df: pd.DataFrame, key: str):
    st.dataframe(
        df,
        use_container_width=True,
        hide_index=True,
        key=key,
        on_select="rerun",
        selection_mode="multi-row",
    )


def _filter_dataframe(df: pd.DataFrame, query: str) -> pd.DataFrame:
    query = (query or "").strip()
    if not query or df.empty:
        return df

    matches = df.astype(str).apply(
        lambda row: row.str.contains(query, case=False, na=False, regex=False).any(),
        axis=1,
    )
    return df[matches]


def _check_status_codes(urls: list[str]) -> dict[str, str]:
    if not urls:
        return {}

    results = {}
    with ThreadPoolExecutor(max_workers=STATUS_CHECK_WORKERS) as executor:
        futures = {executor.submit(_check_status_code, url): url for url in urls}
        for future in as_completed(futures):
            url = futures[future]
            try:
                results[url] = future.result()
            except Exception:
                results[url] = "Error"
    return results


def _check_status_code(url: str) -> str:
    try:
        response = requests.head(
            url,
            allow_redirects=True,
            timeout=STATUS_CHECK_TIMEOUT,
            headers={"User-Agent": "Mozilla/5.0"},
        )
        if response.status_code in (403, 405):
            response = requests.get(
                url,
                allow_redirects=True,
                timeout=STATUS_CHECK_TIMEOUT,
                headers={"User-Agent": "Mozilla/5.0"},
                stream=True,
            )
        return str(response.status_code)
    except requests.RequestException:
        return "Error"
