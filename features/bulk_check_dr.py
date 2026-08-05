import html as _html
import time
import requests
import pandas as pd
import streamlit as st
from concurrent.futures import ThreadPoolExecutor, as_completed
from urllib.parse import urlsplit

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
4. Review the results table and download it as CSV.

Notes:
- No API key needed — this uses Ahrefs' free public endpoint.
- Domain Rating is on a 100-point logarithmic scale.
- Targets are normalized to their root domain (scheme, `www.`, path and
  port are stripped), so `https://www.example.com/page` and `example.com`
  count as one check — duplicates are removed automatically.
- Data is **Domain Rating by Ahrefs** and subject to the
  [Domain Rating License](http://ahrefs.com/legal/domain-rating-license).
            """.strip()
        )


# ── input parsing ────────────────────────────────────────────

def _normalize_target(item: str) -> str:
    """Reduce any domain/URL to a bare, lowercased root domain.

    So ``https://www.Example.com/path``, ``www.example.com`` and
    ``example.com/`` all collapse to ``example.com`` — one API call, not
    three. Strips scheme, userinfo, port, path and a leading ``www.``.
    """
    cleaned = item.strip()
    if not cleaned:
        return ""
    candidate = cleaned if "://" in cleaned else "//" + cleaned
    try:
        netloc = urlsplit(candidate).netloc.lower()
    except Exception:
        netloc = ""
    if not netloc:
        netloc = cleaned.lower()
    if "@" in netloc:
        netloc = netloc.rsplit("@", 1)[-1]
    netloc = netloc.split(":")[0]
    if netloc.startswith("www."):
        netloc = netloc[4:]
    return netloc


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

    # Normalize to a root domain, drop blanks, dedupe while preserving order.
    seen: set[str] = set()
    targets: list[str] = []
    for item in raw:
        domain = _normalize_target(item)
        if domain and domain not in seen:
            seen.add(domain)
            targets.append(domain)
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

    _render_results_table(df)

    st.download_button(
        label="⬇️ Download as CSV",
        data=df.to_csv(index=False).encode("utf-8"),
        file_name="ahrefs_dr_results.csv",
        mime="text/csv",
    )


# ── Ahrefs-style results table ───────────────────────────────

def _dr_badge(dr) -> str:
    if dr is None or pd.isna(dr):
        return '<span class="dr-badge dr-na">—</span>'
    if dr >= 70:
        cls = "dr-high"
    elif dr >= 40:
        cls = "dr-mid"
    elif dr >= 20:
        cls = "dr-low"
    else:
        cls = "dr-vlow"
    return f'<span class="dr-badge {cls}">{int(round(dr))}</span>'


def _status_badge(status: str) -> str:
    if status == "OK":
        cls = "st-ok"
    elif status == "Invalid target":
        cls = "st-na"
    elif status.startswith("Rate limited"):
        cls = "st-warn"
    else:
        cls = "st-err"
    return f'<span class="status-badge {cls}">{_html.escape(status)}</span>'


def _render_results_table(df: pd.DataFrame):
    rows = []
    for _, r in df.iterrows():
        target = _html.escape(str(r["Target"]))
        rows.append(
            f'<tr>'
            f'<td class="t-target">{target}</td>'
            f'<td class="t-center">{_dr_badge(r["DR"])}</td>'
            f'<td class="t-center">{_status_badge(str(r["Status"]))}</td>'
            f'</tr>'
        )
    rows_html = "".join(rows)

    st.markdown(
        f"""
        <style>
        .ahrefs-dr-wrap {{
            border: 1px solid #e7e9ee;
            border-radius: 12px;
            overflow: hidden;
            box-shadow: 0 1px 3px rgba(16,24,40,.07), 0 1px 2px rgba(16,24,40,.04);
            background: #fff;
            margin: 6px 0 10px 0;
        }}
        .ahrefs-dr-scroll {{ max-height: 600px; overflow: auto; }}
        table.ahrefs-dr {{
            width: 100%;
            border-collapse: separate;
            border-spacing: 0;
            font-family: 'Roboto', sans-serif;
            font-size: 14px;
        }}
        table.ahrefs-dr thead th {{
            position: sticky;
            top: 0;
            z-index: 1;
            background: #f7f8fa;
            color: #667085;
            font-family: 'Google Sans','Roboto',sans-serif;
            font-weight: 600;
            font-size: 11px;
            letter-spacing: .6px;
            text-transform: uppercase;
            padding: 13px 18px;
            text-align: center;
            border-bottom: 1px solid #e7e9ee;
            white-space: nowrap;
        }}
        table.ahrefs-dr thead th.left {{ text-align: left; }}
        table.ahrefs-dr tbody td {{
            padding: 12px 18px;
            border-bottom: 1px solid #f0f1f4;
            color: #1d2939;
            vertical-align: middle;
        }}
        table.ahrefs-dr tbody tr:last-child td {{ border-bottom: none; }}
        table.ahrefs-dr tbody tr:hover {{ background: #f9fafb; }}
        .ahrefs-dr .t-target {{ font-weight: 500; color: #101828; word-break: break-all; }}
        .ahrefs-dr .t-center {{ text-align: center; }}
        .dr-badge {{
            display: inline-flex;
            align-items: center;
            justify-content: center;
            min-width: 40px;
            height: 28px;
            padding: 0 9px;
            border-radius: 8px;
            font-family: 'Google Sans','Roboto',sans-serif;
            font-weight: 700;
            font-size: 13px;
            color: #fff;
        }}
        .dr-high {{ background: #0c9d61; }}
        .dr-mid  {{ background: #f5a623; }}
        .dr-low  {{ background: #f97316; }}
        .dr-vlow {{ background: #e5484d; }}
        .dr-na   {{ background: #eef0f3; color: #98a2b3; font-weight: 600; }}
        .status-badge {{
            display: inline-flex;
            align-items: center;
            gap: 6px;
            padding: 4px 11px;
            border-radius: 999px;
            font-size: 12px;
            font-weight: 600;
            letter-spacing: .2px;
        }}
        .status-badge::before {{
            content: "";
            width: 7px;
            height: 7px;
            border-radius: 50%;
            background: currentColor;
            opacity: .85;
        }}
        .st-ok   {{ background: #e7f7ef; color: #0c9d61; }}
        .st-na   {{ background: #f2f4f7; color: #667085; }}
        .st-warn {{ background: #fef3e2; color: #c4720e; }}
        .st-err  {{ background: #fde8e8; color: #d92d20; }}
        </style>
        <div class="ahrefs-dr-wrap">
          <div class="ahrefs-dr-scroll">
            <table class="ahrefs-dr">
              <thead>
                <tr><th class="left">Target</th><th>DR</th><th>Status</th></tr>
              </thead>
              <tbody>{rows_html}</tbody>
            </table>
          </div>
        </div>
        """,
        unsafe_allow_html=True,
    )
    st.caption(f"Source: {ATTRIBUTION} · http://ahrefs.com/legal/domain-rating-license")
