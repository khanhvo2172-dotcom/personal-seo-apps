import io
import os
import re
import uuid
import zipfile
import requests
import streamlit as st
from PIL import Image
from dotenv import load_dotenv
from urllib.parse import urlparse, parse_qs
from features.auth import get_credentials, require_auth

load_dotenv()

PIL_FORMAT_MAP = {".jpg": "JPEG", ".jpeg": "JPEG", ".png": "PNG"}
WEBP_LOSSLESS_THRESHOLD = 90
CLOUDINARY_TEMP_FOLDER = "gdocs_extractor_temp"


def render():
    st.header("Extract & Optimize Images from Google Docs")
    st.caption("Extracts all images from a Google Doc, optimizes them, and downloads a ZIP.")
    _render_quick_guide()

    if not require_auth():
        return

    with st.form("extract_form"):
        doc_url = st.text_input(
            "Google Docs URL",
            placeholder="https://docs.google.com/document/d/.../edit?tab=t.0",
        )
        mode = st.radio(
            "Extraction mode",
            ["Current tab (from URL)", "All tabs"],
            horizontal=True,
        )

        st.markdown("**Optimization Settings**")
        col1, col2, col3, col4 = st.columns(4)
        with col1:
            filename_base = st.text_input("Filename base", value="image")
        with col2:
            width = st.number_input("Width (px)", value=1200, min_value=1, step=10)
        with col3:
            height = st.number_input("Height (px)", value=600, min_value=1, step=10)
        with col4:
            fmt = st.selectbox("Format", [".jpg", ".jpeg", ".png", ".webp"], index=2)

        quality = st.slider("Quality (JPG / WebP lossy)", min_value=1, max_value=100, value=80)
        skip_upscale = st.checkbox(
            "Skip resize if image is already smaller than target dimensions", value=True
        )
        webp_lossless = st.checkbox("WebP: always lossless (overrides auto-threshold)", value=False)

        if fmt == ".webp":
            st.info(
                f"☁️ WebP is processed via **Cloudinary API** for superior quality. "
                f"Images are uploaded temporarily, transformed, then auto-deleted. "
                f"Quality ≥ {WEBP_LOSSLESS_THRESHOLD} or the checkbox above → lossless encoding."
            )

        submitted = st.form_submit_button("⬇️ Extract & Optimize", type="primary")

    if not submitted:
        return
    if not doc_url.strip():
        st.error("Please paste a Google Docs URL.")
        return

    _run_pipeline(
        doc_url.strip(), mode,
        filename_base.strip() or "image",
        int(width), int(height), fmt, quality, skip_upscale, webp_lossless,
    )


def _render_quick_guide():
    with st.expander("How this works"):
        st.markdown(
            """
1. Authenticate with Google in **Settings**.
2. Paste a Google Docs URL.
3. Choose whether to extract images from the current tab or all document tabs.
4. Set filename base, target width/height, output format, and quality.
5. Click **Extract & Optimize**.
6. The app downloads images from the document, resizes/optimizes them, and packages all processed files into a ZIP.

PNG/JPG optimization runs locally. WebP uses Cloudinary for better output quality, so Cloudinary credentials are required when choosing `.webp`.
            """.strip()
        )


# ── Document helpers ──────────────────────────────────────────

def _extract_doc_id(url: str) -> str:
    m = re.search(r"/document/d/([a-zA-Z0-9_-]+)", url)
    if not m:
        raise ValueError("Could not find a Google Docs file ID in the URL.")
    return m.group(1)


def _extract_tab_id(url: str) -> str | None:
    u = urlparse(url)
    q = parse_qs(u.query)
    if "tab" in q:
        return q["tab"][0]
    frag_q = parse_qs(u.fragment.replace("?", "&"))
    if "tab" in frag_q:
        return frag_q["tab"][0]
    return None


def _flatten_tabs(doc: dict) -> list:
    out: list = []
    def _add(tab):
        out.append(tab)
        for child in tab.get("childTabs", []) or []:
            _add(child)
    for t in doc.get("tabs", []) or []:
        _add(t)
    return out


def _tab_title(tab: dict) -> str:
    return (tab.get("tabProperties", {}) or {}).get("title") or "Untitled tab"


def _tab_id(tab: dict) -> str:
    return (tab.get("tabProperties", {}) or {}).get("tabId") or ""


def _fetch_image_bytes(url: str, creds) -> bytes:
    from google.auth.transport.requests import Request as GAuthRequest
    if not creds.valid:
        creds.refresh(GAuthRequest())
    headers = {"Authorization": f"Bearer {creds.token}"}
    r = requests.get(url, headers=headers, stream=True, timeout=60)
    if r.status_code in (401, 403):
        r = requests.get(url, stream=True, timeout=60)
    r.raise_for_status()
    return r.content


def _build_entry(obj_id, embedded, where, segment) -> dict | None:
    img_props = (embedded or {}).get("imageProperties", {}) or {}
    src = img_props.get("sourceUri")
    content = img_props.get("contentUri")
    chosen = src or content
    if not chosen:
        return None
    return {"objectId": obj_id, "where": where, "segment": segment, "chosenUri": chosen}


def _append_inline(inline_id, inline_objects, results, segment):
    obj = inline_objects.get(inline_id)
    if not obj:
        return
    embedded = (obj.get("inlineObjectProperties", {}) or {}).get("embeddedObject", {}) or {}
    entry = _build_entry(inline_id, embedded, "inlineObjects", segment)
    if entry:
        results.append(entry)


def _append_positioned(pos_id, positioned_objects, results, segment):
    obj = positioned_objects.get(pos_id)
    if not obj:
        return
    embedded = (obj.get("positionedObjectProperties", {}) or {}).get("embeddedObject", {}) or {}
    entry = _build_entry(pos_id, embedded, "positionedObjects", segment)
    if entry:
        results.append(entry)


def _traverse(content_list, inline_objects, positioned_objects, results, segment):
    for el in content_list or []:
        if "paragraph" in el:
            p = el.get("paragraph", {}) or {}
            for pid in p.get("positionedObjectIds") or []:
                _append_positioned(pid, positioned_objects, results, segment)
            for pe in p.get("elements") or []:
                io_el = pe.get("inlineObjectElement")
                if io_el and io_el.get("inlineObjectId"):
                    _append_inline(io_el["inlineObjectId"], inline_objects, results, segment)
        elif "table" in el:
            for row in (el["table"].get("tableRows") or []):
                for cell in (row.get("tableCells") or []):
                    _traverse(cell.get("content") or [], inline_objects, positioned_objects, results, segment)
        elif "tableOfContents" in el:
            _traverse(el["tableOfContents"].get("content") or [], inline_objects, positioned_objects, results, segment)


def _collect_entries(tab: dict) -> list:
    doc_tab = tab.get("documentTab", {}) or {}
    inline = doc_tab.get("inlineObjects", {}) or {}
    positioned = doc_tab.get("positionedObjects", {}) or {}
    results: list = []
    _traverse((doc_tab.get("body", {}) or {}).get("content") or [], inline, positioned, results, "body")
    for hid in sorted((doc_tab.get("headers", {}) or {}).keys()):
        _traverse(doc_tab["headers"][hid].get("content") or [], inline, positioned, results, f"header:{hid}")
    for fid in sorted((doc_tab.get("footers", {}) or {}).keys()):
        _traverse(doc_tab["footers"][fid].get("content") or [], inline, positioned, results, f"footer:{fid}")
    for fnid in sorted((doc_tab.get("footnotes", {}) or {}).keys()):
        _traverse(doc_tab["footnotes"][fnid].get("content") or [], inline, positioned, results, f"footnote:{fnid}")
    return results


# ── Image optimization ────────────────────────────────────────

def _quality_label(q: int) -> str:
    if q >= 90: return "best"
    elif q >= 70: return "good"
    elif q >= 50: return "eco"
    else: return "low"


def _optimize_webp(raw_bytes, target_w, target_h, quality, lossless) -> tuple[bytes, str]:
    import cloudinary
    import cloudinary.uploader
    from cloudinary.utils import cloudinary_url as cld_url

    cloud_name = os.getenv("CLOUDINARY_CLOUD_NAME", "")
    api_key = os.getenv("CLOUDINARY_API_KEY", "")
    api_secret = os.getenv("CLOUDINARY_API_SECRET", "")
    if not all([cloud_name, api_key, api_secret]):
        raise RuntimeError("Cloudinary settings are missing. Add them in Settings or Railway variables.")

    cloudinary.config(
        cloud_name=cloud_name,
        api_key=api_key,
        api_secret=api_secret,
        secure=True,
    )

    use_lossless = lossless or quality >= WEBP_LOSSLESS_THRESHOLD
    public_id = f"{CLOUDINARY_TEMP_FOLDER}/img_{uuid.uuid4().hex}"

    upload = cloudinary.uploader.upload(
        raw_bytes, public_id=public_id, resource_type="image", overwrite=True, format=None
    )
    uploaded_id = upload["public_id"]

    tf: dict = {
        "width": target_w, "height": target_h, "crop": "limit", "format": "webp",
        "quality": "auto:best" if use_lossless else f"auto:{_quality_label(quality)}",
    }
    if not use_lossless:
        tf["flags"] = "lossy"

    optimized_url, _ = cld_url(uploaded_id, **tf, secure=True)
    r = requests.get(optimized_url, timeout=60)
    r.raise_for_status()
    result = r.content

    try:
        cloudinary.uploader.destroy(uploaded_id, resource_type="image")
    except Exception:
        pass

    mode = "lossless" if use_lossless else f"lossy q{quality}"
    return result, f"Cloudinary WebP ({mode})"


def _optimize_image(raw_bytes, ext, target_w, target_h, quality, skip_upscale, lossless) -> tuple[bytes, str]:
    if ext.lower() == ".webp":
        return _optimize_webp(raw_bytes, target_w, target_h, quality, lossless)

    pil_fmt = PIL_FORMAT_MAP.get(ext.lower(), "JPEG")
    img = Image.open(io.BytesIO(raw_bytes))

    if pil_fmt == "JPEG" and img.mode != "RGB":
        img = img.convert("RGB")
    elif pil_fmt == "PNG" and img.mode not in ("RGB", "RGBA"):
        img = img.convert("RGBA")

    if skip_upscale and img.width <= target_w and img.height <= target_h:
        resized = img
    elif img.width >= img.height:
        resized = img.resize((target_w, int(target_w * img.height / img.width)), Image.LANCZOS)
    else:
        resized = img.resize((int(target_h * img.width / img.height), target_h), Image.LANCZOS)

    buf = io.BytesIO()
    if pil_fmt == "JPEG":
        resized.save(buf, format="JPEG", quality=quality, subsampling=0, optimize=True, progressive=True)
    else:
        resized.save(buf, format="PNG", optimize=True)

    return buf.getvalue(), ""


# ── Pipeline ──────────────────────────────────────────────────

def _run_pipeline(doc_url, mode, filename_base, target_w, target_h, out_ext, quality, skip_upscale, lossless):
    creds = get_credentials()

    with st.spinner("Fetching document…"):
        try:
            from googleapiclient.discovery import build
            docs_service = build("docs", "v1", credentials=creds)
            drive_service = build("drive", "v3", credentials=creds)

            doc_id = _extract_doc_id(doc_url)
            meta = drive_service.files().get(fileId=doc_id, fields="name").execute()
            doc_name = meta.get("name", "google-doc")
            doc = docs_service.documents().get(documentId=doc_id, includeTabsContent=True).execute()
        except Exception as e:
            st.error(f"Failed to fetch document: {e}")
            return

    all_tabs = _flatten_tabs(doc)
    if not all_tabs:
        st.error("No tabs found in the document.")
        return

    if mode == "All tabs":
        chosen_tabs = all_tabs
    else:
        wanted = _extract_tab_id(doc_url)
        if wanted:
            match = [t for t in all_tabs if _tab_id(t) == wanted]
            chosen_tabs = match or [all_tabs[0]]
        else:
            chosen_tabs = [all_tabs[0]]

    # Collect all entries upfront so we know the total for progress
    tab_entries = {_tab_id(t) or str(i): (_tab_title(t), _collect_entries(t))
                   for i, t in enumerate(chosen_tabs)}
    total = sum(len(e) for _, e in tab_entries.values())

    if total == 0:
        st.warning("No images found in the selected tab(s).")
        return

    st.info(
        f"📄 **{doc_name}** — "
        f"{len(chosen_tabs)} tab(s), {total} image(s) to process"
    )

    progress = st.progress(0, text="Processing images…")
    log_area = st.empty()
    logs: list[str] = []

    zip_buffer = io.BytesIO()
    global_seq = 0
    total_out_bytes = 0

    with zipfile.ZipFile(zip_buffer, "w", zipfile.ZIP_DEFLATED) as zf:
        for tab_key, (t_title, entries) in tab_entries.items():
            logs.append(f"🗂  Tab '{t_title}' — {len(entries)} image(s)")
            log_area.code("\n".join(logs[-25:]))

            for entry in entries:
                global_seq += 1
                out_filename = f"{filename_base}-{global_seq}{out_ext}"
                progress.progress(global_seq / total, text=f"Processing {out_filename}…")

                try:
                    raw = _fetch_image_bytes(entry["chosenUri"], creds)
                    optimized, note = _optimize_image(
                        raw, out_ext, target_w, target_h, quality, skip_upscale, lossless
                    )
                    zf.writestr(out_filename, optimized)
                    total_out_bytes += len(optimized)
                    note_str = f"  [{note}]" if note else ""
                    logs.append(
                        f"  ✅ [{global_seq:04d}] {out_filename}"
                        f"  ({len(raw)//1024} KB → {len(optimized)//1024} KB){note_str}"
                    )
                except Exception as ex:
                    logs.append(f"  ❌ [{global_seq:04d}] {entry['objectId']} — {ex}")

                log_area.code("\n".join(logs[-25:]))

    zip_buffer.seek(0)
    progress.progress(1.0, text="Done!")
    log_area.empty()

    total_mb = total_out_bytes / (1024 * 1024)
    st.success(f"✅ Done! {global_seq} image(s) — {total_mb:.2f} MB total")

    safe_name = re.sub(r"[^a-zA-Z0-9._-]+", "_", doc_name)[:40].strip("_")
    st.download_button(
        label=f"⬇️ Download {safe_name}_images.zip",
        data=zip_buffer,
        file_name=f"{safe_name}_images.zip",
        mime="application/zip",
    )
