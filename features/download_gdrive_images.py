import io
import re
import zipfile
import tempfile
import streamlit as st
from pathlib import Path


def render():
    st.header("📥 Download Google Drive Images")
    st.caption(
        "Downloads images from Google Drive links and packages them into a ZIP. "
        "Files must be shared with **'Anyone with the link'**."
    )

    links_raw = st.text_area(
        "Google Drive Links — one per line",
        placeholder=(
            "https://drive.google.com/file/d/FILE_ID/view\n"
            "https://drive.google.com/open?id=FILE_ID\n"
            "https://drive.google.com/uc?id=FILE_ID"
        ),
        height=200,
    )
    zip_name = st.text_input("ZIP filename (without extension)", value="downloaded_images")

    if st.button("⬇️ Download & ZIP", type="primary"):
        if not links_raw.strip():
            st.error("Please paste at least one Google Drive link.")
            return
        links = [ln.strip() for ln in links_raw.splitlines() if ln.strip()]
        _run_download(links, zip_name.strip() or "downloaded_images")


# ── helpers ──────────────────────────────────────────────────

def _extract_file_id(url: str) -> str | None:
    url = url.strip()
    patterns = [
        r"/file/d/([a-zA-Z0-9_-]{20,})",
        r"[?&]id=([a-zA-Z0-9_-]{20,})",
        r"/d/([a-zA-Z0-9_-]{20,})",
    ]
    for p in patterns:
        m = re.search(p, url)
        if m:
            return m.group(1)
    return None


def _run_download(links: list[str], zip_name: str):
    try:
        import gdown
    except ImportError:
        st.error("gdown is not installed. Run: `pip install gdown`")
        return

    log_area = st.empty()
    logs: list[str] = []
    downloaded_paths: list[str] = []
    failed: list[str] = []

    with tempfile.TemporaryDirectory() as tmp_dir:
        for i, url in enumerate(links, 1):
            file_id = _extract_file_id(url)
            if not file_id:
                logs.append(f"[{i}/{len(links)}] ❌  Cannot parse ID from: {url}")
                failed.append(url)
                log_area.code("\n".join(logs))
                continue

            gdrive_url = f"https://drive.google.com/uc?id={file_id}"
            logs.append(f"[{i}/{len(links)}] ⬇️  Downloading file ID: {file_id}")
            log_area.code("\n".join(logs))

            try:
                out_path = gdown.download(
                    gdrive_url,
                    output=tmp_dir + "/",
                    quiet=True,
                    fuzzy=True,
                )
                if out_path and Path(out_path).exists():
                    downloaded_paths.append(out_path)
                    logs.append(f"[{i}/{len(links)}] ✅  Saved → {Path(out_path).name}")
                else:
                    logs.append(f"[{i}/{len(links)}] ❌  No file returned (check sharing permissions)")
                    failed.append(url)
            except Exception as e:
                logs.append(f"[{i}/{len(links)}] ❌  Error: {e}")
                failed.append(url)

            log_area.code("\n".join(logs))

        log_area.empty()

        if not downloaded_paths:
            st.error(
                "No files were downloaded. "
                "Make sure files are set to **'Anyone with the link'** in Google Drive."
            )
            return

        # Build ZIP in memory from the temp dir files
        zip_buffer = io.BytesIO()
        with zipfile.ZipFile(zip_buffer, "w", zipfile.ZIP_DEFLATED) as zf:
            for fp in downloaded_paths:
                zf.write(fp, arcname=Path(fp).name)
        zip_buffer.seek(0)

        size_mb = len(zip_buffer.getvalue()) / (1024 * 1024)
        st.success(
            f"✅ {len(downloaded_paths)} file(s) downloaded ({size_mb:.2f} MB). "
            + (f"⚠️ {len(failed)} link(s) failed." if failed else "")
        )
        if failed:
            with st.expander("Failed links"):
                st.write("\n".join(f"• {u}" for u in failed))

        st.download_button(
            label=f"⬇️ Download {zip_name}.zip",
            data=zip_buffer,
            file_name=f"{zip_name}.zip",
            mime="application/zip",
        )
