import json
import re
import streamlit as st
import streamlit.components.v1 as components
from urllib.parse import urlparse, parse_qs
from features.auth import get_credentials, require_auth


def render():
    st.header("Flatten Numbered Lists in Google Docs")
    st.caption(
        "Reads a Google Doc and outputs its text with numbered list items "
        "converted to plain text prefixed with their numbers (1. 2. 3. …)."
    )

    if not require_auth():
        return

    with st.form("flatten_lists_form"):
        doc_url = st.text_input(
            "Google Doc URL",
            placeholder="https://docs.google.com/document/d/.../edit",
        )
        submitted = st.form_submit_button("\U0001f4cb Convert", type="primary")

    if submitted:
        if not doc_url.strip():
            st.error("Please provide a Google Doc URL.")
            return
        result = _run_convert(doc_url.strip())
        if result is not None:
            st.session_state["flatten_lists_result"] = result

    if "flatten_lists_result" in st.session_state:
        _render_result(st.session_state["flatten_lists_result"])


def _render_result(text: str):
    st.subheader("Converted Text")
    _render_copy_button(text)
    st.text_area(
        "",
        value=text,
        height=500,
        key="flatten_lists_output",
        label_visibility="collapsed",
    )


def _run_convert(doc_url: str) -> str | None:
    m = re.search(r"/document/d/([a-zA-Z0-9_-]+)", doc_url)
    if not m:
        st.error("Invalid Google Doc URL. Please use the full URL from your browser.")
        return None
    doc_id = m.group(1)
    tab_id = _parse_tab_id(doc_url)

    creds = get_credentials()

    with st.spinner("Fetching document…"):
        try:
            from googleapiclient.discovery import build

            docs = build("docs", "v1", credentials=creds)
            doc = docs.documents().get(documentId=doc_id, includeTabsContent=True).execute()
        except Exception as e:
            err = str(e)
            if "403" in err:
                st.error("Permission denied (403). Make sure you have at least Viewer access.")
            elif "404" in err:
                st.error("Document not found (404). Check the URL.")
            else:
                st.error(f"Failed to fetch document: {e}")
            return None

    body_content, _, _, _ = _get_tab_data(doc, tab_id)
    lists_def = doc.get("lists", {})

    lines: list[str] = []
    counters: dict[tuple, int] = {}
    _process_elements(body_content, lists_def, counters, lines)

    return "\n".join(lines)


# ── conversion helpers ─────────────────────────────────────────────────────────

def _process_elements(elements: list, lists_def: dict, counters: dict, lines: list):
    for el in elements:
        if "paragraph" in el:
            para = el["paragraph"]
            text = _paragraph_text(para)
            bullet = para.get("bullet")
            if bullet:
                list_id = bullet.get("listId", "")
                nesting = bullet.get("nestingLevel", 0)
                indent = "  " * nesting

                level_def = (
                    lists_def
                    .get(list_id, {})
                    .get("listProperties", {})
                    .get("nestingLevels", [{}] * (nesting + 1))
                )
                ld = level_def[nesting] if nesting < len(level_def) else {}

                glyph_type = ld.get("glyphType", "")
                glyph_symbol = ld.get("glyphSymbol", "")
                glyph_format = ld.get("glyphFormat", "%0.")
                start_number = ld.get("startNumber", 1)

                key = (list_id, nesting)

                if glyph_type and glyph_type != "GLYPH_TYPE_UNSPECIFIED":
                    # Ordered list — increment counter
                    if key not in counters:
                        counters[key] = start_number
                    else:
                        counters[key] += 1
                    num = counters[key]
                    glyph = _format_glyph(num, glyph_type)
                    prefix = glyph_format.replace(f"%{nesting}", glyph)
                    lines.append(f"{indent}{prefix} {text}")
                else:
                    # Unordered/bullet list
                    symbol = glyph_symbol or "•"
                    lines.append(f"{indent}{symbol} {text}")
            else:
                if text:
                    lines.append(text)
        elif "table" in el:
            for row in el["table"].get("tableRows", []):
                for cell in row.get("tableCells", []):
                    _process_elements(cell.get("content", []), lists_def, counters, lines)
        elif "tableOfContents" in el:
            _process_elements(el["tableOfContents"].get("content", []), lists_def, counters, lines)


def _paragraph_text(para: dict) -> str:
    parts = []
    for pe in para.get("elements", []):
        if "textRun" in pe:
            parts.append(pe["textRun"].get("content", ""))
        elif "richLink" in pe:
            props = pe["richLink"].get("richLinkProperties", {}) or {}
            title = (props.get("title") or props.get("uri") or "").strip()
            if title:
                parts.append(title)
    return "".join(parts).rstrip("\n")


def _format_glyph(num: int, glyph_type: str) -> str:
    if glyph_type == "DECIMAL":
        return str(num)
    if glyph_type == "ZERO_DECIMAL":
        return f"{num:02d}"
    if glyph_type in ("ALPHA", "UPPER_ALPHA"):
        result = ""
        n = num
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


# ── copy button ────────────────────────────────────────────────────────────────

def _render_copy_button(text: str):
    escaped = json.dumps(text)
    components.html(
        f"""
        <button id="copy-flatten-btn" style="
            border: 1px solid #d1d5db; border-radius: 6px; background: #ffffff;
            color: #374151; padding: 5px 12px; cursor: pointer; font-size: 13px;
            display: inline-flex; align-items: center; gap: 5px; margin-bottom: 4px;
        ">
            <svg width="14" height="14" viewBox="0 0 24 24" fill="none"
                 stroke="currentColor" stroke-width="2" stroke-linecap="round"
                 stroke-linejoin="round">
                <rect x="9" y="9" width="13" height="13" rx="2"/>
                <path d="M5 15H4a2 2 0 0 1-2-2V4a2 2 0 0 1 2-2h9a2 2 0 0 1 2 2v1"/>
            </svg>
            <span>Copy text</span>
        </button>
        <script>
            document.getElementById("copy-flatten-btn").addEventListener("click", async function() {{
                try {{
                    await navigator.clipboard.writeText({escaped});
                    this.querySelector("span").textContent = "Copied!";
                    setTimeout(() => this.querySelector("span").textContent = "Copy text", 1500);
                }} catch(e) {{
                    this.querySelector("span").textContent = "Failed";
                }}
            }});
        </script>
        """,
        height=44,
    )


# ── tab helpers (mirrors check_links.py) ──────────────────────────────────────

def _parse_tab_id(url: str) -> str | None:
    params = parse_qs(urlparse(url).query)
    tabs = params.get("tab", [])
    return tabs[0] if tabs else None


def _find_tab(tabs: list, tab_id: str) -> dict | None:
    for tab in tabs:
        if tab.get("tabProperties", {}).get("tabId") == tab_id:
            return tab
        child = _find_tab(tab.get("childTabs", []), tab_id)
        if child:
            return child
    return None


def _get_tab_data(doc: dict, tab_id: str | None) -> tuple:
    tabs = doc.get("tabs", [])
    if tabs:
        target_tab = _find_tab(tabs, tab_id) if tab_id else None
        if target_tab is None:
            target_tab = tabs[0]
        doc_tab = target_tab.get("documentTab", {})
        return (
            doc_tab.get("body", {}).get("content", []),
            doc_tab.get("footnotes", {}),
            doc_tab.get("headers", {}),
            doc_tab.get("footers", {}),
        )
    return (
        doc.get("body", {}).get("content", []),
        doc.get("footnotes", {}),
        doc.get("headers", {}),
        doc.get("footers", {}),
    )
