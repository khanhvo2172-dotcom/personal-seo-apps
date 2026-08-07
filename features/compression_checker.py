import html as _html
import json
import pandas as pd
import requests
import streamlit as st
import streamlit.components.v1 as components
from concurrent.futures import ThreadPoolExecutor, as_completed
from urllib.parse import urlsplit

MAX_WORKERS = 10
REQUEST_TIMEOUT = 25
# Cap how much of an uncompressed page we pull just to size it, so a rogue
# multi-hundred-MB response can't hang the tool. 15 MB is plenty to prove a
# page is far too big to be served uncompressed.
MAX_READ = 15 * 1024 * 1024

# What a real crawler advertises — this is the header a broken proxy strips.
ACCEPT_ENCODING = "gzip, deflate, br, zstd"

# Encodings that count as "compressed". Anything else (blank, "identity") means
# the server sent the raw, uncompressed bytes.
_COMPRESSED_ENCODINGS = {"gzip", "br", "deflate", "zstd", "compress"}

# User-Agent strings for the bots people care about. Testing several matters
# because some servers / WAFs compress (or block) differently depending on who
# is asking — that difference is invisible from a normal browser.
BOT_AGENTS = {
    "Googlebot": "Mozilla/5.0 AppleWebKit/537.36 (KHTML, like Gecko; compatible; Googlebot/2.1; +http://www.google.com/bot.html) Chrome/120.0.0.0 Safari/537.36",
    "Bingbot": "Mozilla/5.0 AppleWebKit/537.36 (KHTML, like Gecko; compatible; bingbot/2.0; +http://www.bing.com/bingbot.htm) Chrome/120.0.0.0 Safari/537.36",
    "ChatGPT (GPTBot)": "Mozilla/5.0 AppleWebKit/537.36 (KHTML, like Gecko; compatible; GPTBot/1.1; +https://openai.com/gptbot)",
    "ChatGPT (OAI-SearchBot)": "Mozilla/5.0 AppleWebKit/537.36 (KHTML, like Gecko; compatible; OAI-SearchBot/1.0; +https://openai.com/searchbot)",
    "Claude (ClaudeBot)": "Mozilla/5.0 AppleWebKit/537.36 (KHTML, like Gecko; compatible; ClaudeBot/1.0; +claudebot@anthropic.com)",
    "Gemini (Google-Extended)": "Mozilla/5.0 AppleWebKit/537.36 (KHTML, like Gecko; compatible; Google-Extended/1.0; +https://developers.google.com/search/docs/crawling-indexing/overview-google-crawlers)",
    "Web browser (Chrome)": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
}

DEFAULT_BOTS = [
    "Googlebot",
    "Bingbot",
    "ChatGPT (GPTBot)",
    "Claude (ClaudeBot)",
    "Gemini (Google-Extended)",
]


def render():
    st.header("Page Compression Checker")
    st.caption(
        "Check whether your pages are served gzip/Brotli-compressed to search "
        "and AI crawlers. An uncompressed page can be 5–8× larger, wasting crawl "
        "budget — and Search Console won't warn you about it."
    )
    _render_quick_guide()

    with st.form("compression_checker_form"):
        urls_raw = st.text_area(
            "URLs — one per line",
            placeholder="https://trueprofit.io/\nhttps://trueprofit.io/blog/\ntrueprofit.io/pricing",
            height=200,
        )
        bots = st.multiselect(
            "Crawlers to test (each URL is checked as every selected crawler)",
            options=list(BOT_AGENTS.keys()),
            default=DEFAULT_BOTS,
            help="A server can compress for one crawler and not another, so testing several is worth it.",
        )
        submitted = st.form_submit_button("🗜️ Check compression", type="primary")

    if not submitted:
        return

    urls = _collect_urls(urls_raw)
    if not urls:
        st.error("Please paste at least one URL.")
        return
    if not bots:
        st.error("Please select at least one crawler to test.")
        return

    _run_check(urls, bots)


def _render_quick_guide():
    with st.expander("How this works"):
        st.markdown(
            """
1. Paste your URLs, **one per line** (a missing `https://` is added for you).
2. Pick which **crawlers** to imitate — each URL is fetched once per crawler.
3. Click **Check compression**.

For every request the tool sends `Accept-Encoding: gzip, deflate, br, zstd`
(exactly what a real crawler sends) plus that crawler's User-Agent, then reads
the response's `Content-Encoding` header:

- If it comes back `gzip`, `br` (Brotli) or `zstd` → **compressed** ✅
- If it's missing → **not compressed** 🚩 — the crawler is downloading the full,
  heavy page. The tool measures the actual size so you can see the impact.

**Why it matters:** a reverse proxy or CDN misconfiguration can silently strip
compression for crawlers only. Nothing in Google Search Console reports it — the
only way to catch it is to fetch your pages from outside, exactly like this.

**Fix when a page is flagged:** in Cloudflare, check **Speed → Optimization**
(compression). If you added another proxy/CDN in front of the site, make sure it
forwards the `Accept-Encoding` header instead of stripping it.
            """.strip()
        )


# ── input parsing ────────────────────────────────────────────

def _normalize_url(item: str) -> str:
    """Trim, drop blanks, and add a scheme if the user omitted it."""
    cleaned = item.strip()
    if not cleaned:
        return ""
    if "://" not in cleaned:
        cleaned = "https://" + cleaned
    # Basic sanity check — must have a host.
    try:
        if not urlsplit(cleaned).netloc:
            return ""
    except Exception:
        return ""
    return cleaned


def _collect_urls(text: str) -> list[str]:
    seen: set[str] = set()
    urls: list[str] = []
    for line in (text or "").splitlines():
        url = _normalize_url(line)
        if url and url not in seen:
            seen.add(url)
            urls.append(url)
    return urls


# ── fetching ─────────────────────────────────────────────────

def _check_one(url: str, bot_label: str) -> dict:
    """Fetch one URL as one crawler and classify the response."""
    headers = {
        "User-Agent": BOT_AGENTS[bot_label],
        "Accept-Encoding": ACCEPT_ENCODING,
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    }
    base = {"URL": url, "Bot": bot_label, "Encoding": "", "Status": None, "Size": None, "Capped": False}

    try:
        resp = requests.get(
            url, headers=headers, stream=True,
            timeout=REQUEST_TIMEOUT, allow_redirects=True,
        )
    except requests.exceptions.SSLError:
        return {**base, "Result": "error", "Detail": "SSL error"}
    except requests.exceptions.Timeout:
        return {**base, "Result": "error", "Detail": "Timed out"}
    except requests.exceptions.ConnectionError:
        return {**base, "Result": "error", "Detail": "Connection failed"}
    except requests.exceptions.RequestException:
        return {**base, "Result": "error", "Detail": "Request failed"}

    try:
        status = resp.status_code
        enc = (resp.headers.get("Content-Encoding") or "").strip().lower()
        base["Status"] = status
        base["Encoding"] = enc

        if status != 200:
            return {**base, "Result": "error", "Detail": f"HTTP {status}"}

        is_compressed = any(e.strip() in _COMPRESSED_ENCODINGS for e in enc.split(","))
        if is_compressed:
            return {**base, "Result": "compressed", "Detail": enc}

        # Not compressed: read the raw body (without decoding) to measure the
        # true size the crawler is forced to download.
        raw = resp.raw.read(MAX_READ + 1, decode_content=False)
        size = len(raw)
        capped = size > MAX_READ
        return {
            **base, "Result": "not_compressed",
            "Encoding": enc or "none", "Size": min(size, MAX_READ),
            "Capped": capped, "Detail": "no compression",
        }
    except Exception:
        return {**base, "Result": "error", "Detail": "Read error"}
    finally:
        resp.close()


def _run_check(urls: list[str], bots: list[str]):
    tasks = [(u, b) for u in urls for b in bots]
    total = len(tasks)
    progress = st.progress(0, text=f"Checking {total} request(s)…")
    results: list[dict] = []

    with ThreadPoolExecutor(max_workers=MAX_WORKERS) as executor:
        futures = {executor.submit(_check_one, u, b): (u, b) for u, b in tasks}
        for done, future in enumerate(as_completed(futures), start=1):
            results.append(future.result())
            progress.progress(done / total, text=f"({done}/{total}) checked")
    progress.empty()

    # Restore the original (URL, bot) order.
    order = {(u, b): i for i, (u, b) in enumerate(tasks)}
    results.sort(key=lambda r: order.get((r["URL"], r["Bot"]), 0))

    n_ok = sum(1 for r in results if r["Result"] == "compressed")
    n_bad = sum(1 for r in results if r["Result"] == "not_compressed")
    n_err = sum(1 for r in results if r["Result"] == "error")

    if n_bad == 0 and n_err == 0:
        st.success(f"✅ All {n_ok} request(s) came back compressed. Nothing to fix.")
    elif n_bad:
        st.warning(f"🚩 {n_bad} request(s) came back **uncompressed** — see the detail table below.")
    else:
        st.info(f"✅ {n_ok} compressed · ⚠️ {n_err} could not be checked.")

    # ── 1) Summary by crawler ────────────────────────────────
    st.subheader("Summary")
    summary_rows = []
    for bot in bots:
        rs = [r for r in results if r["Bot"] == bot]
        summary_rows.append({
            "Bot": bot,
            "Checked": len(rs),
            "Compressed": sum(1 for r in rs if r["Result"] == "compressed"),
            "Not compressed": sum(1 for r in rs if r["Result"] == "not_compressed"),
            "Errors": sum(1 for r in rs if r["Result"] == "error"),
        })
    summary_df = pd.DataFrame(summary_rows)
    _render_summary_table(summary_df)

    # ── 2) Detail: pages that are NOT compressed ─────────────
    bad = [r for r in results if r["Result"] == "not_compressed"]
    if bad:
        st.subheader("Pages not compressed")
        detail_df = pd.DataFrame([{
            "URL": r["URL"],
            "Bot": r["Bot"],
            "HTTP": r["Status"],
            "Content-Encoding": r["Encoding"],
            "Uncompressed size": _fmt_size(r["Size"]) + (" +" if r["Capped"] else ""),
        } for r in bad])
        _render_detail_table(detail_df)
        st.download_button(
            "⬇️ Download not-compressed list (CSV)",
            data=detail_df.to_csv(index=False).encode("utf-8"),
            file_name="not_compressed_pages.csv",
            mime="text/csv",
        )
    else:
        st.success("🎉 No uncompressed pages found.")

    # ── Errors (couldn't be checked) ─────────────────────────
    errors = [r for r in results if r["Result"] == "error"]
    if errors:
        with st.expander(f"⚠️ {len(errors)} request(s) could not be checked"):
            err_df = pd.DataFrame([{
                "URL": r["URL"], "Bot": r["Bot"], "Reason": r["Detail"],
            } for r in errors])
            st.dataframe(err_df, use_container_width=True, hide_index=True)


# ── formatting helpers ───────────────────────────────────────

def _fmt_size(n) -> str:
    if n is None:
        return "—"
    if n >= 1024 * 1024:
        return f"{n / 1024 / 1024:.2f} MB"
    if n >= 1024:
        return f"{n / 1024:.0f} KB"
    return f"{n} B"


# ── styled tables (custom HTML — st.dataframe can't center/brand) ──

def _num_cell(value: int, kind: str) -> str:
    """Colored number cell; greys out zeros so the eye lands on real counts."""
    if value == 0:
        return '<td class="cc-center"><span class="cc-zero">0</span></td>'
    return f'<td class="cc-center"><span class="cc-num cc-{kind}">{value}</span></td>'


def _render_summary_table(df: pd.DataFrame):
    rows = []
    for _, r in df.iterrows():
        rows.append(
            "<tr>"
            f'<td class="cc-bot">{_html.escape(str(r["Bot"]))}</td>'
            f'<td class="cc-center">{int(r["Checked"])}</td>'
            f'{_num_cell(int(r["Compressed"]), "ok")}'
            f'{_num_cell(int(r["Not compressed"]), "bad")}'
            f'{_num_cell(int(r["Errors"]), "warn")}'
            "</tr>"
        )
    _render_html_table(
        headers=["Crawler", "Checked", "Compressed", "Not compressed", "Errors"],
        rows_html="".join(rows),
        first_left=True,
    )


def _render_detail_table(df: pd.DataFrame):
    _render_copy_button(df, "compression_detail")
    rows = []
    for _, r in df.iterrows():
        rows.append(
            "<tr>"
            f'<td class="cc-url">{_html.escape(str(r["URL"]))}</td>'
            f'<td class="cc-muted">{_html.escape(str(r["Bot"]))}</td>'
            f'<td class="cc-center">{_html.escape(str(r["HTTP"]))}</td>'
            f'<td class="cc-center"><span class="cc-badge cc-b-bad">{_html.escape(str(r["Content-Encoding"]))}</span></td>'
            f'<td class="cc-center cc-size">{_html.escape(str(r["Uncompressed size"]))}</td>'
            "</tr>"
        )
    _render_html_table(
        headers=["URL", "Crawler", "HTTP", "Content-Encoding", "Uncompressed size"],
        rows_html="".join(rows),
        first_left=True,
    )


def _render_html_table(headers: list[str], rows_html: str, first_left: bool):
    head_cells = "".join(
        f'<th class="{"left" if (first_left and i == 0) else ""}">{_html.escape(h)}</th>'
        for i, h in enumerate(headers)
    )
    st.markdown(
        f"""
        <style>
        .cc-wrap {{
            border: 1px solid #e7e9ee; border-radius: 12px; overflow: hidden;
            box-shadow: 0 1px 3px rgba(16,24,40,.07), 0 1px 2px rgba(16,24,40,.04);
            background: #fff; margin: 6px 0 14px 0;
        }}
        .cc-scroll {{ max-height: 620px; overflow: auto; }}
        table.cc {{
            width: 100%; border-collapse: separate; border-spacing: 0;
            font-family: 'Roboto', sans-serif; font-size: 14px;
        }}
        table.cc thead th {{
            position: sticky; top: 0; z-index: 1; background: #f7f8fa;
            color: #667085; font-family: 'Google Sans','Roboto',sans-serif;
            font-weight: 600; font-size: 11px; letter-spacing: .6px;
            text-transform: uppercase; padding: 13px 18px; text-align: center;
            border-bottom: 1px solid #e7e9ee; white-space: nowrap;
        }}
        table.cc thead th.left {{ text-align: left; }}
        table.cc tbody td {{
            padding: 12px 18px; border-bottom: 1px solid #f0f1f4;
            color: #1d2939; vertical-align: middle;
        }}
        table.cc tbody tr:last-child td {{ border-bottom: none; }}
        table.cc tbody tr:hover {{ background: #f9fafb; }}
        .cc .cc-center {{ text-align: center; }}
        .cc .cc-bot {{ font-weight: 500; color: #101828; }}
        .cc .cc-muted {{ color: #667085; }}
        .cc .cc-url {{ font-weight: 500; color: #101828; word-break: break-all; }}
        .cc .cc-size {{ font-weight: 700; color: #d92d20; white-space: nowrap; }}
        .cc-num {{
            display: inline-flex; align-items: center; justify-content: center;
            min-width: 30px; height: 26px; padding: 0 8px; border-radius: 8px;
            font-family: 'Google Sans','Roboto',sans-serif; font-weight: 700; font-size: 13px;
        }}
        .cc-ok  {{ background: #e7f7ef; color: #0c9d61; }}
        .cc-bad {{ background: #fde8e8; color: #d92d20; }}
        .cc-warn {{ background: #fef3e2; color: #c4720e; }}
        .cc-zero {{ color: #cbd2dc; font-weight: 600; }}
        .cc-badge {{
            display: inline-flex; align-items: center; padding: 4px 11px;
            border-radius: 999px; font-size: 12px; font-weight: 600; letter-spacing: .2px;
        }}
        .cc-b-bad {{ background: #fde8e8; color: #d92d20; }}
        </style>
        <div class="cc-wrap">
          <div class="cc-scroll">
            <table class="cc">
              <thead><tr>{head_cells}</tr></thead>
              <tbody>{rows_html}</tbody>
            </table>
          </div>
        </div>
        """,
        unsafe_allow_html=True,
    )


def _render_copy_button(df: pd.DataFrame, key: str):
    """Copy the table as TSV — paste-ready into Google Sheets / Excel.
    Mirrors the copy button used by the other features."""
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
