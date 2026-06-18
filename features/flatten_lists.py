import re
import streamlit as st
from collections import defaultdict
from urllib.parse import urlparse, parse_qs
from features.auth import get_credentials, require_auth


def render():
    st.header("Flatten Numbered Lists in Google Docs")
    st.caption(
        "Converts numbered list formatting directly in the document: "
        "each list item keeps its number as plain text (1. 2. 3. …) "
        "and the list formatting is removed."
    )

    if not require_auth():
        return

    with st.form("flatten_lists_form"):
        doc_url = st.text_input(
            "Google Doc URL",
            placeholder="https://docs.google.com/document/d/.../edit?tab=t.0",
        )
        tab_scope = st.radio(
            "Which tabs to process?",
            options=["Just this tab", "All tabs"],
            horizontal=True,
        )
        submitted = st.form_submit_button("\U0001f4cb Flatten Numbered Lists", type="primary")

    if submitted:
        if not doc_url.strip():
            st.error("Please provide a Google Doc URL.")
            return
        _run_flatten(doc_url.strip(), all_tabs=(tab_scope == "All tabs"))


# ── main logic ─────────────────────────────────────────────────────────────────

def _run_flatten(doc_url: str, all_tabs: bool):
    m = re.search(r"/document/d/([a-zA-Z0-9_-]+)", doc_url)
    if not m:
        st.error("Invalid Google Doc URL. Please use the full URL from your browser.")
        return
    doc_id = m.group(1)
    tab_id = _parse_tab_id(doc_url)

    creds = get_credentials()

    with st.spinner("Fetching document…"):
        try:
            from googleapiclient.discovery import build
            docs_service = build("docs", "v1", credentials=creds)
            doc = docs_service.documents().get(
                documentId=doc_id, includeTabsContent=True
            ).execute()
        except Exception as e:
            err = str(e)
            if "403" in err:
                st.error("Permission denied (403). Make sure you have Editor access to this Google Doc.")
            elif "404" in err:
                st.error("Document not found (404). Check the URL.")
            else:
                st.error(f"Failed to fetch document: {e}")
            return

    all_doc_tabs = _collect_all_tabs(doc.get("tabs", []))
    use_legacy = not bool(doc.get("tabs", []))

    if all_tabs:
        target_tabs = all_doc_tabs
    else:
        # "Just this tab" — use the tab from the URL, or first tab if none specified
        if tab_id:
            target_tabs = [t for t in all_doc_tabs if t.get("tabProperties", {}).get("tabId") == tab_id]
            if not target_tabs:
                st.error(f"Tab '{tab_id}' not found in this document.")
                return
        else:
            target_tabs = all_doc_tabs[:1] if all_doc_tabs else []

    # Collect numbered list items
    items: list[dict] = []
    if use_legacy:
        lists_def = doc.get("lists", {})
        _collect_list_items(
            doc.get("body", {}).get("content", []),
            lists_def, {}, tab_id=None, items=items,
        )
    else:
        for tab in target_tabs:
            t_id = tab.get("tabProperties", {}).get("tabId", "")
            doc_tab = tab.get("documentTab", {})
            lists_def = doc_tab.get("lists", doc.get("lists", {}))
            _collect_list_items(
                doc_tab.get("body", {}).get("content", []),
                lists_def, {}, tab_id=t_id, items=items,
            )

    if not items:
        st.info("No numbered list items found in the selected tab(s).")
        return

    # Build batchUpdate requests — reverse order per tab so indices stay valid
    by_tab: dict[str | None, list] = defaultdict(list)
    for item in items:
        by_tab[item["tabId"]].append(item)

    requests: list[dict] = []
    for t_id, tab_items in by_tab.items():
        tab_items.sort(key=lambda x: -x["startIndex"])
        for item in tab_items:
            rng: dict = {"startIndex": item["startIndex"], "endIndex": item["endIndex"]}
            loc: dict = {"index": item["startIndex"]}
            if t_id is not None:
                rng["tabId"] = t_id
                loc["tabId"] = t_id
            requests.append({"deleteParagraphBullets": {"range": rng}})
            requests.append({"insertText": {"location": loc, "text": item["prefix"]}})

    scope_label = "all tabs" if all_tabs else f"tab '{tab_id or 'first'}'"
    with st.spinner(f"Applying {len(items)} change(s) across {scope_label}…"):
        try:
            docs_service.documents().batchUpdate(
                documentId=doc_id,
                body={"requests": requests},
            ).execute()
        except Exception as e:
            st.error(f"Failed to update document: {e}")
            return

    st.success(
        f"Done! Converted {len(items)} numbered list item(s) to plain text "
        f"across {scope_label}."
    )


# ── helpers ────────────────────────────────────────────────────────────────────

def _collect_list_items(
    elements: list, lists_def: dict, counters: dict,
    tab_id: str | None, items: list,
):
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
                    items.append({
                        "tabId": tab_id,
                        "startIndex": start_idx,
                        "endIndex": end_idx,
                        "prefix": prefix,
                    })
        elif "table" in el:
            for row in el["table"].get("tableRows", []):
                for cell in row.get("tableCells", []):
                    _collect_list_items(
                        cell.get("content", []), lists_def, counters, tab_id, items
                    )
        elif "tableOfContents" in el:
            _collect_list_items(
                el["tableOfContents"].get("content", []), lists_def, counters, tab_id, items
            )


def _format_glyph(num: int, glyph_type: str) -> str:
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


def _to_roman(num: int) -> str:
    vals = [1000, 900, 500, 400, 100, 90, 50, 40, 10, 9, 5, 4, 1]
    syms = ["M", "CM", "D", "CD", "C", "XC", "L", "XL", "X", "IX", "V", "IV", "I"]
    result = ""
    for v, s in zip(vals, syms):
        while num >= v:
            result += s
            num -= v
    return result


def _collect_all_tabs(tabs: list) -> list:
    result = []
    for tab in tabs:
        result.append(tab)
        result.extend(_collect_all_tabs(tab.get("childTabs", [])))
    return result


def _parse_tab_id(url: str) -> str | None:
    params = parse_qs(urlparse(url).query)
    tabs = params.get("tab", [])
    return tabs[0] if tabs else None
