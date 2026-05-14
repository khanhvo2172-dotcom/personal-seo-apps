import io
import re
import zipfile
import tempfile
import requests
import streamlit as st
from pathlib import Path
from urllib.parse import unquote


def render():
    st.header("Download Images using GDrive Links")
    st.caption(
        "Downloads images from Google Drive links and packages them into a ZIP. "
        "Files must be shared with **'Anyone with the link'**."
    )
    _render_quick_guide()

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


def _render_quick_guide():
    with st.expander("How this works"):
        st.markdown(
            """
1. Paste Google Drive image links, one link per line.
2. Enter the ZIP filename you want.
3. Click **Download & ZIP**.
4. The app extracts each Google Drive file ID, downloads each image, and puts all successful downloads into one ZIP file.
5. If a link fails, the app lists it so you can fix sharing permission or remove it.

Public links should be shared as **Anyone with the link**. If you are authenticated in Settings, the app can also try the Google Drive API for files your account can access.
            """.strip()
        )


# ── helpers ──────────────────────────────────────────────────

def _extract_file_id(url: str) -> str | None:
    url = url.strip()
    patterns = [
        r"/file/d/([a-zA-Z0-9_-]{20,})",
        r"/folders/([a-zA-Z0-9_-]{20,})",
        r"[?&]id=([a-zA-Z0-9_-]{20,})",
        r"/d/([a-zA-Z0-9_-]{20,})",
    ]
    for p in patterns:
        m = re.search(p, url)
        if m:
            return m.group(1)
    return None


def _safe_filename(name: str) -> str:
    name = Path(name).name.strip().strip('"')
    name = re.sub(r'[<>:"/\\|?*\x00-\x1f]+', "_", name)
    return name or "google_drive_file"


def _filename_from_content_disposition(header: str) -> str | None:
    if not header:
        return None

    m = re.search(r"filename\*=UTF-8''([^;]+)", header, flags=re.I)
    if m:
        return _safe_filename(unquote(m.group(1)))

    m = re.search(r'filename="?([^";]+)"?', header, flags=re.I)
    if m:
        return _safe_filename(m.group(1))

    return None


def _extension_from_content_type(content_type: str) -> str:
    content_type = content_type.split(";", 1)[0].strip().lower()
    return {
        "image/jpeg": ".jpg",
        "image/png": ".png",
        "image/gif": ".gif",
        "image/webp": ".webp",
        "image/svg+xml": ".svg",
        "image/bmp": ".bmp",
        "image/tiff": ".tif",
    }.get(content_type, "")


def _is_download_response(resp: requests.Response) -> bool:
    content_type = resp.headers.get("content-type", "").lower()
    content_disposition = resp.headers.get("content-disposition", "").lower()
    return (
        resp.ok
        and "text/html" not in content_type
        and (
            "attachment" in content_disposition
            or content_type.startswith("image/")
            or content_type == "application/octet-stream"
        )
    )


def _confirm_url_from_html(html: str) -> str | None:
    patterns = [
        r'href="(/uc\?[^"]*confirm=[^"]*)"',
        r'href="(https://drive\.google\.com/uc\?[^"]*confirm=[^"]*)"',
        r'action="(https://drive\.google\.com/uc\?[^"]*)"',
    ]
    for pattern in patterns:
        m = re.search(pattern, html)
        if m:
            url = m.group(1).replace("&amp;", "&")
            if url.startswith("/"):
                return f"https://drive.google.com{url}"
            return url
    return None


def _write_response(resp: requests.Response, output_dir: str, file_id: str) -> str:
    filename = _filename_from_content_disposition(resp.headers.get("content-disposition", ""))
    if not filename:
        ext = _extension_from_content_type(resp.headers.get("content-type", ""))
        filename = f"{file_id}{ext}"

    out_path = Path(output_dir) / _safe_filename(filename)
    counter = 2
    while out_path.exists():
        out_path = Path(output_dir) / f"{out_path.stem}_{counter}{out_path.suffix}"
        counter += 1

    with out_path.open("wb") as f:
        for chunk in resp.iter_content(chunk_size=1024 * 1024):
            if chunk:
                f.write(chunk)

    if out_path.stat().st_size == 0:
        out_path.unlink(missing_ok=True)
        raise RuntimeError("Google Drive returned an empty file.")

    return str(out_path)


def _download_public_file(file_id: str, output_dir: str) -> str:
    session = requests.Session()
    base_url = "https://drive.google.com/uc"
    params = {"export": "download", "id": file_id}

    resp = session.get(base_url, params=params, stream=True, timeout=90)
    if _is_download_response(resp):
        return _write_response(resp, output_dir, file_id)

    confirm_token = next(
        (value for key, value in session.cookies.items() if key.startswith("download_warning")),
        None,
    )
    if confirm_token:
        resp = session.get(
            base_url,
            params={**params, "confirm": confirm_token},
            stream=True,
            timeout=90,
        )
        if _is_download_response(resp):
            return _write_response(resp, output_dir, file_id)

    html = resp.text if "text/html" in resp.headers.get("content-type", "").lower() else ""
    confirm_url = _confirm_url_from_html(html)
    if confirm_url:
        resp = session.get(confirm_url, stream=True, timeout=90)
        if _is_download_response(resp):
            return _write_response(resp, output_dir, file_id)

    if "quota" in html.lower() or "too many users" in html.lower():
        raise RuntimeError("Google Drive download quota was exceeded for this file.")
    if "access denied" in html.lower() or "permission" in html.lower():
        raise RuntimeError("Google Drive denied access to this file.")
    if "folder" in html.lower():
        raise RuntimeError("This looks like a folder link. Please paste direct file links.")

    status = f"HTTP {resp.status_code}" if resp.status_code else "unknown response"
    raise RuntimeError(f"Google Drive did not return a downloadable file ({status}).")


def _download_with_drive_api(file_id: str, output_dir: str) -> str | None:
    try:
        from features.auth import get_credentials
        from googleapiclient.discovery import build
        from googleapiclient.http import MediaIoBaseDownload
    except Exception:
        return None

    creds = get_credentials()
    if not creds:
        return None

    service = build("drive", "v3", credentials=creds)
    meta = service.files().get(fileId=file_id, fields="name,mimeType").execute()
    request = service.files().get_media(fileId=file_id)
    buf = io.BytesIO()
    downloader = MediaIoBaseDownload(buf, request)

    done = False
    while not done:
        _, done = downloader.next_chunk()

    filename = _safe_filename(meta.get("name") or file_id)
    if "." not in filename:
        filename += _extension_from_content_type(meta.get("mimeType", ""))

    out_path = Path(output_dir) / filename
    out_path.write_bytes(buf.getvalue())
    return str(out_path)


def _download_gdrive_file(file_id: str, output_dir: str) -> str:
    try:
        return _download_public_file(file_id, output_dir)
    except Exception as public_error:
        api_path = _download_with_drive_api(file_id, output_dir)
        if api_path:
            return api_path
        raise public_error


def _run_download(links: list[str], zip_name: str):

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

            logs.append(f"[{i}/{len(links)}] ⬇️  Downloading file ID: {file_id}")
            log_area.code("\n".join(logs))

            try:
                out_path = _download_gdrive_file(file_id, tmp_dir)
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
