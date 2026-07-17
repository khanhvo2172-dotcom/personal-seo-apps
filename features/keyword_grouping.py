import os
import json
import time
import requests
import pandas as pd
import streamlit as st
from dotenv import load_dotenv

load_dotenv()


def render():
    st.header("Keyword Grouping")
    st.caption("Groups keywords based on shared Top 5 SERP results via serper.dev")
    _render_quick_guide()

    saved_key = os.getenv("SERP_API_KEY", "")

    with st.form("keyword_grouping_form"):
        api_key = st.text_input(
            "SERP API Key",
            value=saved_key,
            type="password",
            help="Set permanently in ⚙️ Settings → SERP API.",
        )
        keywords_raw = st.text_area(
            "Keywords — one per line, sorted by search volume descending",
            placeholder="cách làm bánh flan\nhướng dẫn làm caramel\nbánh flan ngon",
            height=200,
        )

        col1, col2, col3, col4 = st.columns(4)
        with col1:
            location = st.text_input("Location (gl)", value="US")
        with col2:
            language = st.text_input("Language (hl)", value="en")
        with col3:
            device = st.selectbox("Device", ["desktop", "mobile"], index=1)
        with col4:
            threshold = st.slider("Common URLs threshold", min_value=2, max_value=5, value=2)

        submitted = st.form_submit_button("🚀 Start Grouping", type="primary")

    if not submitted:
        return

    if not api_key:
        st.error("Please enter your SERP API Key.")
        return

    keywords = [kw.strip() for kw in keywords_raw.splitlines() if kw.strip()]
    if not keywords:
        st.error("Please enter at least one keyword.")
        return

    _run_grouping(api_key, keywords, location.strip(), language.strip(), device, threshold)


def _render_quick_guide():
    with st.expander("How this works"):
        st.markdown(
            """
1. Add your Serper API key in **Settings** or paste it here.
2. Paste keywords, one keyword per line. Put higher-volume or more important keywords first.
3. Choose Google location, language, device, and the common URL threshold.
4. Click **Start Grouping**.
5. The app gets the top 5 Google results for each keyword.
6. Keywords are grouped together when they share enough top-ranking URLs.
7. Download the final keyword-to-topic mapping as a tab-separated text file.

Use a lower threshold for broader groups and a higher threshold for tighter SERP overlap.

If your Serper credits run out mid-run, the app stops immediately, groups the
keywords already fetched, and lists the unprocessed keywords so you can copy
them and re-run just those later — no data is wasted.
            """.strip()
        )


# ── helpers ──────────────────────────────────────────────────

def _get_serp_urls(keyword: str, api_key: str, gl: str, hl: str, device: str):
    """Returns (urls, None) on success.
    On failure returns (None, ("stop"|"skip", message)): "stop" means every
    later call will fail too (bad key / out of credits) — abort the loop and
    salvage what's collected; "skip" means only this keyword failed."""

    def _post():
        return requests.post(
            "https://google.serper.dev/search",
            headers={"X-API-KEY": api_key, "Content-Type": "application/json"},
            data=json.dumps({"q": keyword, "gl": gl.lower(), "hl": hl.lower(), "device": device, "num": 5}),
            timeout=15,
        )

    try:
        r = _post()
        if r.status_code == 429:  # rate limit — back off once and retry
            time.sleep(2)
            r = _post()
            if r.status_code == 429:
                return None, ("stop", "Serper rate/credit limit hit (HTTP 429 twice)")
        if r.status_code in (401, 403, 402):
            return None, ("stop", f"Serper API key rejected or out of credits (HTTP {r.status_code})")
        r.raise_for_status()
        data = r.json()
        return {item["link"] for item in data.get("organic", [])}, None
    except Exception as exc:
        return None, ("skip", str(exc))


def _run_grouping(api_key, keywords, gl, hl, device, threshold):
    progress = st.progress(0, text="Fetching SERP data…")
    log_area = st.empty()
    logs: list[str] = []

    keyword_serps: dict[str, set] = {}
    total = len(keywords)

    stop_reason = None
    for i, kw in enumerate(keywords):
        progress.progress((i + 1) / total, text=f"({i+1}/{total}) Fetching: {kw}")
        urls, error = _get_serp_urls(kw, api_key, gl, hl, device)
        if urls is not None:
            keyword_serps[kw] = urls
            logs.append(f"✅ ({i+1}/{total}) {kw}")
        elif error[0] == "stop":
            # Bad key / no credits: every later call fails too. Stop hammering
            # the API and salvage everything collected so far.
            stop_reason = error[1]
            logs.append(f"🛑 ({i+1}/{total}) {kw} — {stop_reason}")
            log_area.code("\n".join(logs[-15:]))
            break
        else:
            logs.append(f"⚠️ ({i+1}/{total}) Failed — skipping: {kw}")
        log_area.code("\n".join(logs[-15:]))
        if i < total - 1:
            time.sleep(1)

    unprocessed = [kw for kw in keywords if kw not in keyword_serps]

    if stop_reason:
        st.error(
            f"Stopped early: {stop_reason}. Grouping the "
            f"**{len(keyword_serps)}** keyword(s) already fetched so nothing "
            "goes to waste."
        )
    if unprocessed:
        with st.expander(
            f"⚠️ {len(unprocessed)} keyword(s) not processed — copy this list to re-run them later"
        ):
            st.code("\n".join(unprocessed))

    if not keyword_serps:
        progress.empty()
        log_area.empty()
        st.warning("No SERP data could be fetched — nothing to group.")
        return

    progress.progress(1.0, text="Grouping keywords…")

    groups: list[dict] = []
    for kw in keywords:
        if kw not in keyword_serps:
            continue
        kw_urls = keyword_serps[kw]
        best_idx, max_common = -1, -1

        for i, group in enumerate(groups):
            common = len(kw_urls & keyword_serps[group["topic"]])
            if common > max_common:
                max_common, best_idx = common, i

        if max_common >= threshold:
            groups[best_idx]["keywords"].append(kw)
        else:
            groups.append({"topic": kw, "keywords": [kw]})

    progress.empty()
    log_area.empty()

    rows = [{"Keyword": kw, "Topic": g["topic"]} for g in groups for kw in g["keywords"]]
    if not rows:
        st.warning("No results to display.")
        return

    df = pd.DataFrame(rows)
    st.success(f"✅ {len(keyword_serps)} of {total} keywords → {len(groups)} groups")
    st.dataframe(df, use_container_width=True)

    st.download_button(
        label="⬇️ Download as .txt (tab-separated)",
        data=df.to_csv(sep="\t", index=False).encode("utf-8"),
        file_name="keyword_groups.txt",
        mime="text/plain",
    )
