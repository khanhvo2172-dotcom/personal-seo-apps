import json
import os
import re
import pandas as pd
import requests
import streamlit as st
import streamlit.components.v1 as components
from collections import Counter
from concurrent.futures import ThreadPoolExecutor, as_completed
from urllib.parse import urlsplit, urlunsplit, unquote, urlparse, parse_qs, urljoin
from bs4 import BeautifulSoup, NavigableString, Tag
from features.auth import get_credentials, require_auth

STATUS_CHECK_TIMEOUT = 8
STATUS_CHECK_WORKERS = 10
CLAUDE_DEFAULT_MODEL = "claude-sonnet-4-20250514"

BLOG_FETCH_TIMEOUT = 20
# Author-bio block that marks the end of the article body on trueprofit.io.
# The Next.js CSS-module class is hashed (e.g. "style_wrap-bio__oleBA"); the
# second, un-hashed class "wrap-bio" is the stable marker.
BIO_MARKER_CLASS = "wrap-bio"
# Auto-inserted Shopify App Store banner CTAs (image-wrapped, empty anchor).
# The skill defines these as site chrome, not editorial links, so they are
# filtered out of the blog-body link extraction.
RE_BANNER_CTA = re.compile(r"utm_campaign=in-blog-banner", re.IGNORECASE)

RE_ML_LINK = re.compile(
    r"https?://(?:www\.)?trueprofit\.io/(es|de|fr)/blog/([a-z0-9-]+)",
    re.IGNORECASE,
)
RE_EN_BLOG = re.compile(
    r"https?://(?:www\.)?trueprofit\.io/blog/([a-z0-9-]+)",
    re.IGNORECASE,
)
LANG_LABEL = {"es": "ES 🇪🇸", "de": "DE 🇩🇪", "fr": "FR 🇫🇷"}


SOURCE_DOC = "📄 Google Doc"
SOURCE_BLOG = "🌍 TrueProfit's Blog URL"


def render():
    st.header("Check Internal & External Links in Google Docs")
    st.caption(
        "Checks which of your target URLs appear in a Google Doc or a live "
        "TrueProfit blog page, and identifies missing or duplicate links."
    )
    _render_quick_guide()

    source = st.radio(
        "Where should I read the links from?",
        [SOURCE_DOC, SOURCE_BLOG],
        horizontal=True,
        key="check_links_source",
        help="A Google Doc source, or the live article body of a trueprofit.io/blog page.",
    )

    # Google auth is only needed to read a Google Doc; the blog mode is public.
    if source == SOURCE_DOC and not require_auth():
        return

    check_ml = st.checkbox("🌐 Check Multilingual Pages", key="check_ml")

    with st.form("check_links_form"):
        doc_url = ""
        blog_url = ""
        if source == SOURCE_DOC:
            doc_url = st.text_input(
                "Google Doc URL",
                placeholder="https://docs.google.com/document/d/.../edit",
            )
        else:
            blog_url = st.text_input(
                "TrueProfit's Blog URL",
                placeholder="https://trueprofit.io/blog/gross-profit-margin",
                help=(
                    "Reads only the article body: from the H1 heading down to "
                    "(but not including) the author bio, FAQ included. Nav, "
                    "footer, related posts and auto banner CTAs are skipped."
                ),
            )
        urls_input = st.text_area(
            "URLs to check — one per line",
            placeholder=(
                "https://www.example.com/page-1 | Page 1 title\n"
                "https://www.example.com/page-2"
            ),
            height=150,
        )
        ml_input = ""
        if st.session_state.get("check_ml"):
            ml_input = st.text_area(
                "Pages with multilingual versions — one per line",
                placeholder="gross-profit-margin\nhttps://trueprofit.io/blog/net-profit",
                help="Paste slugs or full blog URLs. The tool finds their /es/, /de/, /fr/ versions in the source.",
            )
        submitted = st.form_submit_button("\U0001f50d Check Links", type="primary")

    if submitted:
        if not urls_input.strip():
            st.error("Please provide at least one URL to check.")
            return

        if source == SOURCE_DOC:
            if not doc_url.strip():
                st.error("Please provide a Google Doc URL.")
                return
            results = _run_check(doc_url.strip(), urls_input, ml_input, check_ml)
        else:
            if not blog_url.strip():
                st.error("Please provide a TrueProfit blog URL.")
                return
            results = _run_check_blog(blog_url.strip(), urls_input, ml_input, check_ml)

        if results:
            st.session_state["check_links_results"] = results

    if "check_links_results" in st.session_state:
        _render_results(st.session_state["check_links_results"])


def _render_quick_guide():
    with st.expander("How this works"):
        st.markdown(
            """
**Pick a source:**

- **📄 Google Doc** — authenticate with Google in **Settings**, then paste the Doc URL. Links are read from the document body, tables, headers, footers, footnotes, linked images, and rich links. If the document has multiple tabs, paste the URL with the tab selected (e.g. `?tab=t.0`) to check that specific tab.
- **🌍 TrueProfit's Blog URL** — no Google login needed. Paste a live `trueprofit.io/blog/...` URL. The app fetches the page and reads links only from the **article body**: from the H1 heading down to (but not including) the author-bio block, with the FAQ section included. Navigation, header/footer, related-post blocks and auto-inserted banner CTAs (`utm_campaign=in-blog-banner-*`) are skipped.

**Then:**

1. Paste the internal or external URLs you expect to find, one URL per line.
2. Add optional page titles after each URL with `|`, tab, comma, or ` - `.
3. Click **Check Links**.
4. Review all links found, target URLs missing from the source, and duplicate links.
5. Use the DeepSeek or Claude 4.6 button only when you want anchor text suggestions for missing links.

Use this before publishing or updating SEO content to confirm important internal links and external citations are present.
            """.strip()
        )


# ── helpers ──────────────────────────────────────────────

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


def _parse_tab_id(url: str) -> str | None:
    """Extract tab ID from a Google Doc URL query parameter (e.g. ?tab=t.0)."""
    params = parse_qs(urlparse(url).query)
    tabs = params.get("tab", [])
    return tabs[0] if tabs else None


def _find_tab(tabs: list, tab_id: str) -> dict | None:
    """Recursively find a tab by tabId, including nested child tabs."""
    for tab in tabs:
        if tab.get("tabProperties", {}).get("tabId") == tab_id:
            return tab
        child = _find_tab(tab.get("childTabs", []), tab_id)
        if child:
            return child
    return None


def _get_tab_data(doc: dict, tab_id: str | None) -> tuple:
    """Return (body_content, footnotes, headers, footers) for the target tab.

    When the document has a tabs structure, select the tab whose tabId matches
    tab_id. If tab_id is None or not found, fall back to the first tab.
    For legacy single-tab documents (no tabs key), use doc["body"] directly.
    """
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


def _document_text(body_content: list, footnotes: dict, headers: dict, footers: dict) -> str:
    chunks: list[str] = []
    _extract_text(body_content, chunks)
    for part_dict in (footnotes, headers, footers):
        for item in part_dict.values():
            _extract_text(item.get("content", []), chunks)
    return "\n\n".join(chunks)


def _parse_ml_slugs(text: str) -> set:
    slugs = set()
    for line in text.strip().splitlines():
        line = line.strip()
        if not line:
            continue
        m = re.search(r"/blog/([a-z0-9-]+)", line, re.IGNORECASE)
        if m:
            slugs.add(m.group(1).lower())
        elif re.match(r"^[a-z0-9-]+$", line):
            slugs.add(line.lower())
    return slugs


def _find_ml_links(found_links: list, ml_slugs: set) -> list:
    results = []
    seen = set()
    for url, anchor in found_links:
        m = RE_ML_LINK.search(url)
        if not m:
            continue
        lang = m.group(1).lower()
        slug = m.group(2).lower()
        if ml_slugs and slug not in ml_slugs:
            continue
        key = (lang, slug)
        if key in seen:
            continue
        seen.add(key)
        results.append({
            "Language": LANG_LABEL.get(lang, lang.upper()),
            "URL": url,
            "Anchor Text": anchor or "—",
        })
    return sorted(results, key=lambda x: (x["Language"], x["URL"]))


def _find_english_ml_links(found_links: list, ml_slugs: set) -> list:
    results = []
    seen = set()
    for url, anchor in found_links:
        m = RE_EN_BLOG.search(url)
        if not m:
            continue
        slug = m.group(1).lower()
        if not ml_slugs or slug not in ml_slugs:
            continue
        if slug in seen:
            continue
        seen.add(slug)
        results.append({"URL": url, "Anchor Text": anchor or "—"})
    return sorted(results, key=lambda x: x["URL"])


def _run_check(doc_url: str, urls_input: str, ml_input: str = "", check_ml: bool = False):
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
            from googleapiclient.errors import HttpError

            docs = build("docs", "v1", credentials=creds)
            doc = docs.documents().get(documentId=doc_id, includeTabsContent=True).execute()
        except Exception as e:
            err = str(e)
            if "403" in err:
                st.error("Permission denied (403). Make sure you have at least Viewer access to this Google Doc.")
            elif "404" in err:
                st.error(f"Document not found (404). Check the URL.")
            else:
                st.error(f"Failed to fetch document: {e}")
            return None

    body_content, footnotes, headers, footers = _get_tab_data(doc, tab_id)

    # Collect links from body, headers, footers, footnotes
    raw: list = []
    _find_links(body_content, raw)
    for part_dict in (footnotes, headers, footers):
        for item in part_dict.values():
            _find_links(item.get("content", []), raw)

    doc_text = _document_text(body_content, footnotes, headers, footers)
    return _analyze_links(raw, urls_input, ml_input, check_ml, doc_text)


def _run_check_blog(blog_url: str, urls_input: str, ml_input: str = "", check_ml: bool = False):
    """Fetch a live TrueProfit blog page and analyze its article-body links.

    The article body range mirrors the compare-content skill: start at the H1
    heading, stop just before the author-bio block (``div.wrap-bio``), keep the
    FAQ, and skip site chrome and auto banner CTAs.
    """
    if not re.match(r"^https?://", blog_url, re.IGNORECASE):
        st.error("Invalid URL. Please paste the full blog URL, including https://.")
        return None

    with st.spinner("Fetching blog page…"):
        try:
            resp = requests.get(
                blog_url,
                timeout=BLOG_FETCH_TIMEOUT,
                headers={"User-Agent": "Mozilla/5.0"},
            )
            resp.raise_for_status()
        except requests.RequestException as e:
            st.error(f"Could not fetch the page: {e}")
            return None

    raw, article_text, bio_found = _extract_blog_article(resp.text, blog_url)

    if not raw:
        st.warning(
            "No links were found in the article body. Double-check the URL points "
            "to a live blog article."
        )
    elif not bio_found:
        st.info(
            "Couldn't find the author-bio marker on this page, so links were read "
            "from the H1 to the end of the page. Results may include footer links."
        )

    return _analyze_links(raw, urls_input, ml_input, check_ml, article_text)


def _has_class(target: str):
    """Return a bs4 class matcher that is True when *target* is one of the
    element's classes (handles the multi-class Next.js CSS-module lists)."""
    def _match(value):
        if not value:
            return False
        classes = value if isinstance(value, list) else str(value).split()
        return target in classes
    return _match


def _extract_blog_article(html: str, base_url: str) -> tuple[list, str, bool]:
    """Extract (links, article_text, bio_found) from a blog page's article body.

    Links are returned as (absolute_url, anchor_text) pairs, in reading order,
    for everything between the first <h1> and the author-bio block.
    """
    soup = BeautifulSoup(html, "html.parser")
    for tag in soup(["script", "style", "noscript", "template"]):
        tag.decompose()

    h1 = soup.find("h1")
    start = h1 or soup.body or soup
    bio = soup.find(class_=_has_class(BIO_MARKER_CLASS))

    links: list[tuple[str, str]] = []
    text_parts: list[str] = []
    for el in start.next_elements:
        if bio is not None and el is bio:
            break
        if isinstance(el, Tag) and el.name == "a":
            href = (el.get("href") or "").strip()
            if not href or href.startswith(("#", "javascript:", "mailto:", "tel:")):
                continue
            if RE_BANNER_CTA.search(href):
                continue
            full = urljoin(base_url, href)
            anchor = el.get_text(" ", strip=True)
            if not anchor and el.find("img"):
                anchor = "embedded in image"
            links.append((full, anchor))
        elif isinstance(el, NavigableString):
            chunk = str(el).strip()
            if chunk:
                text_parts.append(chunk)

    return links, " ".join(text_parts), bio is not None


def _analyze_links(raw: list, urls_input: str, ml_input: str, check_ml: bool, doc_text: str):
    """Shared analysis for both sources: compare found links against targets,
    detect duplicates, check status codes, and build the result dict."""
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

    ml_slugs = _parse_ml_slugs(ml_input) if check_ml else set()
    ml_links = _find_ml_links(normalized, ml_slugs) if check_ml else []
    english_ml_links = _find_english_ml_links(normalized, ml_slugs) if check_ml else []

    return {
        "unique": [(u, a, status_codes.get(u, "N/A")) for u, a in unique],
        "target_count": len(target_urls),
        "missing": [
            (u, target_url_titles.get(u, ""), status_codes.get(u, "N/A"))
            for u in missing
        ],
        "duplicates": [(u, c, status_codes.get(u, "N/A")) for u, c in duplicates],
        "deepseek_doc_text": doc_text,
        "deepseek_missing_targets": [
            {"url": u, "title": target_url_titles.get(u, "")}
            for u in missing
        ],
        "deepseek_suggestions": [],
        "claude_suggestions": [],
        "check_ml": check_ml,
        "multilingual_links": ml_links,
        "english_ml_links": english_ml_links,
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
    df_found = pd.DataFrame(unique, columns=["\U0001f517 Link", "\U0001f4ac Anchor Text", "Status Code"])
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
        copy_column="\U0001f517 Link",
    )

    col1, col2 = st.columns(2)

    with col1:
        st.subheader("\U0001f6ab Missing Links")
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
        st.subheader("\U0001f501 Duplicate Links (> 1 occurrence)")
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

    if results.get("check_ml"):
        ml_col, eng_col = st.columns(2)
        with ml_col:
            st.subheader("🌐 Multilingual Links Found in Document")
            ml_links = results.get("multilingual_links") or []
            if ml_links:
                df_ml = pd.DataFrame(ml_links)
                _render_selectable_table(df_ml, "check_links_table_ml", "Multilingual links", copy_column="URL")
            else:
                st.info("No multilingual links (ES/DE/FR) found for the provided pages.")
        with eng_col:
            st.subheader("⚠️ Still-English Links (multilingual version available)")
            eng_links = results.get("english_ml_links") or []
            if eng_links:
                df_eng = pd.DataFrame(eng_links)
                _render_selectable_table(df_eng, "check_links_table_eng_ml", "Still-English links", copy_column="URL")
            else:
                st.success("No English-version links found for pages that have multilingual versions.")

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
