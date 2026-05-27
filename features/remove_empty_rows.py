import re
import pandas as pd
import streamlit as st
from urllib.parse import urlparse, parse_qs
from features.auth import get_credentials, require_auth


def render():
    st.header("Remove Empty Rows in Google Docs")
    st.caption("Removes all completely empty paragraphs from one or more Google Docs files.")
    _render_guide()

    if not require_auth():
        return

    with st.form("remove_empty_rows_form"):
        urls_input = st.text_area(
            "Google Docs URLs (one per line)",
            placeholder=(
                "https://docs.google.com/document/d/.../edit\n"
                "https://docs.google.com/document/d/.../edit?tab=t.abc123"
            ),
            height=160,
        )
        tab_mode = st.radio(
            "Tabs to process",
            ["Tab from URL (or first tab if none specified)", "All tabs"],
            horizontal=True,
        )
        submitted = st.form_submit_button("🗑️ Remove Empty Rows", type="primary")

    if not submitted:
        return

    urls = [u.strip() for u in urls_input.strip().splitlines() if u.strip()]
    if not urls:
        st.error("Please enter at least one Google Docs URL.")
        return

    creds = get_credentials()
    _run(urls, tab_mode, creds)


def _render_guide():
    with st.expander("How this works"):
        st.markdown("""
1. Authenticate with Google in **Settings** (requires Docs edit permission — re-authenticate if you haven't yet).
2. Paste one or more Google Docs URLs, one per line.
3. Choose whether to process only the tab from the URL or all document tabs.
4. Click **Remove Empty Rows**.

Only completely empty paragraphs (no text, no images, no special elements) are removed.
If a tab URL is provided, only that tab is processed. Otherwise the first tab is used.
        """.strip())


def _extract_doc_id(url: str) -> str:
    m = re.search(r"/document/d/([a-zA-Z0-9_-]+)", url)
    if not m:
        raise ValueError(f"No Google Docs file ID found in URL: {url}")
    return m.group(1)


def _extract_tab_id(url: str) -> str | None:
    parsed = urlparse(url)
    q = parse_qs(parsed.query)
    if "tab" in q:
        return q["tab"][0]
    frag_q = parse_qs(parsed.fragment.replace("?", "&"))
    if "tab" in frag_q:
        return frag_q["tab"][0]
    return None


def _flatten_tabs(doc: dict) -> list:
    out: list = []

    def _add(tab):
        out.append(tab)
        for child in tab.get("childTabs", []) or []:
            _add(child)

    for t in doc.get("tabs", []) or []:
        _add(t)
    return out


def _tab_id(tab: dict) -> str:
    return (tab.get("tabProperties", {}) or {}).get("tabId") or ""


def _is_empty_paragraph(element: dict) -> bool:
    para = element.get("paragraph")
    if not para:
        return False
    for pe in para.get("elements") or []:
        tr = pe.get("textRun")
        if tr:
            if tr.get("content", "").replace("\n", ""):
                return False
        elif pe.get("inlineObjectElement") or pe.get("autoText") or pe.get("pageBreak"):
            return False
    return True


def _collect_empty_ranges(content: list) -> list[dict]:
    ranges = []
    # Skip the last structural element (document end marker)
    items = content[:-1] if len(content) > 1 else []
    for element in items:
        if _is_empty_paragraph(element):
            start = element.get("startIndex")
            end = element.get("endIndex")
            if start is not None and end is not None:
                ranges.append({"startIndex": start, "endIndex": end})
    # Delete from bottom to top so earlier indices aren't shifted
    ranges.sort(key=lambda r: r["startIndex"], reverse=True)
    return ranges


def _delete_in_tab(docs_service, doc_id: str, tab_id: str | None, ranges: list[dict]) -> int:
    if not ranges:
        return 0
    requests_body = []
    for r in ranges:
        rng = {"startIndex": r["startIndex"], "endIndex": r["endIndex"]}
        if tab_id:
            rng["tabId"] = tab_id
        requests_body.append({"deleteContentRange": {"range": rng}})
    docs_service.documents().batchUpdate(
        documentId=doc_id, body={"requests": requests_body}
    ).execute()
    return len(requests_body)


def _process_doc(docs_service, url: str, tab_mode: str) -> dict:
    doc_id = _extract_doc_id(url)
    doc = docs_service.documents().get(
        documentId=doc_id, includeTabsContent=True
    ).execute()
    title = doc.get("title", "Untitled")
    all_tabs = _flatten_tabs(doc)

    if not all_tabs:
        return {"title": title, "url": url, "removed": 0, "error": "No tabs found"}

    if "All tabs" in tab_mode:
        chosen = all_tabs
    else:
        wanted = _extract_tab_id(url)
        if wanted:
            chosen = [t for t in all_tabs if _tab_id(t) == wanted] or [all_tabs[0]]
        else:
            chosen = [all_tabs[0]]

    total_removed = 0
    for tab in chosen:
        tid = _tab_id(tab) or None
        doc_tab = tab.get("documentTab", {}) or {}
        body_content = (doc_tab.get("body", {}) or {}).get("content") or []
        ranges = _collect_empty_ranges(body_content)
        removed = _delete_in_tab(docs_service, doc_id, tid, ranges)
        total_removed += removed

    return {"title": title, "url": url, "removed": total_removed, "error": None}


def _run(urls: list[str], tab_mode: str, creds):
    from googleapiclient.discovery import build
    from googleapiclient.errors import HttpError

    try:
        docs_service = build("docs", "v1", credentials=creds)
    except Exception as e:
        st.error(f"Failed to connect to Google Docs API: {e}")
        return

    results = []
    progress = st.progress(0, text="Processing…")
    status = st.empty()

    for i, url in enumerate(urls):
        progress.progress(i / len(urls), text=f"Processing {i + 1}/{len(urls)}…")
        status.caption(url)
        try:
            result = _process_doc(docs_service, url, tab_mode)
        except HttpError as e:
            if e.resp.status == 403:
                error_msg = (
                    "Permission denied (403). Your token may not have Docs edit access. "
                    "Sign out in Settings and re-authenticate to grant edit permission."
                )
            else:
                error_msg = str(e)
            fallback = url.split("/d/")[1].split("/")[0] if "/d/" in url else url
            result = {"title": fallback, "url": url, "removed": 0, "error": error_msg}
        except Exception as e:
            fallback = url.split("/d/")[1].split("/")[0] if "/d/" in url else url
            result = {"title": fallback, "url": url, "removed": 0, "error": str(e)}
        results.append(result)

    progress.progress(1.0, text="Done!")
    status.empty()

    total_removed = sum(r["removed"] for r in results)
    errors = [r for r in results if r["error"]]

    if errors:
        st.warning(f"Completed with {len(errors)} error(s). {total_removed} empty row(s) removed in total.")
    else:
        st.success(f"✅ Done! Removed {total_removed} empty row(s) across {len(results)} document(s).")

    df = pd.DataFrame([
        {
            "Document": r["title"],
            "Empty Rows Removed": r["removed"],
            "Status": f"❌ {r['error']}" if r["error"] else "✅ Success",
        }
        for r in results
    ])
    st.dataframe(df, use_container_width=True, hide_index=True)
