import json
import re
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta, timezone

import pandas as pd
import requests
import streamlit as st
import streamlit.components.v1 as components

API_BASE = "https://www.googleapis.com/youtube/v3"
UA_HEADERS = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"}
REQUEST_TIMEOUT = 20
SHORT_MAX_SECONDS = 183      # Shorts can be up to 3 minutes
MAX_COMMENT_PAGES = 5        # ~500 newest top-level threads per video
MAX_FULL_REPLY_THREADS = 20  # per video: threads with >5 replies fetched in full
SHORTS_CHECK_WORKERS = 8

MAX_SEARCH_PAGES = 3         # up to 150 search results per keyword
MODE_KEYWORDS = "🔎 Keywords"
MODE_LINKS = "🔗 Video links"
MODE_CHECK = "🗑️ Check deletions"

DAILY_QUOTA = 10_000
QUOTA_COSTS = {"search": 100, "videos": 1, "commentThreads": 1, "comments": 1}

DEFAULT_CHANNELS = "\n".join([
    "@CalebThompson-w2p",
    "@AveryWalker-z4g",
    "@DaisyNguyen-o4x",
    "@DorianFinch-m2f",
    "@EthanMiller-h3x",
    "@HarperMartinez-r2l",
    "@JacksonRobinson-j4g",
    "@MasonAnderson-f7e",
    "@SophiaGarcia-o7q",
    "@OscarLi-w2c",
])

TYPE_SHORT = "Short"
TYPE_LONG = "Long Video"

RE_BRANDED = re.compile(r"true\s*profit", re.IGNORECASE)
RE_DURATION = re.compile(r"PT(?:(\d+)H)?(?:(\d+)M)?(?:(\d+)S)?")
RE_VIDEO_ID_PATTERNS = [
    re.compile(r"youtube\.com/shorts/([\w-]{11})"),
    re.compile(r"youtube\.com/watch\?[^\s]*v=([\w-]{11})"),
    re.compile(r"youtu\.be/([\w-]{11})"),
    re.compile(r"youtube\.com/embed/([\w-]{11})"),
    re.compile(r"youtube\.com/live/([\w-]{11})"),
]


def render():
    st.header("YouTube Seeding Tracker")
    st.caption(
        "Find the comments your seeding accounts have published — search by "
        "keyword or paste video links, filter by your channel names."
    )
    _render_quick_guide()

    api_key = _get_api_key()
    if not api_key:
        return

    mode = st.radio(
        "Input mode",
        [MODE_KEYWORDS, MODE_LINKS, MODE_CHECK],
        horizontal=True,
        key="yst_mode",
    )

    with st.form("yst_form"):
        keywords_raw = ""
        links_raw = ""
        ids_raw = ""
        video_type = "Both"
        top_n = 5

        if mode == MODE_CHECK:
            ids_raw = st.text_area(
                "Comment IDs — one per line",
                placeholder=(
                    "UgxK2AbCdEfGhIjKlMn4AaABAg\n"
                    "UgzQwErTyUiOpAsDfGh4AaABAg.AbCdEfGhIjK\n"
                    "…paste the 'Comment ID' column from a previous export"
                ),
                height=220,
                help="Copy the Comment ID column from an earlier scan's results "
                "(commas, spaces, or new lines all work as separators). Each ID "
                "is re-checked and marked Live or Deleted / hidden.",
            )
        elif mode == MODE_KEYWORDS:
            keywords_raw = st.text_area(
                "Keywords — one per line",
                placeholder="profit tracking app\nshopify profit calculator",
                height=120,
            )
            c1, c2 = st.columns(2)
            with c1:
                video_type = st.radio(
                    "Video type",
                    [TYPE_SHORT, TYPE_LONG, "Both"],
                    horizontal=True,
                )
            with c2:
                top_n = int(
                    st.number_input(
                        "Top videos per keyword",
                        min_value=1,
                        max_value=100,
                        value=5,
                        step=5,
                    )
                )
        else:
            links_raw = st.text_area(
                "YouTube video links — one per line",
                placeholder=(
                    "https://www.youtube.com/watch?v=XXXXXXXXXXX\n"
                    "https://youtu.be/XXXXXXXXXXX\n"
                    "https://www.youtube.com/shorts/XXXXXXXXXXX"
                ),
                height=140,
            )

        channels_raw = ""
        if mode != MODE_CHECK:
            channels_raw = st.text_area(
                "Seeding channel names — one per line",
                value=DEFAULT_CHANNELS,
                height=240,
                help="Paste the names exactly as they appear on the channel "
                "(with or without the leading @). Matching is case-insensitive.",
            )

        submit_label = (
            "🗑️ Check for deletions" if mode == MODE_CHECK
            else "🌱 Find seeding comments"
        )
        submitted = st.form_submit_button(submit_label, type="primary")

    if not submitted:
        return

    if mode == MODE_CHECK:
        _run_deletion_check(api_key, ids_raw)
        return

    names = _parse_channel_names(channels_raw)
    if not names:
        st.error("Please enter at least one seeding channel name.")
        return

    if mode == MODE_KEYWORDS:
        keywords = [k.strip() for k in keywords_raw.splitlines() if k.strip()]
        if not keywords:
            st.error("Please enter at least one keyword.")
            return
        _run_keyword_mode(api_key, keywords, video_type, top_n, names)
    else:
        video_ids = _extract_video_ids(links_raw)
        if not video_ids:
            st.error("No valid YouTube video links found. Check the URLs and try again.")
            return
        _run_links_mode(api_key, video_ids, names)


def _render_quick_guide():
    with st.expander("How this works"):
        st.markdown(
            """
**Keywords mode** — enter keywords, pick Short / Long / Both and how many top
videos per keyword. The tool searches YouTube (relevance order), classifies each
result as Short or Long, then scans the selected videos' comments.

**Video links mode** — paste specific video URLs; only those videos are scanned.

**Check deletions mode** — upload a CSV exported by this tool; every comment is
re-checked by its Comment ID and marked 🟢 Live or 🔴 Deleted / hidden (YouTube
can't distinguish deleted vs. spam-filtered vs. held-for-review — all appear gone).

In both modes, only comments whose **author name matches your seeding channel
list** are returned (top-level comments **and** replies).

Notes:
- Needs a **YouTube Data API v3 key** in the app secrets (`YOUTUBE_API_KEY`).
- Scans the **~500 newest comment threads** per video — seeding comments are
  usually recent, so this catches them. Threads with more than 5 replies are
  fetched in full (up to 20 such threads per video).
- Shorts are detected via video duration + the `/shorts/` URL check.
- Quota: a keyword search costs 100 units per 50 results (top 100 may use 2–3
  pages), each video ~6 — the free daily quota (10,000) still comfortably
  covers many runs per day.
            """.strip()
        )


# ── api key ──────────────────────────────────────────────────

def _get_api_key() -> str:
    key = ""
    try:
        key = (st.secrets.get("YOUTUBE_API_KEY") or "").strip()
    except Exception:
        key = ""
    if key:
        return key

    st.warning(
        "**YouTube API key not configured.** Add `YOUTUBE_API_KEY = \"...\"` to the "
        "app secrets (Streamlit Cloud → app → **Settings → Secrets**), or paste a "
        "key below to use it for this session only."
    )
    manual = st.text_input(
        "YouTube Data API v3 key (session only)",
        type="password",
        key="yst_manual_key",
    )
    return (manual or "").strip()


# ── input parsing ────────────────────────────────────────────

def _norm_name(name: str) -> str:
    return name.strip().lstrip("@").lower()


def _parse_channel_names(text: str) -> set[str]:
    return {_norm_name(line) for line in (text or "").splitlines() if line.strip()}


def _extract_video_ids(text: str) -> list[str]:
    ids: list[str] = []
    for line in (text or "").splitlines():
        line = line.strip()
        if not line:
            continue
        for pattern in RE_VIDEO_ID_PATTERNS:
            m = pattern.search(line)
            if m:
                if m.group(1) not in ids:
                    ids.append(m.group(1))
                break
    return ids


# ── YouTube API helpers ──────────────────────────────────────

class YTError(Exception):
    def __init__(self, message: str, reason: str = ""):
        super().__init__(message)
        self.reason = reason


# ── quota tracking (estimate) ────────────────────────────────

def _pacific_date() -> str:
    """YouTube quota resets at midnight Pacific time."""
    try:
        from zoneinfo import ZoneInfo
        return datetime.now(ZoneInfo("America/Los_Angeles")).strftime("%Y-%m-%d")
    except Exception:
        return (datetime.now(timezone.utc) - timedelta(hours=7)).strftime("%Y-%m-%d")


@st.cache_resource
def _quota_ledger() -> dict:
    """Best-effort daily counter shared across sessions; resets when the
    app restarts (Streamlit Cloud has no persistent storage)."""
    return {"date": "", "units": 0}


def _add_quota(endpoint: str):
    cost = QUOTA_COSTS.get(endpoint, 1)
    st.session_state["yst_run_units"] = st.session_state.get("yst_run_units", 0) + cost
    ledger = _quota_ledger()
    today = _pacific_date()
    if ledger["date"] != today:
        ledger["date"] = today
        ledger["units"] = 0
    ledger["units"] += cost


def _reset_run_quota():
    st.session_state["yst_run_units"] = 0


def _render_quota_note():
    run = st.session_state.get("yst_run_units", 0)
    ledger = _quota_ledger()
    st.caption(
        f"📊 API quota used: **~{run:,} units** this run · "
        f"~{ledger['units']:,} / {DAILY_QUOTA:,} today (estimate — resets at "
        f"midnight Pacific time and when the app restarts; exact usage: "
        f"[Google Cloud Console](https://console.cloud.google.com/apis/api/youtube.googleapis.com/quotas))"
    )


def _yt_get(endpoint: str, params: dict, api_key: str) -> dict:
    resp = requests.get(
        f"{API_BASE}/{endpoint}",
        params={**params, "key": api_key},
        timeout=REQUEST_TIMEOUT,
    )
    if resp.status_code == 200:
        _add_quota(endpoint)
        return resp.json()

    reason, message = "", f"HTTP {resp.status_code}"
    try:
        err = resp.json().get("error", {})
        message = err.get("message", message)
        errors = err.get("errors", [])
        if errors:
            reason = errors[0].get("reason", "")
    except Exception:
        pass
    raise YTError(message, reason)


def _parse_duration(iso: str) -> int:
    m = RE_DURATION.fullmatch(iso or "")
    if not m:
        return 10**6  # unknown → treat as long
    h, mi, s = (int(g) if g else 0 for g in m.groups())
    return h * 3600 + mi * 60 + s


def _check_shorts_url(video_id: str) -> bool:
    """True if youtube.com/shorts/<id> serves directly (i.e. it IS a Short)."""
    try:
        resp = requests.get(
            f"https://www.youtube.com/shorts/{video_id}",
            headers=UA_HEADERS,
            allow_redirects=False,
            timeout=REQUEST_TIMEOUT,
            stream=True,
        )
        resp.close()
        return resp.status_code == 200
    except requests.RequestException:
        return False


def _classify_videos(video_ids: list[str], api_key: str) -> dict[str, str]:
    """Map video_id -> TYPE_SHORT / TYPE_LONG."""
    durations: dict[str, int] = {}
    for i in range(0, len(video_ids), 50):
        chunk = video_ids[i : i + 50]
        data = _yt_get(
            "videos",
            {"part": "contentDetails", "id": ",".join(chunk), "maxResults": 50},
            api_key,
        )
        for item in data.get("items", []):
            durations[item["id"]] = _parse_duration(
                item.get("contentDetails", {}).get("duration", "")
            )

    types: dict[str, str] = {}
    maybe_short = []
    for vid in video_ids:
        if durations.get(vid, 10**6) > SHORT_MAX_SECONDS:
            types[vid] = TYPE_LONG
        else:
            maybe_short.append(vid)

    if maybe_short:
        with ThreadPoolExecutor(max_workers=SHORTS_CHECK_WORKERS) as pool:
            futures = {pool.submit(_check_shorts_url, v): v for v in maybe_short}
            for future in as_completed(futures):
                vid = futures[future]
                types[vid] = TYPE_SHORT if future.result() else TYPE_LONG
    return types


def _search_videos_page(keyword: str, api_key: str, page_token=None):
    params = {"part": "id", "type": "video", "q": keyword, "maxResults": 50,
              "order": "relevance"}
    if page_token:
        params["pageToken"] = page_token
    data = _yt_get("search", params, api_key)
    ids = [
        item["id"]["videoId"]
        for item in data.get("items", [])
        if item.get("id", {}).get("videoId")
    ]
    return ids, data.get("nextPageToken")


def _comment_date(snippet: dict) -> str:
    """'2026-07-15T08:30:00Z' -> '2026-07-15 08:30' (UTC)."""
    iso = snippet.get("publishedAt", "")
    return iso[:16].replace("T", " ") if iso else ""


def _fetch_all_comments(video_id: str, api_key: str) -> list[tuple[str, str, str, str]]:
    """(author, text, date, comment_id) — newest ~500 top-level threads + replies."""
    comments: list[tuple[str, str, str, str]] = []
    full_reply_budget = MAX_FULL_REPLY_THREADS
    page_token = None

    for _ in range(MAX_COMMENT_PAGES):
        params = {
            "part": "snippet,replies",
            "videoId": video_id,
            "maxResults": 100,
            "order": "time",
            "textFormat": "plainText",
        }
        if page_token:
            params["pageToken"] = page_token
        data = _yt_get("commentThreads", params, api_key)

        for thread in data.get("items", []):
            top_comment = thread["snippet"]["topLevelComment"]
            top = top_comment["snippet"]
            comments.append(
                (top.get("authorDisplayName", ""), top.get("textOriginal", ""),
                 _comment_date(top), top_comment.get("id", ""))
            )
            embedded = thread.get("replies", {}).get("comments", [])
            total_replies = thread["snippet"].get("totalReplyCount", 0)

            if total_replies > len(embedded) and full_reply_budget > 0:
                full_reply_budget -= 1
                comments.extend(_fetch_thread_replies(thread["id"], api_key))
            else:
                for reply in embedded:
                    rs = reply["snippet"]
                    comments.append(
                        (rs.get("authorDisplayName", ""), rs.get("textOriginal", ""),
                         _comment_date(rs), reply.get("id", ""))
                    )

        page_token = data.get("nextPageToken")
        if not page_token:
            break
    return comments


def _fetch_thread_replies(thread_id: str, api_key: str) -> list[tuple[str, str, str, str]]:
    replies: list[tuple[str, str, str, str]] = []
    page_token = None
    for _ in range(2):  # up to 200 replies per thread
        params = {
            "part": "snippet",
            "parentId": thread_id,
            "maxResults": 100,
            "textFormat": "plainText",
        }
        if page_token:
            params["pageToken"] = page_token
        try:
            data = _yt_get("comments", params, api_key)
        except YTError:
            break
        for item in data.get("items", []):
            s = item["snippet"]
            replies.append(
                (s.get("authorDisplayName", ""), s.get("textOriginal", ""),
                 _comment_date(s), item.get("id", ""))
            )
        page_token = data.get("nextPageToken")
        if not page_token:
            break
    return replies


# ── run modes ────────────────────────────────────────────────

def _run_deletion_check(api_key, ids_raw):
    _reset_run_quota()

    ids: list[str] = []
    for token in re.split(r"[\s,]+", ids_raw or ""):
        token = token.strip()
        if token and token not in ids:
            ids.append(token)
    if not ids:
        st.error("Please paste at least one Comment ID.")
        return

    live_ids: set[str] = set()
    with st.spinner(f"Re-checking {len(ids)} comment(s)…"):
        try:
            for i in range(0, len(ids), 50):
                chunk = ids[i : i + 50]
                data = _yt_get(
                    "comments",
                    {"part": "id", "id": ",".join(chunk)},
                    api_key,
                )
                live_ids.update(item["id"] for item in data.get("items", []))
        except YTError as exc:
            _report_api_error(exc, "re-checking comments")
            return

    df = pd.DataFrame(
        {
            "Comment ID": ids,
            "Status": [
                "🟢 Live" if i in live_ids else "🔴 Deleted / hidden" for i in ids
            ],
        }
    )

    deleted = int((df["Status"] == "🔴 Deleted / hidden").sum())
    live = int((df["Status"] == "🟢 Live").sum())
    if deleted:
        st.error(
            f"🔴 **{deleted}** of {len(df)} comment(s) are gone "
            f"(deleted, hidden, or held for review) · 🟢 {live} still live."
        )
    else:
        st.success(f"🟢 All {live} comment(s) are still live.")

    # Deleted rows first so losses are immediately visible.
    df = df.sort_values("Status", key=lambda s: s != "🔴 Deleted / hidden")
    columns = ["Status"] + [c for c in df.columns if c != "Status"]
    df = df[columns]

    st.dataframe(
        df,
        use_container_width=True,
        hide_index=True,
        height=460,
        column_config={"Video link": st.column_config.LinkColumn("Video link")}
        if "Video link" in df.columns else None,
    )

    txt_lines = ["\t".join(columns)]
    for _, r in df.iterrows():
        txt_lines.append(
            "\t".join(str(r[c]).replace("\t", " ").replace("\n", " ") for c in columns)
        )
    txt = "\n".join(txt_lines)

    c1, c2, c3 = st.columns(3)
    with c1:
        _copy_button(txt)
    with c2:
        st.download_button(
            "⬇️ Download as CSV",
            data=df.to_csv(index=False).encode("utf-8-sig"),
            file_name="seeding_comments_deletion_check.csv",
            mime="text/csv",
            use_container_width=True,
        )
    with c3:
        st.download_button(
            "⬇️ Download as TXT",
            data=txt.encode("utf-8"),
            file_name="seeding_comments_deletion_check.txt",
            mime="text/plain",
            use_container_width=True,
        )
    _render_quota_note()


def _run_keyword_mode(api_key, keywords, video_type, top_n, names):
    _reset_run_quota()
    selected: list[tuple[str, str, str]] = []  # (video_id, type, keyword)
    wanted = [TYPE_SHORT, TYPE_LONG] if video_type == "Both" else [video_type]

    with st.spinner("Searching YouTube…"):
        for keyword in keywords:
            # Paginate search + classify until we have top_n of each wanted type
            # (search returns 50 results per page, so top 100 needs 2-3 pages).
            found: dict[str, list[str]] = {TYPE_SHORT: [], TYPE_LONG: []}
            page_token = None
            any_results = False
            try:
                for page in range(MAX_SEARCH_PAGES):
                    ids, page_token = _search_videos_page(keyword, api_key, page_token)
                    if not ids:
                        break
                    any_results = True
                    types = _classify_videos(ids, api_key)
                    for vid in ids:
                        vtype = types.get(vid, TYPE_LONG)
                        if vid not in found[vtype]:
                            found[vtype].append(vid)
                    if all(len(found[t]) >= top_n for t in wanted) or not page_token:
                        break
            except YTError as exc:
                _report_api_error(exc, f'searching "{keyword}"')
                return

            if not any_results:
                st.info(f'No videos found for "{keyword}".')
                continue

            for wtype in wanted:
                picked = found[wtype][:top_n]
                if len(picked) < top_n:
                    st.info(
                        f'"{keyword}": only {len(picked)} {wtype} video(s) found '
                        f"in the top search results (asked for {top_n})."
                    )
                selected.extend((v, wtype, keyword) for v in picked)

    if not selected:
        st.warning("No matching videos to scan.")
        return

    _scan_videos_and_render(api_key, selected, names, include_keyword=True)


def _run_links_mode(api_key, video_ids, names):
    _reset_run_quota()
    with st.spinner("Classifying videos…"):
        try:
            types = _classify_videos(video_ids, api_key)
        except YTError as exc:
            _report_api_error(exc, "classifying videos")
            return
    selected = [(v, types.get(v, TYPE_LONG), "") for v in video_ids]
    _scan_videos_and_render(api_key, selected, names, include_keyword=False)


def _video_url(video_id: str, vtype: str) -> str:
    if vtype == TYPE_SHORT:
        return f"https://www.youtube.com/shorts/{video_id}"
    return f"https://www.youtube.com/watch?v={video_id}"


def _report_api_error(exc: YTError, context: str):
    if exc.reason in ("quotaExceeded", "dailyLimitExceeded"):
        st.error(
            f"YouTube API daily quota exhausted while {context}. "
            "Quota resets at midnight Pacific time."
        )
    elif exc.reason in ("keyInvalid", "badRequest") or "API key" in str(exc):
        st.error(f"YouTube API key problem while {context}: {exc}")
    else:
        st.error(f"YouTube API error while {context}: {exc}")


def _scan_videos_and_render(api_key, selected, names, include_keyword: bool):
    total = len(selected)
    progress = st.progress(0, text=f"Scanning comments on {total} video(s)…")

    rows: list[dict] = []
    scan_notes: list[str] = []
    scanned = 0

    for done, (video_id, vtype, keyword) in enumerate(selected, start=1):
        url = _video_url(video_id, vtype)
        try:
            comments = _fetch_all_comments(video_id, api_key)
            scanned += 1
        except YTError as exc:
            if exc.reason == "commentsDisabled":
                scan_notes.append(f"{url} — comments are disabled")
            elif exc.reason in ("quotaExceeded", "dailyLimitExceeded"):
                progress.empty()
                _report_api_error(exc, "reading comments")
                st.info(f"Stopped after {done - 1} of {total} video(s).")
                break
            else:
                scan_notes.append(f"{url} — {exc}")
            comments = []

        for author, text, date, comment_id in comments:
            if _norm_name(author) in names:
                row = {
                    "Channel name": author,
                    "Comment": text,
                    "Branded comment": "Branded" if RE_BRANDED.search(text) else "Non-Branded",
                    "Comment date": date,
                    "Video link": url,
                    "Video type": vtype,
                    "Comment ID": comment_id,
                }
                if include_keyword:
                    row["Keyword"] = keyword
                rows.append(row)

        progress.progress(done / total, text=f"({done}/{total}) videos scanned")

    progress.empty()
    _render_results(rows, scanned, scan_notes, include_keyword)


# ── results ──────────────────────────────────────────────────

def _render_results(rows, scanned, scan_notes, include_keyword: bool):
    if scan_notes:
        with st.expander(f"⚠️ {len(scan_notes)} video(s) skipped or partial"):
            for note in scan_notes:
                st.markdown(f"- {note}")

    if not rows:
        st.warning(
            f"No seeding comments found on the {scanned} scanned video(s). "
            "Double-check the channel names match exactly how they appear on YouTube."
        )
        _render_quota_note()
        return

    columns = ["Channel name", "Comment", "Branded comment", "Comment date",
               "Video link", "Video type"]
    if include_keyword:
        columns.append("Keyword")
    columns.append("Comment ID")  # keeps exports usable by "Check deletions" mode
    df = pd.DataFrame(rows, columns=columns)

    by_channel = df["Channel name"].value_counts()
    branded = (df["Branded comment"] == "Branded").sum()
    st.success(
        f"✅ Found **{len(df)}** seeding comment(s) from "
        f"**{len(by_channel)}** channel(s) across {scanned} scanned video(s) — "
        f"**{branded}** Branded · **{len(df) - branded}** Non-Branded."
    )
    st.dataframe(
        df,
        use_container_width=True,
        hide_index=True,
        height=460,
        column_config={
            "Video link": st.column_config.LinkColumn("Video link"),
        },
    )

    txt_lines = ["\t".join(columns)]
    for _, r in df.iterrows():
        txt_lines.append(
            "\t".join(str(r[c]).replace("\t", " ").replace("\n", " ") for c in columns)
        )
    txt = "\n".join(txt_lines)

    c1, c2, c3 = st.columns(3)
    with c1:
        _copy_button(txt)
    with c2:
        st.download_button(
            "⬇️ Download as CSV",
            data=df.to_csv(index=False).encode("utf-8-sig"),
            file_name="seeding_comments.csv",
            mime="text/csv",
            use_container_width=True,
        )
    with c3:
        st.download_button(
            "⬇️ Download as TXT",
            data=txt.encode("utf-8"),
            file_name="seeding_comments.txt",
            mime="text/plain",
            use_container_width=True,
        )
    _render_quota_note()


def _copy_button(text: str):
    """Clipboard 'Copy all' button (tab-separated — pastes cleanly into Sheets).
    Runs inside a component iframe, so the app-wide Ctrl+C guard doesn't apply;
    falls back to execCommand when the async clipboard API is blocked."""
    payload = json.dumps(text)
    components.html(
        f"""
        <style>
        .cp-btn {{
            width: 100%;
            height: 38px;
            font-family: 'Google Sans','Roboto',sans-serif;
            font-weight: 500;
            font-size: 14px;
            letter-spacing: .25px;
            color: #1d2939;
            background: #fff;
            border: 1px solid #dadce0;
            border-radius: 4px;
            cursor: pointer;
            transition: background .18s, box-shadow .18s, border-color .18s;
        }}
        .cp-btn:hover {{
            background: #f8f9fa;
            border-color: #4285F4;
            box-shadow: 0 1px 2px rgba(60,64,67,.15);
        }}
        .cp-btn.copied {{ color: #0c9d61; border-color: #0c9d61; }}
        </style>
        <button class="cp-btn" id="cpbtn">📋 Copy all</button>
        <script>
        const data = {payload};
        const btn = document.getElementById('cpbtn');
        btn.addEventListener('click', async () => {{
            let ok = false;
            try {{
                await navigator.clipboard.writeText(data);
                ok = true;
            }} catch (e) {{
                try {{
                    const ta = document.createElement('textarea');
                    ta.value = data;
                    ta.style.position = 'fixed';
                    ta.style.opacity = '0';
                    document.body.appendChild(ta);
                    ta.focus(); ta.select();
                    ok = document.execCommand('copy');
                    ta.remove();
                }} catch (e2) {{ ok = false; }}
            }}
            btn.textContent = ok ? '✅ Copied!' : '⚠️ Press Ctrl+C';
            btn.classList.toggle('copied', ok);
            setTimeout(() => {{
                btn.textContent = '📋 Copy all';
                btn.classList.remove('copied');
            }}, 1600);
        }});
        </script>
        """,
        height=46,
    )
