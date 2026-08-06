import os
import json
import time
from urllib.parse import urlsplit

import requests
import pandas as pd
import streamlit as st
from dotenv import load_dotenv

load_dotenv()

SERPER_URL = "https://google.serper.dev/search"
SEARCHAPI_URL = "https://www.searchapi.io/api/v1/search"
TOP_N = 5
REQUEST_TIMEOUT = 30


def render():
    st.header("Keyword Ranking Tracker")
    st.caption(
        "Track your Google ranking for a list of keywords, and whether Reddit "
        "and Quora appear in the Top 5 — in both organic results and the Forums tab."
    )
    _render_quick_guide()

    serper_key = _get_secret("SERP_API_KEY")
    searchapi_key = _get_secret("SEARCHAPI_API_KEY")

    with st.form("ranking_tracker_form"):
        website = st.text_input(
            "Your website (domain)",
            value="trueprofit.io",
            help="Only the domain is used, e.g. trueprofit.io",
        )
        keywords_raw = st.text_area(
            "Keywords — one per line",
            placeholder="shopify profit calculator\ndropshipping profit margin\nhow much does dropshipping make",
            height=220,
        )
        uploaded = st.file_uploader(
            "…or upload a .txt file (one keyword per line)", type=["txt"]
        )

        col1, col2, col3, col4 = st.columns(4)
        with col1:
            location = st.text_input("Location", value="United States")
        with col2:
            gl = st.text_input("Country (gl)", value="us")
        with col3:
            hl = st.text_input("Language (hl)", value="en")
        with col4:
            device = st.selectbox("Device", ["desktop", "mobile"], index=0)

        check_forums = st.checkbox(
            "Also check the Google Forums tab (needs SearchApi key)",
            value=bool(searchapi_key),
        )

        submitted = st.form_submit_button("🎯 Track Rankings", type="primary")

    if not submitted:
        return

    if not serper_key:
        st.error(
            "No Serper API key found. Add **SERP_API_KEY** in ⚙️ Settings → SERP API "
            "(the same key used by Keyword Grouping)."
        )
        return

    my_domain = _domain_only(website)
    if not my_domain:
        st.error("Please enter a valid website domain.")
        return

    keywords = _collect_keywords(keywords_raw, uploaded)
    if not keywords:
        st.error("Please paste at least one keyword or upload a .txt file.")
        return

    if check_forums and not searchapi_key:
        st.warning(
            "Forums-tab check is on but no **SEARCHAPI_API_KEY** was found — "
            "skipping the Forums columns. Add the key in Streamlit Secrets to enable it."
        )
        check_forums = False

    _run_tracking(
        keywords, my_domain, serper_key, searchapi_key if check_forums else "",
        location.strip(), gl.strip(), hl.strip(), device,
    )


def _render_quick_guide():
    with st.expander("How this works"):
        st.markdown(
            """
1. Add your **Serper** key (`SERP_API_KEY`) in ⚙️ Settings — this powers the organic search.
2. (Optional) Add a **SearchApi** key (`SEARCHAPI_API_KEY`) in Streamlit Secrets to also check Google's **Forums** tab.
3. Enter your website domain and paste keywords (one per line), or upload a `.txt` file.
4. Click **Track Rankings**. For each keyword the app records:
   - **Your ranking** — your site's organic position (or *Featured Snippet*, or *101+* if not in the top 100).
   - **Reddit / Quora in Organic Top 5?** — Yes/No + the exact URLs.
   - **Reddit / Quora in Forums Top 5?** — same check on Google's Forums tab (if enabled).
5. Review the table and download it as CSV.

**Location** is fixed per run (default: United States, desktop). If a key runs out of
credits mid-run, the app stops the organic search, keeps every keyword already
checked, and lists the ones it did not reach so you can re-run just those.
            """.strip()
        )


# ── secrets & input parsing ──────────────────────────────────

def _get_secret(key: str) -> str:
    value = os.getenv(key, "").strip()
    if value:
        return value
    try:
        return str(st.secrets.get(key, "")).strip()
    except Exception:
        return ""


def _domain_only(url: str) -> str:
    """Bare, lowercased domain of a URL/host, without scheme, www or path."""
    cleaned = (url or "").strip()
    if not cleaned:
        return ""
    candidate = cleaned if "://" in cleaned else "//" + cleaned
    try:
        netloc = urlsplit(candidate).netloc.lower()
    except Exception:
        netloc = ""
    if not netloc:
        netloc = cleaned.lower()
    netloc = netloc.split("@")[-1].split(":")[0]
    return netloc[4:] if netloc.startswith("www.") else netloc


def _collect_keywords(text: str, uploaded) -> list[str]:
    raw: list[str] = []
    if text:
        raw.extend(text.splitlines())
    if uploaded is not None:
        try:
            raw.extend(uploaded.getvalue().decode("utf-8", errors="ignore").splitlines())
        except Exception as exc:
            st.warning(f"Could not read uploaded file: {exc}")

    seen: set[str] = set()
    keywords: list[str] = []
    for item in raw:
        kw = item.strip()
        if kw and kw.lower() not in seen:
            seen.add(kw.lower())
            keywords.append(kw)
    return keywords


# ── ranking helpers ──────────────────────────────────────────

def _domain_in_top_n(results: list[dict], domain: str, top_n: int = TOP_N):
    """Return ("Yes"/"No", url_lines) for a domain within the top N results."""
    found = []
    for r in results:
        position = r.get("position", 101)
        link = r.get("link", "")
        if domain in link and position <= top_n:
            found.append(f"#{position}: {link}")
    return ("Yes" if found else "No"), ("\n".join(found) if found else "N/A")


def _serper_organic(keyword, api_key, location, gl, hl, device):
    """Returns (data, None) or (None, ("stop"|"skip", message)). 'stop' means
    every later call fails too (bad key / no credits) — abort and salvage."""

    def _post():
        return requests.post(
            SERPER_URL,
            headers={"X-API-KEY": api_key, "Content-Type": "application/json"},
            data=json.dumps({
                "q": keyword, "location": location, "gl": gl.lower(),
                "hl": hl.lower(), "device": device, "num": 100,
            }),
            timeout=REQUEST_TIMEOUT,
        )

    try:
        r = _post()
        if r.status_code == 429:
            time.sleep(2)
            r = _post()
            if r.status_code == 429:
                return None, ("stop", "Serper rate/credit limit hit (HTTP 429 twice)")
        if r.status_code in (401, 402, 403):
            return None, ("stop", f"Serper key rejected or out of credits (HTTP {r.status_code})")
        r.raise_for_status()
        return r.json(), None
    except Exception as exc:
        return None, ("skip", str(exc))


def _searchapi_forums(keyword, api_key, location, gl, hl, device):
    """Returns (organic_results, None) or (None, ("stop"|"skip", message))."""

    def _get():
        return requests.get(
            SEARCHAPI_URL,
            headers={"Authorization": f"Bearer {api_key}"},
            params={
                "engine": "google_forums", "q": keyword, "location": location,
                "gl": gl.lower(), "hl": hl.lower(), "device": device,
            },
            timeout=REQUEST_TIMEOUT,
        )

    try:
        r = _get()
        if r.status_code == 429:
            time.sleep(2)
            r = _get()
            if r.status_code == 429:
                return None, ("stop", "SearchApi rate/credit limit hit (HTTP 429 twice)")
        if r.status_code in (401, 402, 403):
            return None, ("stop", f"SearchApi key rejected or out of credits (HTTP {r.status_code})")
        r.raise_for_status()
        return r.json().get("organic_results", []), None
    except Exception as exc:
        return None, ("skip", str(exc))


def _own_ranking(data: dict, my_domain: str):
    """Extract the site's own ranking + URL from a Serper response."""
    rank, url = "101+", "N/A"
    answer_box = data.get("answerBox") or {}
    ab_link = answer_box.get("link", "")
    if ab_link and my_domain in ab_link:
        return "1 (Featured Snippet)", ab_link

    for r in data.get("organic", []):
        link = r.get("link", "")
        if my_domain in link:
            return r.get("position", 101), link
    return rank, url


# ── main run ─────────────────────────────────────────────────

COLUMNS = [
    "Keyword", "Your Ranking", "Your URL",
    "Reddit — Organic Top 5?", "Reddit — Organic URLs",
    "Quora — Organic Top 5?", "Quora — Organic URLs",
    "Reddit — Forums Top 5?", "Reddit — Forums URLs",
    "Quora — Forums Top 5?", "Quora — Forums URLs",
]


def _run_tracking(keywords, my_domain, serper_key, searchapi_key,
                  location, gl, hl, device):
    total = len(keywords)
    progress = st.progress(0, text="Tracking rankings…")
    log_area = st.empty()
    logs: list[str] = []

    rows: list[list] = []
    processed: set[str] = set()
    stop_reason = None
    forums_enabled = bool(searchapi_key)
    forums_stop_reason = None

    for i, kw in enumerate(keywords, 1):
        progress.progress(i / total, text=f"({i}/{total}) {kw}")

        data, error = _serper_organic(kw, serper_key, location, gl, hl, device)
        if error and error[0] == "stop":
            stop_reason = error[1]
            logs.append(f"🛑 ({i}/{total}) {kw} — {stop_reason}")
            log_area.code("\n".join(logs[-15:]))
            break
        if error:
            logs.append(f"⚠️ ({i}/{total}) skipped ({error[1][:60]}): {kw}")
            log_area.code("\n".join(logs[-15:]))
            continue

        organic = data.get("organic", [])
        rank, url = _own_ranking(data, my_domain)
        reddit_o, reddit_o_urls = _domain_in_top_n(organic, "reddit.com")
        quora_o, quora_o_urls = _domain_in_top_n(organic, "quora.com")

        # Forums tab (independent API — its failure never discards organic data)
        if forums_enabled and not forums_stop_reason:
            fres, ferr = _searchapi_forums(kw, searchapi_key, location, gl, hl, device)
            if ferr and ferr[0] == "stop":
                forums_stop_reason = ferr[1]
                reddit_f = quora_f = "n/a"
                reddit_f_urls = quora_f_urls = forums_stop_reason
            elif ferr:
                reddit_f = quora_f = "n/a"
                reddit_f_urls = quora_f_urls = f"Error: {ferr[1][:60]}"
            else:
                reddit_f, reddit_f_urls = _domain_in_top_n(fres, "reddit.com")
                quora_f, quora_f_urls = _domain_in_top_n(fres, "quora.com")
        elif forums_stop_reason:
            reddit_f = quora_f = "n/a"
            reddit_f_urls = quora_f_urls = forums_stop_reason
        else:
            reddit_f = quora_f = "—"
            reddit_f_urls = quora_f_urls = "—"

        rows.append([
            kw, rank, url,
            reddit_o, reddit_o_urls, quora_o, quora_o_urls,
            reddit_f, reddit_f_urls, quora_f, quora_f_urls,
        ])
        processed.add(kw)
        logs.append(f"✅ ({i}/{total}) {kw} — rank {rank}")
        log_area.code("\n".join(logs[-15:]))

        if i < total:
            time.sleep(1.2)

    progress.empty()
    log_area.empty()

    unprocessed = [kw for kw in keywords if kw not in processed]

    if stop_reason:
        st.error(
            f"Stopped early: {stop_reason}. Showing the **{len(rows)}** keyword(s) "
            "already tracked so nothing is wasted."
        )
    if forums_stop_reason:
        st.warning(
            f"Forums-tab check stopped: {forums_stop_reason}. Organic columns are "
            "still complete; affected Forums cells show the reason."
        )
    if unprocessed:
        with st.expander(
            f"⚠️ {len(unprocessed)} keyword(s) not processed — copy to re-run later"
        ):
            st.code("\n".join(unprocessed))

    if not rows:
        st.warning("No rankings could be fetched.")
        return

    df = pd.DataFrame(rows, columns=COLUMNS)
    st.success(f"✅ Tracked {len(rows)} of {total} keyword(s)")
    _render_copy_button(df, "ranking_tracker")
    st.dataframe(df, use_container_width=True, hide_index=True)

    st.download_button(
        label="⬇️ Download as CSV",
        data=df.to_csv(index=False).encode("utf-8"),
        file_name="ranking_tracking_results.csv",
        mime="text/csv",
    )


def _render_copy_button(df: pd.DataFrame, key: str):
    import streamlit.components.v1 as components

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
