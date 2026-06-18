import re
import pandas as pd
import streamlit as st
from collections import defaultdict
from urllib.parse import urlparse, parse_qs
from features.auth import get_credentials, require_auth


def render():
    st.header("Format Google Docs File")
    st.caption(
        "Apply one or more formatting operations across one or more Google Docs files."
    )

    if not require_auth():
        return

    with st.form("format_gdocs_form"):
        urls_input = st.text_area(
            "Google Docs URLs (one per line)",
            placeholder=(
                "https://docs.google.com/document/d/.../edit\n"
                "https://docs.google.com/document/d/.../edit?tab=t.abc123"
            ),
            height=160,
        )
        tab_mode = st.radio(
            "Choose Tabs to process",
            ["Tab from URL (or first tab if none specified)", "All tabs"],
            horizontal=True,
        )
        st.markdown("**Features to apply**")
        do_flatten = st.checkbox("Flatten Numbered Lists")
        do_remove_empty = st.checkbox("Remove Empty Rows")
        do_trim = st.checkbox(
            "Also trim trailing spaces and empty soft-returns within paragraphs",
            help=(
                "Removes invisible spaces/tabs and trailing soft-returns (Shift+Enter) "
                "at the end of each paragraph."
            ),
        )
        submitted = st.form_submit_button("\U0001f527 Format", type="primary")

    if not submitted:
        return

    if not do_flatten and not do_remove_empty and not do_trim:
        st.error("Please select at least one feature to apply.")
        return

    urls = [u.strip() for u in urls_input.strip().splitlines() if u.strip()]
    if not urls:
        st.error("Please enter at least one Google Docs URL.")
        return

    _run(urls, tab_mode, do_flatten, do_remove_empty, do_trim, get_credentials())


# ── orchestration ──────────────────────────────────────────────────────────────

def _run(urls, tab_mode, do_flatten, do_remove_empty, do_trim, creds):
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
            result = _process_doc(docs_service, url, tab_mode, do_flatten, do_remove_empty, do_trim)
        except HttpError as e:
            if e.resp.status == 403:
                msg = (
                    "Permission denied (403). Make sure the token has Docs edit access. "
                    "Sign out in Settings and re-authenticate."
                )
            else:
                msg = str(e)
            fallback = url.split("/d/")[1].split("/")[0] if "/d/" in url else url
            result = {"title": fallback, "url": url, "flattened": 0, "removed": 0, "trimmed": 0, "error": msg}
        except Exception as e:
            fallback = url.split("/d/")[1].split("/")[0] if "/d/" in url else url
            result = {"title": fallback, "url": url, "flattened": 0, "removed": 0, "trimmed": 0, "error": str(e)}
        results.append(result)

    progress.progress(1.0, text="Done!")
    status.empty()

    errors = [r for r in results if r["error"]]
    parts = []
    if do_flatten:
        n = sum(r["flattened"] for r in results)
        if n:
            parts.append(f"{n} list item(s) flattened")
    if do_remove_empty:
        n = sum(r["removed"] for r in results)
        if n:
            parts.append(f"{n} empty row(s) removed")
    if do_trim:
        n = sum(r["trimmed"] for r in results)
        if n:
            parts.append(f"{n} paragraph(s) trimmed")
    summary = ", ".join(parts) if parts else "No changes needed"

    if errors:
        st.warning(f"Completed with {len(errors)} error(s). {summary}.")
    else:
        st.success(f"✅ Done! {summary} across {len(results)} document(s).")

    rows = []
    for r in results:
        row = {"Document": r["title"]}
        if do_flatten:
            row["Lists Flattened"] = r["flattened"]
        if do_remove_empty:
            row["Empty Rows Removed"] = r["removed"]
        if do_trim:
            row["Paragraphs Trimmed"] = r["trimmed"]
        row["Status"] = f"❌ {r['error']}" if r["error"] else "✅ Success"
        rows.append(row)

    st.dataframe(pd.DataFrame(rows), use_container_width=True, hide_index=True)


def _process_doc(docs_service, url, tab_mode, do_flatten, do_remove_empty, do_trim):
    doc_id = _extract_doc_id(url)
    doc = docs_service.documents().get(documentId=doc_id, includeTabsContent=True).execute()
    title = doc.get("title", "Untitled")

    target_tabs = _pick_tabs(doc, url, tab_mode)
    if target_tabs is None:
        return {"title": title, "url": url, "flattened": 0, "removed": 0, "trimmed": 0, "error": "No tabs found"}

    total_flattened = 0
    total_removed = 0
    total_trimmed = 0

    # ── step 1: flatten numbered lists ────────────────────────────────────────
    if do_flatten:
        items = []
        use_legacy = not bool(doc.get("tabs", []))
        if use_legacy:
            lists_def = doc.get("lists", {})
            _collect_flatten_items(
                doc.get("body", {}).get("content", []),
                lists_def, {}, tab_id=None, items=items,
            )
        else:
            for tab in target_tabs:
                t_id = _tab_id(tab) or None
                doc_tab = tab.get("documentTab", {})
                lists_def = doc_tab.get("lists", doc.get("lists", {}))
                _collect_flatten_items(
                    doc_tab.get("body", {}).get("content", []),
                    lists_def, {}, tab_id=t_id, items=items,
                )

        if items:
            by_tab = defaultdict(list)
            for item in items:
                by_tab[item["tabId"]].append(item)

            requests = []
            for t_id, tab_items in by_tab.items():
                tab_items.sort(key=lambda x: -x["startIndex"])
                for item in tab_items:
                    rng = {"startIndex": item["startIndex"], "endIndex": item["endIndex"]}
                    loc = {"index": item["startIndex"]}
                    if t_id is not None:
                        rng["tabId"] = t_id
                        loc["tabId"] = t_id
                    requests.append({"deleteParagraphBullets": {"range": rng}})
                    requests.append({"insertText": {"location": loc, "text": item["prefix"]}})

            docs_service.documents().batchUpdate(
                documentId=doc_id, body={"requests": requests}
            ).execute()
            total_flattened = len(items)

            # Re-fetch so empty-row indices are correct after text was inserted
            if do_remove_empty or do_trim:
                doc = docs_service.documents().get(documentId=doc_id, includeTabsContent=True).execute()
                target_tabs = _pick_tabs(doc, url, tab_mode)

    # ── step 2: remove empty rows + trim ──────────────────────────────────────
    if do_remove_empty or do_trim:
        use_legacy = not bool(doc.get("tabs", []))
        if use_legacy:
            body_content = doc.get("body", {}).get("content", [])
            empty_ranges = _collect_empty_ranges(body_content) if do_remove_empty else []
            space_ranges = _collect_trailing_space_ranges(body_content) if do_trim else []
            all_ranges = sorted(empty_ranges + space_ranges, key=lambda r: r["startIndex"], reverse=True)
            if all_ranges:
                _delete_ranges_in_tab(docs_service, doc_id, None, all_ranges)
            total_removed += len(empty_ranges)
            total_trimmed += len(space_ranges)
        else:
            for tab in (target_tabs or []):
                t_id = _tab_id(tab) or None
                body_content = (tab.get("documentTab", {}).get("body", {}) or {}).get("content", [])
                empty_ranges = _collect_empty_ranges(body_content) if do_remove_empty else []
                space_ranges = _collect_trailing_space_ranges(body_content) if do_trim else []
                all_ranges = sorted(empty_ranges + space_ranges, key=lambda r: r["startIndex"], reverse=True)
                if all_ranges:
                    _delete_ranges_in_tab(docs_service, doc_id, t_id, all_ranges)
                total_removed += len(empty_ranges)
                total_trimmed += len(space_ranges)

    return {"title": title, "url": url, "flattened": total_flattened, "removed": total_removed, "trimmed": total_trimmed, "error": None}


# ── flatten helpers ────────────────────────────────────────────────────────────

def _collect_flatten_items(elements, lists_def, counters, tab_id, items):
    for el in elements:
        if "paragraph" in el:
            para = el["paragraph"]
            start_idx = el.get("startIndex", 0)
            end_idx = el.get("endIndex", 0)
            bullet = para.get("bullet")
            if bullet:
                list_id = bullet.get("listId", "")
                nesting = bullet.get("nestingLevel", 0)
                nesting_levels = (
                    lists_def.get(list_id, {})
                    .get("listProperties", {})
                    .get("nestingLevels", [])
                )
                ld = nesting_levels[nesting] if nesting < len(nesting_levels) else {}
                glyph_type = ld.get("glyphType", "")
                glyph_format = ld.get("glyphFormat", "%0.")
                start_number = ld.get("startNumber", 1)
                key = (list_id, nesting)
                if glyph_type and glyph_type != "GLYPH_TYPE_UNSPECIFIED":
                    if key not in counters:
                        counters[key] = start_number
                    else:
                        counters[key] += 1
                    num = counters[key]
                    glyph = _format_glyph(num, glyph_type)
                    prefix = glyph_format.replace(f"%{nesting}", glyph) + " "
                    items.append({"tabId": tab_id, "startIndex": start_idx, "endIndex": end_idx, "prefix": prefix})
        elif "table" in el:
            for row in el["table"].get("tableRows", []):
                for cell in row.get("tableCells", []):
                    _collect_flatten_items(cell.get("content", []), lists_def, counters, tab_id, items)
        elif "tableOfContents" in el:
            _collect_flatten_items(el["tableOfContents"].get("content", []), lists_def, counters, tab_id, items)


def _format_glyph(num, glyph_type):
    if glyph_type == "DECIMAL":
        return str(num)
    if glyph_type == "ZERO_DECIMAL":
        return f"{num:02d}"
    if glyph_type in ("ALPHA", "UPPER_ALPHA"):
        result, n = "", num
        while n > 0:
            n, rem = divmod(n - 1, 26)
            result = chr(ord("a") + rem) + result
        return result.upper() if glyph_type == "UPPER_ALPHA" else result
    if glyph_type in ("ROMAN", "UPPER_ROMAN"):
        roman = _to_roman(num)
        return roman if glyph_type == "UPPER_ROMAN" else roman.lower()
    return str(num)


def _to_roman(num):
    vals = [1000, 900, 500, 400, 100, 90, 50, 40, 10, 9, 5, 4, 1]
    syms = ["M", "CM", "D", "CD", "C", "XC", "L", "XL", "X", "IX", "V", "IV", "I"]
    result = ""
    for v, s in zip(vals, syms):
        while num >= v:
            result += s
            num -= v
    return result


# ── empty-row / trim helpers ───────────────────────────────────────────────────

def _collect_empty_ranges(content):
    ranges = []
    para_indices = [i for i, el in enumerate(content) if "paragraph" in el]
    if len(para_indices) <= 1:
        return []
    protected = {para_indices[-1]}
    merge_into_prev = set()
    for i, el in enumerate(content):
        is_structural = any(k in el for k in ("table", "sectionBreak", "tableOfContents"))
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
    seen = set()
    unique = []
    for r in ranges:
        key = (r["startIndex"], r["endIndex"])
        if key not in seen:
            seen.add(key)
            unique.append(r)
    unique.sort(key=lambda r: r["startIndex"], reverse=True)
    return unique


def _is_empty_paragraph(element):
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


def _collect_trailing_space_ranges(content):
    ranges = []
    for element in content:
        para = element.get("paragraph")
        if not para:
            continue
        elements = para.get("elements") or []
        if not elements:
            continue
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
                space_start = end_idx - 1 - trailing_len
                space_end = end_idx - 1
                ranges.append({"startIndex": space_start, "endIndex": space_end})
    ranges.sort(key=lambda r: r["startIndex"], reverse=True)
    return ranges


def _delete_ranges_in_tab(docs_service, doc_id, tab_id, ranges):
    if not ranges:
        return
    requests_body = []
    for r in ranges:
        rng = {"startIndex": r["startIndex"], "endIndex": r["endIndex"]}
        if tab_id:
            rng["tabId"] = tab_id
        requests_body.append({"deleteContentRange": {"range": rng}})
    docs_service.documents().batchUpdate(
        documentId=doc_id, body={"requests": requests_body}
    ).execute()


# ── tab / URL helpers ──────────────────────────────────────────────────────────

def _pick_tabs(doc, url, tab_mode):
    all_tabs = _flatten_tabs(doc)
    if not all_tabs:
        return None
    if "All tabs" in tab_mode:
        return all_tabs
    wanted = _extract_tab_id(url)
    if wanted:
        return [t for t in all_tabs if _tab_id(t) == wanted] or [all_tabs[0]]
    return [all_tabs[0]]


def _flatten_tabs(doc):
    out = []
    def _add(tab):
        out.append(tab)
        for child in tab.get("childTabs", []) or []:
            _add(child)
    for t in doc.get("tabs", []) or []:
        _add(t)
    return out


def _tab_id(tab):
    return (tab.get("tabProperties", {}) or {}).get("tabId") or ""


def _extract_doc_id(url):
    m = re.search(r"/document/d/([a-zA-Z0-9_-]+)", url)
    if not m:
        raise ValueError(f"No Google Docs file ID found in URL: {url}")
    return m.group(1)


def _extract_tab_id(url):
    parsed = urlparse(url)
    q = parse_qs(parsed.query)
    if "tab" in q:
        return q["tab"][0]
    frag_q = parse_qs(parsed.fragment.replace("?", "&"))
    if "tab" in frag_q:
        return frag_q["tab"][0]
    return None
