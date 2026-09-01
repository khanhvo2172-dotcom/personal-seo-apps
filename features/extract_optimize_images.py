import io
import os
import re
import uuid
import zipfile
import unicodedata
import requests
import pandas as pd
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

    input_mode = st.radio(
        "Input mode",
        ["Single document", "Multiple documents (bulk)"],
        horizontal=True,
        key="img_input_mode",
    )

    if input_mode == "Single document":
        _render_single()
    else:
        _render_bulk()


# ── Single-document mode (original flow) ──────────────────────

def _render_single():
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
            height = st.number_input("Height (px)", value=1000, min_value=1, step=10)
        with col4:
            fmt = st.selectbox("Format", [".jpg", ".jpeg", ".png", ".webp"], index=3)

        quality = st.slider("Quality (JPG / WebP lossy)", min_value=1, max_value=100, value=90)
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


# ── Bulk mode (multiple documents) ────────────────────────────

def _render_bulk():
    st.markdown("#### 1. Paste Google Docs URLs (one per row)")
    urls_raw = st.text_area(
        "Google Docs URLs",
        height=160,
        key="bulk_urls",
        placeholder=(
            "https://docs.google.com/document/d/AAA.../edit\n"
            "https://docs.google.com/document/d/BBB.../edit?tab=t.0\n"
            "https://docs.google.com/document/d/CCC.../edit"
        ),
        label_visibility="collapsed",
    )
    mode = st.radio(
        "Extraction mode",
        ["Current tab (from URL)", "All tabs"],
        horizontal=True,
        key="bulk_mode",
    )

    st.markdown("**Optimization Settings** (applied to every document)")
    col1, col2, col3 = st.columns(3)
    with col1:
        width = st.number_input("Width (px)", value=1200, min_value=1, step=10, key="bulk_w")
    with col2:
        height = st.number_input("Height (px)", value=1000, min_value=1, step=10, key="bulk_h")
    with col3:
        fmt = st.selectbox("Format", [".jpg", ".jpeg", ".png", ".webp"], index=3, key="bulk_fmt")

    quality = st.slider(
        "Quality (JPG / WebP lossy)", min_value=1, max_value=100, value=90, key="bulk_q"
    )
    skip_upscale = st.checkbox(
        "Skip resize if image is already smaller than target dimensions",
        value=True, key="bulk_skip",
    )
    webp_lossless = st.checkbox(
        "WebP: always lossless (overrides auto-threshold)", value=False, key="bulk_lossless"
    )
    if fmt == ".webp":
        st.info(
            f"☁️ WebP is processed via **Cloudinary API** for superior quality. "
            f"Images are uploaded temporarily, transformed, then auto-deleted. "
            f"Quality ≥ {WEBP_LOSSLESS_THRESHOLD} or the checkbox above → lossless encoding."
        )

    if st.button("🏷️ Generate Slugs", type="primary", key="bulk_gen"):
        if not urls_raw.strip():
            st.error("Paste at least one Google Docs URL above.")
        else:
            with st.spinner("Fetching document names…"):
                st.session_state["bulk_docs"] = _generate_bulk_docs(urls_raw)

    docs = st.session_state.get("bulk_docs")
    if not docs:
        return

    st.markdown("#### 2. Review & edit slugs")
    st.caption(
        "Each doc's images will be saved in a subfolder named after its slug "
        "(e.g. `costco-dropshipping/costco-dropshipping-1.webp`). Edit any slug below."
    )

    df = pd.DataFrame(
        [{"File name": d["name"], "Slug": d["slug"], "URL": d["url"]} for d in docs]
    )
    edited = st.data_editor(
        df,
        key="bulk_slug_editor",
        use_container_width=True,
        hide_index=True,
        num_rows="fixed",
        column_config={
            "File name": st.column_config.TextColumn("File name", disabled=True, width="medium"),
            "Slug": st.column_config.TextColumn(
                "Slug (editable)", help="Filename base for this document's images", width="medium"
            ),
            "URL": st.column_config.TextColumn("URL", disabled=True, width="large"),
        },
    )

    invalid = [d for d in docs if not d["doc_id"]]
    if invalid:
        st.warning(
            f"{len(invalid)} row(s) could not be read (invalid URL or no access) and will be skipped."
        )

    st.markdown("#### 3. Extract & optimize")
    if st.button("⬇️ Extract & Optimize All", type="primary", key="bulk_extract"):
        final_docs = []
        for i, d in enumerate(docs):
            if not d["doc_id"]:
                continue
            try:
                raw_slug = str(edited.iloc[i]["Slug"])
            except Exception:
                raw_slug = d["slug"]
            slug = _slugify(raw_slug) or d["slug"] or "document"
            final_docs.append({**d, "slug": slug})

        if not final_docs:
            st.error("No valid documents to process.")
        else:
            _run_bulk_pipeline(
                final_docs, mode,
                int(width), int(height), fmt, quality, skip_upscale, webp_lossless,
            )


def _generate_bulk_docs(urls_raw: str) -> list:
    creds = get_credentials()
    from googleapiclient.discovery import build
    drive_service = build("drive", "v3", credentials=creds)

    docs: list = []
    seen_ids: set = set()
    for line in urls_raw.splitlines():
        url = line.strip()
        if not url:
            continue
        try:
            doc_id = _extract_doc_id(url)
        except ValueError:
            docs.append({"url": url, "doc_id": "", "name": "⚠️ Invalid Google Docs URL", "slug": ""})
            continue
        if doc_id in seen_ids:
            continue
        seen_ids.add(doc_id)
        try:
            meta = drive_service.files().get(fileId=doc_id, fields="name").execute()
            name = meta.get("name", "google-doc")
            docs.append({"url": url, "doc_id": doc_id, "name": name, "slug": _slugify(name)})
        except Exception as e:
            docs.append({"url": url, "doc_id": doc_id, "name": f"⚠️ Could not open ({e})", "slug": ""})
    return docs


def _slugify(text: str) -> str:
    s = unicodedata.normalize("NFKD", text or "")
    s = s.encode("ascii", "ignore").decode("ascii")
    s = s.strip().lower()
    s = re.sub(r"[^a-z0-9]+", "-", s)
    s = re.sub(r"-{2,}", "-", s).strip("-")
    return s or "document"


def _render_quick_guide():
    with st.expander("How this works"):
        st.markdown(
            """
1. Authenticate with Google in **Settings**.
2. Pick an **Input mode**:
   - **Single document** — paste one Google Docs URL, set a filename base, and extract.
   - **Multiple documents (bulk)** — paste several Google Docs URLs (one per row).
3. Choose whether to extract images from the current tab or all document tabs.
4. Set target width/height, output format, and quality.

**Bulk mode extra steps:** after pasting URLs, click **Generate Slugs** to auto-create a
filename slug from each document's name (e.g. *Costco Dropshipping* → `costco-dropshipping`).
Review and edit the slugs in the table, then click **Extract & Optimize All**. Each document's
images are packaged into their own subfolder inside a single ZIP.

PNG/JPG optimization runs locally. WebP uses Cloudinary for better output quality, so Cloudinary
credentials are required when choosing `.webp`.
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


def _pick_tabs(all_tabs: list, url: str, mode: str) -> list:
    if mode == "All tabs":
        return all_tabs
    wanted = _extract_tab_id(url)
    if wanted:
        match = [t for t in all_tabs if _tab_id(t) == wanted]
        return match or [all_tabs[0]]
    return [all_tabs[0]]


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


# ── Pipeline (single document) ────────────────────────────────

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

    chosen_tabs = _pick_tabs(all_tabs, doc_url, mode)

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


# ── Pipeline (bulk documents) ─────────────────────────────────

def _run_bulk_pipeline(docs, mode, target_w, target_h, out_ext, quality, skip_upscale, lossless):
    creds = get_credentials()
    from googleapiclient.discovery import build
    docs_service = build("docs", "v1", credentials=creds)

    # Pass 1: fetch each doc and collect its image entries (so we know the grand total).
    prepared: list = []  # (slug, name, entries)
    fetch_fail = 0
    with st.spinner("Reading documents…"):
        for d in docs:
            try:
                doc = docs_service.documents().get(
                    documentId=d["doc_id"], includeTabsContent=True
                ).execute()
                all_tabs = _flatten_tabs(doc)
                if not all_tabs:
                    prepared.append((d["slug"], d["name"], []))
                    continue
                chosen = _pick_tabs(all_tabs, d["url"], mode)
                entries: list = []
                for t in chosen:
                    entries.extend(_collect_entries(t))
                prepared.append((d["slug"], d["name"], entries))
            except Exception as e:
                fetch_fail += 1
                prepared.append((d["slug"], f"{d['name']} — FETCH FAILED: {e}", []))

    total = sum(len(e) for _, _, e in prepared)
    if total == 0:
        st.warning("No images found across the selected document(s).")
        return

    st.info(
        f"📚 {len(docs)} document(s), {total} image(s) to process"
        + (f" — ⚠️ {fetch_fail} document(s) could not be read" if fetch_fail else "")
    )

    progress = st.progress(0, text="Processing images…")
    log_area = st.empty()
    logs: list[str] = []

    zip_buffer = io.BytesIO()
    used_paths: set = set()
    processed = 0
    written = 0
    total_out_bytes = 0

    with zipfile.ZipFile(zip_buffer, "w", zipfile.ZIP_DEFLATED) as zf:
        for slug, name, entries in prepared:
            logs.append(f"🗂  {name}  →  {slug}/  ({len(entries)} image(s))")
            log_area.code("\n".join(logs[-25:]))

            seq = 0
            for entry in entries:
                seq += 1
                processed += 1
                path = _unique_path(f"{slug}/{slug}-{seq}{out_ext}", used_paths)
                progress.progress(processed / total, text=f"Processing {path}…")

                try:
                    raw = _fetch_image_bytes(entry["chosenUri"], creds)
                    optimized, note = _optimize_image(
                        raw, out_ext, target_w, target_h, quality, skip_upscale, lossless
                    )
                    zf.writestr(path, optimized)
                    written += 1
                    total_out_bytes += len(optimized)
                    note_str = f"  [{note}]" if note else ""
                    logs.append(
                        f"  ✅ {path}"
                        f"  ({len(raw)//1024} KB → {len(optimized)//1024} KB){note_str}"
                    )
                except Exception as ex:
                    logs.append(f"  ❌ {path} — {ex}")

                log_area.code("\n".join(logs[-25:]))

    zip_buffer.seek(0)
    progress.progress(1.0, text="Done!")
    log_area.empty()

    total_mb = total_out_bytes / (1024 * 1024)
    failed = processed - written
    msg = f"✅ Done! {written} image(s) across {len(docs)} document(s) — {total_mb:.2f} MB total"
    if failed:
        msg += f"  (⚠️ {failed} image(s) failed — see log above)"
    st.success(msg)

    st.download_button(
        label="⬇️ Download bulk_images.zip",
        data=zip_buffer,
        file_name="bulk_images.zip",
        mime="application/zip",
    )


def _unique_path(path: str, used: set) -> str:
    if path not in used:
        used.add(path)
        return path
    stem, dot, ext = path.rpartition(".")
    i = 2
    while True:
        cand = f"{stem}-{i}.{ext}" if dot else f"{path}-{i}"
        if cand not in used:
            used.add(cand)
            return cand
        i += 1
