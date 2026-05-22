import json
import os
import re
import html
import pandas as pd
import requests
import streamlit as st
import streamlit.components.v1 as components
from collections import Counter
from concurrent.futures import ThreadPoolExecutor, as_completed
from urllib.parse import urlsplit, urlunsplit, unquote
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
            placeholder=(
                "https://www.example.com/page-1 | Page 1 title\n"
                "https://www.example.com/page-2"
            ),
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
4. Add optional page titles after each URL with `|`, tab, comma, or ` - `.
5. Click **Check Links**.
6. The app reads links from the document body, tables, headers, footers, footnotes, linked images, and rich links.
7. Review outputs: all links found, target URLs missing from the document, duplicate links, and DeepSeek anchor text suggestions for missing links.

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


def _extract_text(elements: list, chunks: list):
    """Recursively extract readable text from document elements."""
    for el in elements:
        if "paragraph" in el:
            text = []
            for pe in el["paragraph"].get("elements", []):
                if "textRun" in pe:
                    text.append(pe["textRun"].get("content", ""))
                elif "richLink" in pe:
                    props = pe["richLink"].get("richLinkProperties", {}) or {}
                    title = (props.get("title") or props.get("uri") or "").strip()
                    if title:
                        text.append(title)
            paragraph = "".join(text).strip()
            if paragraph:
                chunks.append(paragraph)
        elif "table" in el:
            for row in el["table"].get("tableRows", []):
                for cell in row.get("tableCells", []):
                    _extract_text(cell.get("content", []), chunks)
        elif "tableOfContents" in el:
            _extract_text(el["tableOfContents"].get("content", []), chunks)


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


def _parse_target_urls(urls_input: str) -> dict[str, str]:
    targets: dict[str, str] = {}
    for raw_line in urls_input.strip().splitlines():
        line = raw_line.strip()
        if not line:
            continue

        url_match = re.search(r"https?://\S+", line)
        if not url_match:
            continue

        raw_url = url_match.group(0).rstrip("),.;]")
        title = ""
        before = line[: url_match.start()].strip(" \t-|,:")
        after = line[url_match.end() :].strip(" \t-|,:")

        if after:
            title = after
        elif before:
            title = before

        targets[_normalize(raw_url)] = title or _title_from_url(raw_url)
    return targets


def _title_from_url(url: str) -> str:
    parts = urlsplit(url)
    path = unquote(parts.path.rstrip("/"))
    slug = path.rsplit("/", 1)[-1] if path else parts.netloc
    slug = re.sub(r"[-_]+", " ", slug)
    slug = re.sub(r"\s+", " ", slug).strip()
    return slug.title() if slug else url


def _document_text(doc: dict) -> str:
    chunks: list[str] = []
    _extract_text(doc.get("body", {}).get("content", []), chunks)
    for part in ("footnotes", "headers", "footers"):
        for item in doc.get(part, {}).values():
            _extract_text(item.get("content", []), chunks)
    return "\n\n".join(chunks)


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

    target_url_titles = _parse_target_urls(urls_input)
    target_urls = set(target_url_titles)
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
        "missing": [
            (u, target_url_titles.get(u, ""), status_codes.get(u, "N/A"))
            for u in missing
        ],
        "duplicates": [(u, c, status_codes.get(u, "N/A")) for u, c in duplicates],
        "deepseek_suggestions": _get_deepseek_suggestions(
            doc,
            [{"url": u, "title": target_url_titles.get(u, "")} for u in missing],
        ),
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
        "All links",
    )

    col1, col2 = st.columns(2)

    with col1:
        st.subheader("🚫 Missing Links")
        if missing:
            df_missing = pd.DataFrame(missing, columns=["URL", "Title", "Status Code"])
            df_missing = _filter_dataframe(
                df_missing,
                _render_filter(
                    "Filter missing links",
                    "check_links_filter_missing",
                    "Type part of a URL, title, or status code...",
                ),
            )
            _render_selectable_table(
                df_missing,
                "check_links_table_missing",
                "Missing links",
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
                "Duplicate links",
            )
        else:
            st.success("No duplicate links found.")

    suggestions = results.get("deepseek_suggestions")
    if suggestions:
        st.subheader("DeepSeek Suggestions for Missing Links")
        df_suggestions = _filter_dataframe(
            pd.DataFrame(suggestions),
            _render_filter(
                "Filter DeepSeek suggestions",
                "check_links_filter_deepseek",
                "Type part of a URL, anchor text, or recommendation...",
            ),
        )
        _render_selectable_table(
            df_suggestions,
            "check_links_table_deepseek",
            "DeepSeek suggestions",
        )


def _render_filter(label: str, key: str, placeholder: str) -> str:
    icon_col, input_col = st.columns([0.06, 0.94], vertical_alignment="bottom")
    with icon_col:
        st.markdown(
            """
            <div style="height: 38px; display: flex; align-items: center;">
                <svg width="18" height="18" viewBox="0 0 24 24" fill="none"
                     stroke="#4b5563" stroke-width="2" stroke-linecap="round"
                     stroke-linejoin="round" aria-label="Filter">
                    <polygon points="22 3 2 3 10 12.46 10 19 14 21 14 12.46 22 3"></polygon>
                </svg>
            </div>
            """,
            unsafe_allow_html=True,
        )
    with input_col:
        return st.text_input(label, key=key, placeholder=placeholder, label_visibility="collapsed")


def _with_status(rows: list, table_type: str) -> list:
    normalized = []
    for row in rows:
        if table_type == "missing":
            if isinstance(row, str):
                normalized.append((row, "", "N/A"))
            else:
                if len(row) >= 3:
                    normalized.append(row)
                elif len(row) == 2:
                    normalized.append((row[0], "", row[1]))
                else:
                    normalized.append((row[0], "", "N/A"))
        elif table_type == "duplicates":
            normalized.append(row if len(row) >= 3 else (row[0], row[1], "N/A"))
        else:
            normalized.append(row if len(row) >= 3 else (row[0], row[1], "N/A"))
    return normalized


def _render_selectable_table(df: pd.DataFrame, key: str, label: str):
    st.dataframe(
        df,
        use_container_width=True,
        hide_index=True,
        key=key,
        on_select="rerun",
        selection_mode="multi-row",
    )
    with st.expander(f"Copy cells from {label}"):
        components.html(
            _copy_cells_table_html(df, key),
            height=_copy_cells_table_height(df),
            scrolling=True,
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


def _copy_cells_table_height(df: pd.DataFrame) -> int:
    return min(520, max(220, 125 + (min(len(df), 10) * 36)))


def _copy_cells_table_html(df: pd.DataFrame, key: str) -> str:
    columns = [str(col) for col in df.columns]
    rows = df.astype(str).values.tolist()
    table_id = f"copy-cells-{key}"
    rows_json = json.dumps(rows)

    header_html = "".join(f"<th>{html.escape(col)}</th>" for col in columns)
    body_html = []
    for row_index, row in enumerate(rows):
        cells = []
        for col_index, value in enumerate(row):
            cells.append(
                f'<td data-row="{row_index}" data-col="{col_index}">{html.escape(value)}</td>'
            )
        body_html.append(f"<tr>{''.join(cells)}</tr>")

    return f"""
    <style>
        #{table_id} {{
            font-family: Arial, sans-serif;
            color: #111827;
        }}
        #{table_id} .copy-toolbar {{
            display: flex;
            align-items: center;
            gap: 8px;
            margin-bottom: 8px;
        }}
        #{table_id} button {{
            border: 1px solid #d1d5db;
            border-radius: 6px;
            background: #ffffff;
            color: #111827;
            padding: 6px 10px;
            cursor: pointer;
        }}
        #{table_id} .copy-status {{
            font-size: 12px;
            color: #4b5563;
        }}
        #{table_id} .copy-table-wrap {{
            max-height: 390px;
            overflow: auto;
            border: 1px solid #e5e7eb;
            border-radius: 8px;
        }}
        #{table_id} table {{
            width: 100%;
            border-collapse: collapse;
            table-layout: fixed;
            font-size: 13px;
        }}
        #{table_id} th,
        #{table_id} td {{
            border-bottom: 1px solid #e5e7eb;
            border-right: 1px solid #e5e7eb;
            padding: 8px;
            text-align: left;
            vertical-align: top;
            overflow-wrap: anywhere;
            user-select: none;
        }}
        #{table_id} th {{
            position: sticky;
            top: 0;
            background: #f9fafb;
            z-index: 1;
        }}
        #{table_id} td.selected {{
            background: #dbeafe;
            outline: 2px solid #2563eb;
            outline-offset: -2px;
        }}
    </style>
    <div id="{table_id}">
        <div class="copy-toolbar">
            <button type="button" class="copy-selected">Copy selected cells</button>
            <button type="button" class="clear-selected">Clear</button>
            <span class="copy-status">No cells selected.</span>
        </div>
        <div class="copy-table-wrap">
            <table>
                <thead><tr>{header_html}</tr></thead>
                <tbody>{''.join(body_html)}</tbody>
            </table>
        </div>
    </div>
    <script>
        const root = document.getElementById({json.dumps(table_id)});
        const data = {rows_json};
        const selected = new Set();
        const status = root.querySelector(".copy-status");

        function selectionKey(cell) {{
            return cell.dataset.row + "," + cell.dataset.col;
        }}

        root.querySelectorAll("td").forEach((cell) => {{
            cell.addEventListener("click", (event) => {{
                if (!event.ctrlKey && !event.metaKey) {{
                    selected.clear();
                    root.querySelectorAll("td.selected").forEach((el) => el.classList.remove("selected"));
                }}

                const key = selectionKey(cell);
                if (selected.has(key)) {{
                    selected.delete(key);
                    cell.classList.remove("selected");
                }} else {{
                    selected.add(key);
                    cell.classList.add("selected");
                }}
                status.textContent = selected.size + " cell(s) selected.";
            }});
        }});

        root.querySelector(".clear-selected").addEventListener("click", () => {{
            selected.clear();
            root.querySelectorAll("td.selected").forEach((el) => el.classList.remove("selected"));
            status.textContent = "No cells selected.";
        }});

        root.querySelector(".copy-selected").addEventListener("click", async () => {{
            const values = Array.from(selected).map((key) => {{
                const [row, col] = key.split(",").map(Number);
                return data[row][col];
            }});
            if (!values.length) {{
                status.textContent = "No cells selected.";
                return;
            }}

            try {{
                await navigator.clipboard.writeText(values.join("\\n"));
                status.textContent = "Copied " + values.length + " cell(s).";
            }} catch (error) {{
                status.textContent = values.join("\\n");
            }}
        }});
    </script>
    """


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


def _get_deepseek_suggestions(doc: dict, missing_targets: list[dict]) -> list[dict]:
    if not missing_targets:
        return []

    api_key = _private_value("DEEPSEEK_API_KEY")
    if not api_key:
        st.warning("Add DEEPSEEK_API_KEY to enable anchor text suggestions.")
        return []

    doc_text = _document_text(doc)
    with st.spinner("Asking DeepSeek for anchor text recommendations..."):
        return _suggest_missing_links(doc_text, missing_targets, api_key) or []


def _suggest_missing_links(doc_text: str, missing_targets: list[dict], api_key: str) -> list[dict] | None:
    links_block = "\n".join(
        f"{i + 1}. URL: {item['url']}\n   Title: {item.get('title') or '(no title)'}"
        for i, item in enumerate(missing_targets)
    )

    system_prompt = """You are an SEO editor recommending where to insert missing internal or external links into an existing article.

Rules:
1. Prefer an in-text link using natural anchor text that already appears in the content.
2. Do not use only the exact URL slug as anchor text. Make the anchor natural for readers.
3. If no relevant anchor text exists, suggest a slight edit to current text and show the edited anchor text.
4. If a URL is too hard to place naturally, mark it as "Hard to embed".
5. Each URL must be embedded only once.
6. Do not invent facts that are not supported by the content or URL title.

Return ONLY a valid JSON array. Each item must have:
{
  "url": "...",
  "title": "...",
  "status": "Use existing text" | "Slight edit needed" | "Hard to embed",
  "anchor_text": "...",
  "where_to_insert": "short quote or nearby sentence from the content",
  "recommendation": "short instruction for the editor"
}
Do not wrap JSON in markdown."""

    user_prompt = f"""Full Google Doc content:
{doc_text}

Missing URLs and titles:
{links_block}"""

    try:
        response = requests.post(
            "https://api.deepseek.com/chat/completions",
            headers={
                "Authorization": f"Bearer {api_key}",
                "Content-Type": "application/json",
            },
            json={
                "model": "deepseek-v4-pro",
                "messages": [
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": user_prompt},
                ],
                "thinking": {"type": "disabled"},
                "temperature": 0.2,
                "max_tokens": 3500,
                "stream": False,
            },
            timeout=90,
        )
        response.raise_for_status()
        data = response.json()
        raw = data["choices"][0]["message"]["content"].strip()
        raw = re.sub(r"^```(?:json)?\s*", "", raw)
        raw = re.sub(r"\s*```$", "", raw)
        parsed = json.loads(raw)
        if not isinstance(parsed, list):
            raise ValueError("DeepSeek did not return a JSON array.")
        return [_normalize_suggestion_row(item) for item in parsed]
    except Exception as exc:
        st.error(f"DeepSeek request failed: {exc}")
        return None


def _normalize_suggestion_row(item: dict) -> dict:
    return {
        "URL": str(item.get("url", "")).strip(),
        "Title": str(item.get("title", "")).strip(),
        "Status": str(item.get("status", "")).strip(),
        "Suggested Anchor Text": str(item.get("anchor_text", "")).strip(),
        "Where to Insert": str(item.get("where_to_insert", "")).strip(),
        "Recommendation": str(item.get("recommendation", "")).strip(),
    }


def _private_value(key: str) -> str:
    value = os.getenv(key, "").strip()
    if value:
        return value
    try:
        return str(st.secrets.get(key, "")).strip()
    except Exception:
        return ""
