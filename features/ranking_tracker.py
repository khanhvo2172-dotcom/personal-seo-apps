import os
import json
import time
from urllib.parse import urlsplit

import requests
import pandas as pd
import streamlit as st
import streamlit.components.v1 as components
from dotenv import load_dotenv

load_dotenv()

SERPER_SEARCH_URL = "https://google.serper.dev/search"
SERPER_VIDEOS_URL = "https://google.serper.dev/videos"
SEARCHAPI_URL = "https://www.searchapi.io/api/v1/search"
TOP_N = 5
REQUEST_TIMEOUT = 30

# Platform -> the host(s) that count as that platform.
PLATFORMS = {
    "Reddit": ["reddit.com"],
    "Quora": ["quora.com"],
    "YouTube": ["youtube.com", "youtu.be"],
    "TikTok": ["tiktok.com"],
    "X": ["x.com", "twitter.com"],
    "LinkedIn": ["linkedin.com"],
    "Facebook": ["facebook.com", "fb.com", "fb.watch"],
}

# Which platforms are checked in which surface.
ORGANIC_PLATFORMS = ["Reddit", "Quora", "YouTube", "TikTok", "X", "LinkedIn", "Facebook"]
VIDEO_PLATFORMS = ["YouTube", "TikTok"]
FORUM_PLATFORMS = ["Reddit", "Quora", "X", "LinkedIn", "Facebook"]


def render():
    st.header("Keyword Ranking Tracker")
    st.caption(
        "Track your Google ranking for a list of keywords, and whether social/video "
        "platforms appear in the Top 5 — across organic, Videos, Short Videos and Forums."
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

        st.markdown("**Extra tabs to check** (organic Top 5 is always checked)")
        check_videos = st.checkbox(
            "Videos tab (Serper) — YouTube / TikTok", value=True
        )
        check_shorts = st.checkbox(
            "Short Videos tab (SearchApi) — YouTube / TikTok", value=bool(searchapi_key)
        )
        check_forums = st.checkbox(
            "Forums tab (SearchApi) — Reddit / Quora / X / LinkedIn / Facebook",
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

    # SearchApi-backed tabs need the SearchApi key; disable with a notice if missing.
    if (check_shorts or check_forums) and not searchapi_key:
        st.warning(
            "Short Videos / Forums need a **SEARCHAPI_API_KEY** (not found) — those "
            "tabs are skipped. Add the key in Streamlit Secrets to enable them."
        )
        check_shorts = False
        check_forums = False

    _run_tracking(
        keywords, my_domain, serper_key, searchapi_key,
        location.strip(), gl.strip(), hl.strip(), device,
        check_videos, check_shorts, check_forums,
    )


def _render_quick_guide():
    with st.expander("How this works"):
        st.markdown(
            """
1. Add your **Serper** key (`SERP_API_KEY`) in ⚙️ Settings — powers organic + Videos.
2. (Optional) Add a **SearchApi** key (`SEARCHAPI_API_KEY`) in Streamlit Secrets for the **Short Videos** and **Forums** tabs.
3. Enter your website domain and paste keywords (one per line), or upload a `.txt` file.
4. Tick which extra tabs to check, then click **Track Rankings**. For each keyword:
   - **Your ranking** — your site's organic position (or *Featured Snippet*, or *101+*).
   - **Organic Top 5** — Reddit, Quora, YouTube, TikTok, X, LinkedIn, Facebook (Yes/No + URLs).
   - **Videos tab** — YouTube, TikTok (Serper).
   - **Short Videos tab** — YouTube, TikTok (SearchApi `google_shorts`).
   - **Forums tab** — Reddit, Quora, X, LinkedIn, Facebook (SearchApi `google_forums`).
5. Review the table and download it as CSV.

If a key runs out of credits mid-run, the organic search stops and keeps everything
already fetched; each extra tab fails independently (its cells show the reason) so one
outage never discards the rest.
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
    """Bare, lowercased host of a URL, without scheme, www, userinfo or port."""
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


# ── matching helpers ─────────────────────────────────────────

def _link_matches(link: str, domains: list[str]) -> bool:
    """True if the link's host is (a subdomain of) one of `domains`. Matching on
    host boundaries avoids false positives like 'x.com' inside 'netflix.com'."""
    host = _domain_only(link)
    if not host:
        return False
    return any(host == d or host.endswith("." + d) for d in domains)


def _platform_in_top_n(results: list[dict], domains: list[str], top_n: int = TOP_N):
    """Return ("Yes"/"No", url_lines) for a platform within the top N results."""
    found = []
    for r in results:
        position = r.get("position", 101)
        link = r.get("link", "")
        if position <= top_n and _link_matches(link, domains):
            found.append(f"#{position}: {link}")
    return ("Yes" if found else "No"), ("\n".join(found) if found else "N/A")


# ── API calls ────────────────────────────────────────────────

def _serper_post(url, keyword, api_key, location, gl, hl, device, num):
    """Returns (json, None) or (None, ("stop"|"skip", message))."""

    def _post():
        return requests.post(
            url,
            headers={"X-API-KEY": api_key, "Content-Type": "application/json"},
            data=json.dumps({
                "q": keyword, "location": location, "gl": gl.lower(),
                "hl": hl.lower(), "device": device, "num": num,
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


def _searchapi_get(engine, result_field, keyword, api_key, location, gl, hl, device):
    """Query a SearchApi engine. Returns (results_list, None) or
    (None, ("stop"|"skip", message))."""

    def _get():
        return requests.get(
            SEARCHAPI_URL,
            headers={"Authorization": f"Bearer {api_key}"},
            params={
                "engine": engine, "q": keyword, "location": location,
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
        return r.json().get(result_field, []), None
    except Exception as exc:
        return None, ("skip", str(exc))


def _own_ranking(data: dict, my_domain: str):
    """Extract the site's own ranking + URL from a Serper organic response."""
    answer_box = data.get("answerBox") or {}
    ab_link = answer_box.get("link", "")
    if ab_link and _link_matches(ab_link, [my_domain]):
        return "1 (Featured Snippet)", ab_link
    for r in data.get("organic", []):
        link = r.get("link", "")
        if _link_matches(link, [my_domain]):
            return r.get("position", 101), link
    return "101+", "N/A"


# ── main run ─────────────────────────────────────────────────

def _build_columns(check_videos, check_shorts, check_forums) -> list[str]:
    cols = ["Keyword", "Your Ranking", "Your URL"]
    for p in ORGANIC_PLATFORMS:
        cols += [f"{p} — Organic Top 5?", f"{p} — Organic URLs"]
    if check_videos:
        for p in VIDEO_PLATFORMS:
            cols += [f"{p} — Videos Top 5?", f"{p} — Videos URLs"]
    if check_shorts:
        for p in VIDEO_PLATFORMS:
            cols += [f"{p} — Short Videos Top 5?", f"{p} — Short Videos URLs"]
    if check_forums:
        for p in FORUM_PLATFORMS:
            cols += [f"{p} — Forums Top 5?", f"{p} — Forums URLs"]
    return cols


def _fill_surface(row, results, platforms, label, note=None):
    """Write Top-5?/URLs cells for a surface. If `note` is set the surface
    failed — write the note into every cell instead of checking results."""
    for p in platforms:
        flag_col, url_col = f"{p} — {label} Top 5?", f"{p} — {label} URLs"
        if note is not None:
            row[flag_col], row[url_col] = "n/a", note
        else:
            row[flag_col], row[url_col] = _platform_in_top_n(results, PLATFORMS[p])


def _run_tracking(keywords, my_domain, serper_key, searchapi_key,
                  location, gl, hl, device,
                  check_videos, check_shorts, check_forums):
    total = len(keywords)
    columns = _build_columns(check_videos, check_shorts, check_forums)
    progress = st.progress(0, text="Tracking rankings…")
    log_area = st.empty()
    logs: list[str] = []

    rows: list[dict] = []
    processed: set[str] = set()
    stop_reason = None
    # Independent per-surface disable flags so one API's outage never kills others.
    videos_stop = shorts_stop = forums_stop = None

    for i, kw in enumerate(keywords, 1):
        progress.progress(i / total, text=f"({i}/{total}) {kw}")

        data, error = _serper_post(
            SERPER_SEARCH_URL, kw, serper_key, location, gl, hl, device, 100
        )
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
        row = {"Keyword": kw, "Your Ranking": rank, "Your URL": url}
        _fill_surface(row, organic, ORGANIC_PLATFORMS, "Organic")

        # Videos tab (Serper)
        if check_videos:
            if videos_stop:
                _fill_surface(row, None, VIDEO_PLATFORMS, "Videos", note=videos_stop)
            else:
                vdata, verr = _serper_post(
                    SERPER_VIDEOS_URL, kw, serper_key, location, gl, hl, device, 10
                )
                if verr and verr[0] == "stop":
                    videos_stop = verr[1]
                    _fill_surface(row, None, VIDEO_PLATFORMS, "Videos", note=videos_stop)
                elif verr:
                    _fill_surface(row, None, VIDEO_PLATFORMS, "Videos", note=f"Error: {verr[1][:50]}")
                else:
                    _fill_surface(row, vdata.get("videos", []), VIDEO_PLATFORMS, "Videos")
                time.sleep(0.5)

        # Short Videos tab (SearchApi google_shorts)
        if check_shorts:
            if shorts_stop:
                _fill_surface(row, None, VIDEO_PLATFORMS, "Short Videos", note=shorts_stop)
            else:
                sres, serr = _searchapi_get(
                    "google_shorts", "shorts", kw, searchapi_key, location, gl, hl, device
                )
                if serr and serr[0] == "stop":
                    shorts_stop = serr[1]
                    _fill_surface(row, None, VIDEO_PLATFORMS, "Short Videos", note=shorts_stop)
                elif serr:
                    _fill_surface(row, None, VIDEO_PLATFORMS, "Short Videos", note=f"Error: {serr[1][:50]}")
                else:
                    _fill_surface(row, sres, VIDEO_PLATFORMS, "Short Videos")
                time.sleep(0.5)

        # Forums tab (SearchApi google_forums)
        if check_forums:
            if forums_stop:
                _fill_surface(row, None, FORUM_PLATFORMS, "Forums", note=forums_stop)
            else:
                fres, ferr = _searchapi_get(
                    "google_forums", "organic_results", kw, searchapi_key, location, gl, hl, device
                )
                if ferr and ferr[0] == "stop":
                    forums_stop = ferr[1]
                    _fill_surface(row, None, FORUM_PLATFORMS, "Forums", note=forums_stop)
                elif ferr:
                    _fill_surface(row, None, FORUM_PLATFORMS, "Forums", note=f"Error: {ferr[1][:50]}")
                else:
                    _fill_surface(row, fres, FORUM_PLATFORMS, "Forums")
                time.sleep(0.5)

        rows.append(row)
        processed.add(kw)
        logs.append(f"✅ ({i}/{total}) {kw} — rank {rank}")
        log_area.code("\n".join(logs[-15:]))

        if i < total:
            time.sleep(1.0)

    progress.empty()
    log_area.empty()

    unprocessed = [kw for kw in keywords if kw not in processed]

    if stop_reason:
        st.error(
            f"Stopped early: {stop_reason}. Showing the **{len(rows)}** keyword(s) "
            "already tracked so nothing is wasted."
        )
    for surface, reason in [("Videos", videos_stop), ("Short Videos", shorts_stop), ("Forums", forums_stop)]:
        if reason:
            st.warning(f"{surface} tab stopped: {reason}. Other columns are unaffected.")
    if unprocessed:
        with st.expander(
            f"⚠️ {len(unprocessed)} keyword(s) not processed — copy to re-run later"
        ):
            st.code("\n".join(unprocessed))

    if not rows:
        st.warning("No rankings could be fetched.")
        return

    df = pd.DataFrame(rows, columns=columns)
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
