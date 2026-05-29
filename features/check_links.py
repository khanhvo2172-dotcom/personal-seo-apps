import json
import os
import re
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
CLAUDE_DEFAULT_MODEL = "claude-sonnet-4-20250514"


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
7. Review all links found, target URLs missing from the document, and duplicate links.
8. Use the DeepSeek or Claude 4.6 button only when you want anchor text suggestions for missing links.

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
    """Recursively extract (url, anchor_text) pairs from document elements.

    Adjacent same-URL textRuns within a single paragraph are merged (they
    represent one hyperlink whose anchor text was split by formatting).
    Links in *different* paragraphs are kept separate so duplicate
    detection works correctly.
    """
    for el in elements:
        if "paragraph" in el:
            para_links: list[tuple[str, str]] = []
            for pe in el["paragraph"].get("elements", []):
                if "textRun" in pe:
                    tr = pe["textRun"]
                    link = tr.get("textStyle", {}).get("link")
                    if link and link.get("url"):
                        anchor = tr.get("content", "").replace("\n", " ").strip()
                        para_links.append((link["url"], anchor))
                if "inlineObjectElement" in pe:
                    link = pe["inlineObjectElement"].get("textStyle", {}).get("link")
                    if link and link.get("url"):
                        para_links.append((link["url"], "embedded in image"))
                if "richLink" in pe:
                    props = pe["richLink"].get("richLinkProperties", {}) or {}
                    if props.get("uri"):
                        para_links.append((props["uri"], (props.get("title") or "").strip()))
            found.extend(_merge_adjacent(para_links))
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

    normalized = [(n, a) for u, a in raw if (n := _normalize(u))]
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
        "deepseek_doc_text": _document_text(doc),
        "deepseek_missing_targets": [
            {"url": u, "title": target_url_titles.get(u, "")}
            for u in missing
        ],
        "deepseek_suggestions": [],
        "claude_suggestions": [],
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
    df_found.insert(0, "#", range(1, len(df_found) + 1))
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
        copy_column="🔗 Link",
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
                copy_column="URL",
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

    missing_targets = results.get("deepseek_missing_targets") or []
    if missing_targets:
        st.subheader("AI Suggestions for Missing Links")
        deepseek_col, claude_col = st.columns(2)
        with deepseek_col:
            if st.button("Ask DeepSeek for anchor text suggestions", type="secondary"):
                results["deepseek_suggestions"] = _get_deepseek_suggestions(
                    results.get("deepseek_doc_text", ""),
                    missing_targets,
                )
                st.session_state["check_links_results"] = results
        with claude_col:
            if st.button("Ask Claude 4.6 for anchor text suggestions", type="secondary"):
                results["claude_suggestions"] = _get_claude_suggestions(
                    results.get("deepseek_doc_text", ""),
                    missing_targets,
                )
                st.session_state["check_links_results"] = results

    suggestions = results.get("deepseek_suggestions")
    if suggestions:
        df_suggestions = _filter_dataframe(
            pd.DataFrame(suggestions),
            _render_filter(
                "Filter DeepSeek suggestions",
                "check_links_filter_deepseek",
                "Type part of a URL, title, status, or anchor text...",
            ),
        )
        _render_selectable_table(
            df_suggestions,
            "check_links_table_deepseek",
            "DeepSeek suggestions",
        )

    claude_suggestions = results.get("claude_suggestions")
    if claude_suggestions:
        st.subheader("Claude 4.6 Suggestions for Missing Links")
        df_claude_suggestions = _filter_dataframe(
            pd.DataFrame(claude_suggestions),
            _render_filter(
                "Filter Claude 4.6 suggestions",
                "check_links_filter_claude",
                "Type part of a URL, title, status, or anchor text...",
            ),
        )
        _render_selectable_table(
            df_claude_suggestions,
            "check_links_table_claude",
            "Claude 4.6 suggestions",
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


def _render_selectable_table(
    df: pd.DataFrame, key: str, label: str, copy_column: str | None = None
):
    if copy_column and copy_column in df.columns:
        event = st.dataframe(
            df,
            use_container_width=True,
            hide_index=True,
            key=key,
            on_select="rerun",
            selection_mode="multi-row",
        )
        selected_rows = (
            event.selection.rows
            if event and hasattr(event, "selection") and event.selection
            else []
        )
        if selected_rows:
            links = df.iloc[selected_rows][copy_column].tolist()
            links_text = "\n".join(str(l) for l in links)
            _render_copy_selected_button(links_text, key, len(links))
    else:
        _render_copy_button(df, key)
        st.dataframe(
            df,
            use_container_width=True,
            hide_index=True,
            key=key,
        )


def _render_copy_button(df: pd.DataFrame, key: str):
    tsv = df.to_csv(sep="\t", index=False)
    tsv_escaped = json.dumps(tsv)
    btn_id = f"copy-btn-{key}"
    components.html(
        f"""
        <button id="{btn_id}" style="
            border: 1px solid #d1d5db; border-radius: 6px; background: #ffffff;
            color: #374151; padding: 5px 12px; cursor: pointer; font-size: 13px;
            display: inline-flex; align-items: center; gap: 5px;
        ">
            <svg width="14" height="14" viewBox="0 0 24 24" fill="none"
                 stroke="currentColor" stroke-width="2" stroke-linecap="round"
                 stroke-linejoin="round">
                <rect x="9" y="9" width="13" height="13" rx="2"/>
                <path d="M5 15H4a2 2 0 0 1-2-2V4a2 2 0 0 1 2-2h9a2 2 0 0 1 2 2v1"/>
            </svg>
            <span>Copy table</span>
        </button>
        <script>
            document.getElementById({json.dumps(btn_id)}).addEventListener("click", async function() {{
                const data = {tsv_escaped};
                try {{
                    await navigator.clipboard.writeText(data);
                    this.querySelector("span").textContent = "Copied!";
                    setTimeout(() => this.querySelector("span").textContent = "Copy table", 1500);
                }} catch(e) {{
                    this.querySelector("span").textContent = "Failed";
                }}
            }});
        </script>
        """,
        height=38,
    )


def _render_copy_selected_button(links_text: str, key: str, count: int):
    escaped = json.dumps(links_text)
    btn_id = f"copy-sel-{key}"
    components.html(
        f"""
        <button id="{btn_id}" style="
            border: 1px solid #2563eb; border-radius: 6px; background: #eff6ff;
            color: #1d4ed8; padding: 5px 12px; cursor: pointer; font-size: 13px;
            display: inline-flex; align-items: center; gap: 5px;
        ">
            <svg width="14" height="14" viewBox="0 0 24 24" fill="none"
                 stroke="currentColor" stroke-width="2" stroke-linecap="round"
                 stroke-linejoin="round">
                <rect x="9" y="9" width="13" height="13" rx="2"/>
                <path d="M5 15H4a2 2 0 0 1-2-2V4a2 2 0 0 1 2-2h9a2 2 0 0 1 2 2v1"/>
            </svg>
            <span>Copy {count} selected link{"s" if count != 1 else ""}</span>
        </button>
        <script>
            document.getElementById({json.dumps(btn_id)}).addEventListener("click", async function() {{
                const data = {escaped};
                try {{
                    await navigator.clipboard.writeText(data);
                    this.querySelector("span").textContent = "Copied!";
                    setTimeout(() => this.querySelector("span").textContent = "Copy {count} selected link{"s" if count != 1 else ""}", 1500);
                }} catch(e) {{
                    this.querySelector("span").textContent = "Failed";
                }}
            }});
        </script>
        """,
        height=38,
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


def _get_deepseek_suggestions(doc_text: str, missing_targets: list[dict]) -> list[dict]:
    if not missing_targets:
        return []

    api_key = _private_value("DEEPSEEK_API_KEY")
    if not api_key:
        st.warning("Add DEEPSEEK_API_KEY to enable anchor text suggestions.")
        return []

    with st.spinner("Asking DeepSeek for anchor text recommendations..."):
        return _suggest_missing_links(doc_text, missing_targets, api_key) or []


def _get_claude_suggestions(doc_text: str, missing_targets: list[dict]) -> list[dict]:
    if not missing_targets:
        return []

    api_key = _private_value("ANTHROPIC_API_KEY")
    if not api_key:
        st.warning("Add ANTHROPIC_API_KEY to enable Claude 4.6 anchor text suggestions.")
        return []

    with st.spinner("Asking Claude 4.6 for anchor text recommendations..."):
        return _suggest_missing_links_with_claude(doc_text, missing_targets, api_key) or []


def _build_missing_links_prompt(doc_text: str, missing_targets: list[dict]) -> tuple[str, str]:
    links_block = "\n".join(
        f"{i + 1}. URL: {item['url']}\n   Title: {item.get('title') or '(no title)'}"
        for i, item in enumerate(missing_targets)
    )

    system_prompt = """You are an SEO editor recommending where to insert missing internal or external links into an existing article.

Rules:
1. Prefer an in-text link using natural anchor text that already appears in the content.
2. Do not use only the exact URL slug as anchor text. Make the anchor natural for readers.
3. The anchor text must match the target page's SEO intent based on its URL and title.
4. If no relevant anchor text exists, suggest a slight edit to current text and show the edited anchor text.
5. If a URL is too hard to place naturally, mark it as "Hard to embed".
6. Each URL must be embedded only once.
7. Do not invent facts that are not supported by the content or URL title.

Return ONLY a valid JSON array. Each item must have:
{
  "url": "...",
  "title": "...",
  "status": "Use existing text" | "Slight edit needed" | "Hard to embed",
  "anchor_text": "..."
}
Do not wrap JSON in markdown."""

    user_prompt = f"""Full Google Doc content:
{doc_text}

Missing URLs and titles:
{links_block}"""

    return system_prompt, user_prompt


def _suggest_missing_links(doc_text: str, missing_targets: list[dict], api_key: str) -> list[dict] | None:
    system_prompt, user_prompt = _build_missing_links_prompt(doc_text, missing_targets)

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
        return _parse_suggestion_rows(raw, "DeepSeek")
    except Exception as exc:
        st.error(f"DeepSeek request failed: {exc}")
        return None


def _suggest_missing_links_with_claude(
    doc_text: str,
    missing_targets: list[dict],
    api_key: str,
) -> list[dict] | None:
    system_prompt, user_prompt = _build_missing_links_prompt(doc_text, missing_targets)
    model = _private_value("ANTHROPIC_MODEL") or CLAUDE_DEFAULT_MODEL

    try:
        response = requests.post(
            "https://api.anthropic.com/v1/messages",
            headers={
                "x-api-key": api_key,
                "anthropic-version": "2023-06-01",
                "Content-Type": "application/json",
            },
            json={
                "model": model,
                "system": system_prompt,
                "messages": [
                    {"role": "user", "content": user_prompt},
                ],
                "temperature": 0.2,
                "max_tokens": 3500,
            },
            timeout=90,
        )
        response.raise_for_status()
        data = response.json()
        raw = "".join(
            block.get("text", "")
            for block in data.get("content", [])
            if block.get("type") == "text"
        )
        return _parse_suggestion_rows(raw, "Claude 4.6")
    except Exception as exc:
        st.error(f"Claude 4.6 request failed: {exc}")
        return None


def _parse_suggestion_rows(raw: str, provider: str) -> list[dict]:
    raw = raw.strip()
    raw = re.sub(r"^```(?:json)?\s*", "", raw)
    raw = re.sub(r"\s*```$", "", raw)
    parsed = json.loads(raw)
    if not isinstance(parsed, list):
        raise ValueError(f"{provider} did not return a JSON array.")
    return [_normalize_suggestion_row(item) for item in parsed]


def _normalize_suggestion_row(item: dict) -> dict:
    return {
        "URL": str(item.get("url", "")).strip(),
        "Title": str(item.get("title", "")).strip(),
        "Status": str(item.get("status", "")).strip(),
        "Suggested Anchor Text": str(item.get("anchor_text", "")).strip(),
    }


def _private_value(key: str) -> str:
    value = os.getenv(key, "").strip()
    if value:
        return value
    try:
        return str(st.secrets.get(key, "")).strip()
    except Exception:
        return ""
