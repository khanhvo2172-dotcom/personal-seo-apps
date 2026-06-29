import io
import time
import requests
import pandas as pd
import streamlit as st
from concurrent.futures import ThreadPoolExecutor, as_completed

API_URL = "https://api.ahrefs.com/v3/public/domain-rating-free"
MAX_WORKERS = 8
REQUEST_TIMEOUT = 20
ATTRIBUTION = "Domain Rating by Ahrefs"


def render():
    st.header("Bulk Check Ahrefs DR")
    st.caption("Bulk-check Domain Rating for many domains via the free Ahrefs public API — no API key required.")
    _render_quick_guide()

    with st.form("bulk_check_dr_form"):
        targets_raw = st.text_area(
            "Domains or URLs — one per line",
            placeholder="trueprofit.io\nshopify.com\nhttps://ahrefs.com",
            height=220,
        )
        uploaded = st.file_uploader(
            "…or upload a file (one target per row)",
            type=["txt", "csv", "xlsx", "xls"],
            help="First column is used for .csv/.xlsx; every non-empty line for .txt.",
        )
        submitted = st.form_submit_button("📈 Check DR", type="primary")

    if not submitted:
        return

    targets = _collect_targets(targets_raw, uploaded)
    if not targets:
        st.error("Please paste at least one domain/URL or upload a file.")
        return

    _run_bulk_check(targets)


def _render_quick_guide():
    with st.expander("How this works"):
        st.markdown(
            """
1. Paste domains or URLs, **one per line** (or upload a `.txt`, `.csv`, or `.xlsx` file).
2. Click **Check DR**.
3. The app queries the free Ahrefs Domain Rating endpoint for each target in parallel.
4. Review the sortable results table and download it as CSV.

Notes:
- No API key needed — this uses Ahrefs' free public endpoint.
- Domain Rating is on a 100-point logarithmic scale.
- Duplicate targets are removed automatically.
- Data is **Domain Rating by Ahrefs** and subject to the
  [Domain Rating License](http://ahrefs.com/legal/domain-rating-license).
            """.strip()
        )


# ── input parsing ────────────────────────────────────────────

def _collect_targets(text: str, uploaded) -> list[str]:
    raw: list[str] = []

    if text:
        raw.extend(text.splitlines())

    if uploaded is not None:
        name = uploaded.name.lower()
        try:
            if name.endswith((".xlsx", ".xls")):
                df = pd.read_excel(uploaded, header=None, dtype=str)
                raw.extend(df.iloc[:, 0].dropna().astype(str).tolist())
            elif name.endswith(".csv"):
                df = pd.read_csv(uploaded, header=None, dtype=str)
                raw.extend(df.iloc[:, 0].dropna().astype(str).tolist())
            else:  # .txt
                raw.extend(uploaded.getvalue().decode("utf-8", errors="ignore").splitlines())
        except Exception as exc:
            st.warning(f"Could not read uploaded file: {exc}")

    # Strip, drop blanks, dedupe while preserving order.
    seen: set[str] = set()
    targets: list[str] = []
    for item in raw:
        cleaned = item.strip()
        if cleaned and cleaned.lower() not in seen:
            seen.add(cleaned.lower())
            targets.append(cleaned)
    return targets


# ── fetching ─────────────────────────────────────────────────

def _fetch_dr(target: str) -> dict:
    try:
        for attempt in range(2):
            resp = requests.get(
                API_URL,
                params={"target": target},
                headers={"Accept": "application/json"},
                timeout=REQUEST_TIMEOUT,
            )
            if resp.status_code == 429:  # rate limited — back off once and retry
                if attempt == 0:
                    time.sleep(2)
                    continue
                return {"Target": target, "DR": None, "Status": "Rate limited (429)"}

            if resp.status_code == 200:
                dr = resp.json().get("domain_rating", {}).get("domain_rating")
                return {"Target": target, "DR": dr, "Status": "OK"}

            if resp.status_code == 400:
                return {"Target": target, "DR": None, "Status": "Invalid target"}

            return {"Target": target, "DR": None, "Status": f"Error ({resp.status_code})"}
    except requests.RequestException:
        return {"Target": target, "DR": None, "Status": "Request failed"}


def _run_bulk_check(targets: list[str]):
    total = len(targets)
    progress = st.progress(0, text=f"Checking {total} target(s)…")
    results: list[dict] = []

    with ThreadPoolExecutor(max_workers=MAX_WORKERS) as executor:
        futures = {executor.submit(_fetch_dr, t): t for t in targets}
        for done, future in enumerate(as_completed(futures), start=1):
            results.append(future.result())
            progress.progress(done / total, text=f"({done}/{total}) checked")

    progress.empty()

    # Preserve the user's original input order.
    order = {t: i for i, t in enumerate(targets)}
    results.sort(key=lambda r: order.get(r["Target"], 0))

    df = pd.DataFrame(results, columns=["Target", "DR", "Status"])
    ok = (df["Status"] == "OK").sum()
    failed = total - ok

    if failed:
        st.success(f"✅ {ok} succeeded · ⚠️ {failed} failed (of {total})")
    else:
        st.success(f"✅ All {ok} target(s) checked")

    st.dataframe(
        df,
        use_container_width=True,
        hide_index=True,
        column_config={
            "DR": st.column_config.NumberColumn("DR", format="%.0f"),
        },
    )
    st.caption(f"Source: {ATTRIBUTION} · http://ahrefs.com/legal/domain-rating-license")

    st.download_button(
        label="⬇️ Download as CSV",
        data=df.to_csv(index=False).encode("utf-8"),
        file_name="ahrefs_dr_results.csv",
        mime="text/csv",
    )
