import re
import pandas as pd
import streamlit as st
from urllib.parse import urlparse, parse_qs
from features.auth import get_credentials, require_auth


def render():
    st.header("Remove Empty Rows in Google Docs")
    st.caption("Removes empty paragraphs and optionally trims trailing spaces and soft-returns from one or more Google Docs files.")
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
        trim_spaces = st.checkbox(
            "Also trim trailing spaces and empty soft-returns within paragraphs",
            value=True,
            help="Removes invisible spaces/tabs and trailing soft-returns (Shift+Enter) at the end of each paragraph. A soft-return right before the paragraph break creates a visible empty row at the same indentation as the paragraph above it.",
        )
        submitted = st.form_submit_button("🗑️ Remove Empty Rows", type="primary")

    if not submitted:
        return

    urls = [u.strip() for u in urls_input.strip().splitlines() if u.strip()]
    if not urls:
        st.error("Please enter at least one Google Docs URL.")
        return

    creds = get_credentials()
    _run(urls, tab_mode, trim_spaces, creds)


def _render_guide():
    with st.expander("How this works"):
        st.markdown("""
1. Authenticate with Google in **Settings** (requires Docs edit permission — re-authenticate if you haven't yet).
2. Paste one or more Google Docs URLs, one per line.
3. Choose whether to process only the tab from the URL or all document tabs.
4. Click **Remove Empty Rows**.

Only completely empty paragraphs (no text, no images, no special elements) are removed.
Optionally, trailing spaces and soft-returns (Shift+Enter) at the end of each paragraph are also trimmed — this catches the case where a numbered-list item ends with a bare Shift+Enter, which creates a visible empty row at the list's indentation level.
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

    para_indices = [i for i, el in enumerate(content) if "paragraph" in el]
    if len(para_indices) <= 1:
        return []

    protected: set[int] = {para_indices[-1]}
    merge_into_prev: set[int] = set()

    for i, el in enumerate(content):
        is_structural = any(
            k in el for k in ("table", "sectionBreak", "tableOfContents")
        )
        if is_structural and i > 0 and "paragraph" in content[i - 1]:
            has_other_para = False
            for j in range(i - 2, -1, -1):
                if any(k in content[j] for k in ("table", "sectionBreak", "tableOfContents")):
                    break
                if "paragraph" in content[j] and not _is_empty_paragraph(content[j]):
                    has_other_para = True
                    break
            if has_other_para:
                merge_into_prev.add(i - 1)
            protected.add(i - 1)

    for i, element in enumerate(content):
        if i in protected:
            if i in merge_into_prev and _is_empty_paragraph(element):
                start = element.get("startIndex")
                if start is not None and start > 1:
                    ranges.append({"startIndex": start - 1, "endIndex": start})
            continue
        if _is_empty_paragraph(element):
            start = element.get("startIndex")
            end = element.get("endIndex")
            if start is not None and end is not None:
                ranges.append({"startIndex": start, "endIndex": end})

    seen: set[tuple[int, int]] = set()
    unique = []
    for r in ranges:
        key = (r["startIndex"], r["endIndex"])
        if key not in seen:
            seen.add(key)
            unique.append(r)

    unique.sort(key=lambda r: r["startIndex"], reverse=True)
    return unique


def _collect_trailing_space_ranges(content: list) -> list[dict]:
    """Find trailing whitespace (spaces/tabs before the newline) in paragraph text runs."""
    ranges = []
    for element in content:
        para = element.get("paragraph")
        if not para:
            continue
        elements = para.get("elements") or []
        if not elements:
            continue
        # Check the last text run in the paragraph
        last_el = elements[-1]
        tr = last_el.get("textRun")
        if not tr:
            continue
        text = tr.get("content", "")
        if not text.endswith("\n"):
            continue
        before_newline = text[:-1]
        stripped = before_newline.rstrip(" \t\x0b")
        trailing_len = len(before_newline) - len(stripped)
        if trailing_len > 0:
            end_idx = last_el.get("endIndex")
            if end_idx is not None:
                # Trailing spaces sit right before the \n character
                space_start = end_idx - 1 - trailing_len
                space_end = end_idx - 1
                ranges.append({"startIndex": space_start, "endIndex": space_end})
    ranges.sort(key=lambda r: r["startIndex"], reverse=True)
    return ranges


def _delete_ranges_in_tab(
    docs_service, doc_id: str, tab_id: str | None, ranges: list[dict]
) -> int:
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


def _process_doc(docs_service, url: str, tab_mode: str, trim_spaces: bool) -> dict:
    doc_id = _extract_doc_id(url)
    doc = docs_service.documents().get(
        documentId=doc_id, includeTabsContent=True
    ).execute()
    title = doc.get("title", "Untitled")
    all_tabs = _flatten_tabs(doc)

    if not all_tabs:
        return {"title": title, "url": url, "removed": 0, "trimmed": 0, "error": "No tabs found"}

    if "All tabs" in tab_mode:
        chosen = all_tabs
    else:
        wanted = _extract_tab_id(url)
        if wanted:
            chosen = [t for t in all_tabs if _tab_id(t) == wanted] or [all_tabs[0]]
        else:
            chosen = [all_tabs[0]]

    total_removed = 0
    total_trimmed = 0
    for tab in chosen:
        tid = _tab_id(tab) or None
        doc_tab = tab.get("documentTab", {}) or {}
        body_content = (doc_tab.get("body", {}) or {}).get("content") or []

        # Collect all deletion ranges — empty rows + trailing spaces
        empty_ranges = _collect_empty_ranges(body_content)
        space_ranges = _collect_trailing_space_ranges(body_content) if trim_spaces else []

        # Merge into one sorted list (both already reverse-sorted)
        # Ranges don't overlap (empty vs non-empty paragraphs), so merge + re-sort
        all_ranges = sorted(
            empty_ranges + space_ranges,
            key=lambda r: r["startIndex"],
            reverse=True,
        )

        deleted = _delete_ranges_in_tab(docs_service, doc_id, tid, all_ranges)
        total_removed += len(empty_ranges)
        total_trimmed += len(space_ranges)

    return {"title": title, "url": url, "removed": total_removed, "trimmed": total_trimmed, "error": None}


def _run(urls: list[str], tab_mode: str, trim_spaces: bool, creds):
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
            result = _process_doc(docs_service, url, tab_mode, trim_spaces)
        except HttpError as e:
            if e.resp.status == 403:
                error_msg = (
                    "Permission denied (403). Your token may not have Docs edit access. "
                    "Sign out in Settings and re-authenticate to grant edit permission."
                )
            else:
                error_msg = str(e)
            fallback = url.split("/d/")[1].split("/")[0] if "/d/" in url else url
            result = {"title": fallback, "url": url, "removed": 0, "trimmed": 0, "error": error_msg}
        except Exception as e:
            fallback = url.split("/d/")[1].split("/")[0] if "/d/" in url else url
            result = {"title": fallback, "url": url, "removed": 0, "trimmed": 0, "error": str(e)}
        results.append(result)

    progress.progress(1.0, text="Done!")
    status.empty()

    total_removed = sum(r["removed"] for r in results)
    total_trimmed = sum(r.get("trimmed", 0) for r in results)
    errors = [r for r in results if r["error"]]

    parts = []
    if total_removed:
        parts.append(f"{total_removed} empty row(s) removed")
    if total_trimmed:
        parts.append(f"{total_trimmed} paragraph(s) trimmed")
    summary = ", ".join(parts) if parts else "No changes needed"

    if errors:
        st.warning(f"Completed with {len(errors)} error(s). {summary}.")
    else:
        st.success(f"✅ Done! {summary} across {len(results)} document(s).")

    rows = []
    for r in results:
        row = {
            "Document": r["title"],
            "Empty Rows Removed": r["removed"],
        }
        if trim_spaces:
            row["Trailing Spaces Trimmed"] = r.get("trimmed", 0)
        row["Status"] = f"❌ {r['error']}" if r["error"] else "✅ Success"
        rows.append(row)

    df = pd.DataFrame(rows)
    st.dataframe(df, use_container_width=True, hide_index=True)
