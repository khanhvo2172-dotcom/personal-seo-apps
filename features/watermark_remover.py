"""Remove AI/metadata fingerprints from uploaded images.

Self-contained port of the `imgclean` engine from
https://github.com/lhfer/image-fingerprint-remover — detection + cleaning for
JPEG and PNG, offered as a Streamlit feature.

Three cleaning modes:
  • safe     — strip identifying metadata only; pixels stay byte-identical.
  • paranoid — safe + re-encode pixels with light Gaussian noise (σ=0.5) to
               neutralize JPEG quantization-table fingerprints.
  • nuclear  — paranoid + resize / crop / color-shift to disrupt frequency-domain
               robust watermarks (SynthID, Digimarc, IMATAG). Visible-but-mild.

Everything runs locally in the app process; no image leaves the server.
"""
from __future__ import annotations

import hashlib
import io
import re
import struct
import zipfile
import zlib
from dataclasses import dataclass, field
from enum import Enum

import numpy as np
import pandas as pd
import streamlit as st
from PIL import Image

# ══════════════════════════════════════════════════════════════════════════════
# findings.py — severity / category enums + report dataclasses
# ══════════════════════════════════════════════════════════════════════════════


class Severity(str, Enum):
    INFO = "info"
    LOW = "low"
    MED = "medium"
    HIGH = "high"
    CRITICAL = "critical"


_SEV_RANK = {
    Severity.INFO: 0,
    Severity.LOW: 1,
    Severity.MED: 2,
    Severity.HIGH: 3,
    Severity.CRITICAL: 4,
}
_SEV_ICON = {
    Severity.INFO: "⚪",
    Severity.LOW: "🔵",
    Severity.MED: "🟡",
    Severity.HIGH: "🟠",
    Severity.CRITICAL: "🔴",
}


class Category(str, Enum):
    EXIF = "exif"
    XMP = "xmp"
    IPTC = "iptc"
    ICC = "icc"
    THUMBNAIL = "thumbnail"
    PNG_TEXT = "png_text"
    PNG_CHUNK = "png_chunk_unknown"
    JPEG_APP = "jpeg_app_segment"
    JPEG_COMMENT = "jpeg_comment"
    JPEG_QT = "jpeg_quantization_table"
    C2PA = "c2pa_manifest"
    AI_PROMPT = "ai_generation_prompt"
    AI_TAG = "ai_generation_tag"
    GPS = "gps_location"
    SERIAL = "device_serial"
    TRAILING = "trailing_bytes"
    FS_META = "filesystem_metadata"
    OTHER = "other"


_CAT_LABELS = {
    Category.EXIF: "EXIF metadata",
    Category.XMP: "XMP packet",
    Category.IPTC: "Photoshop / IPTC",
    Category.ICC: "ICC color profile",
    Category.THUMBNAIL: "Embedded thumbnail",
    Category.PNG_TEXT: "PNG text chunk",
    Category.PNG_CHUNK: "Non-standard PNG chunk",
    Category.JPEG_APP: "JPEG APPn segment",
    Category.JPEG_COMMENT: "JPEG comment",
    Category.JPEG_QT: "Quantization table",
    Category.C2PA: "C2PA / Content Credentials",
    Category.AI_PROMPT: "AI generation prompt",
    Category.AI_TAG: "AI generation tag",
    Category.GPS: "GPS location",
    Category.SERIAL: "Device serial",
    Category.TRAILING: "Trailing bytes",
    Category.FS_META: "Filesystem metadata",
    Category.OTHER: "Other",
}


@dataclass
class Finding:
    category: Category
    severity: Severity
    location: str
    name: str
    detail: str
    size_bytes: int = 0
    value_preview: str = ""
    source: str = ""
    extra: dict = field(default_factory=dict)


@dataclass
class InspectReport:
    format: str
    file_size: int
    findings: list = field(default_factory=list)
    notes: list = field(default_factory=list)

    @property
    def is_clean(self) -> bool:
        # Clean = no MED/HIGH/CRITICAL findings. INFO/LOW are residual fingerprints
        # (quantization tables, standard ICC) that only paranoid/nuclear neutralize.
        return not any(
            f.severity in (Severity.MED, Severity.HIGH, Severity.CRITICAL)
            for f in self.findings
        )


# ══════════════════════════════════════════════════════════════════════════════
# fingerprints.py — known AI / editor signatures
# ══════════════════════════════════════════════════════════════════════════════

PNG_AI_TEXT_KEYS = {
    "parameters": ("Stable Diffusion (A1111)",
                   "Automatic1111 web-UI stores full prompt, seed, model hash, sampler in this key."),
    "prompt": ("ComfyUI / generic prompt",
               "ComfyUI stores the API prompt JSON in this key."),
    "workflow": ("ComfyUI workflow",
                 "ComfyUI stores the full node graph JSON in this key."),
    "comfy": ("ComfyUI", "Marker key written by ComfyUI exporters."),
    "sd-metadata": ("InvokeAI",
                    "InvokeAI stores its generation metadata under this key."),
    "invokeai": ("InvokeAI", "InvokeAI generation marker."),
    "novelai": ("NovelAI", "NovelAI generation marker."),
    "dream": ("Dream/InvokeAI", "InvokeAI dream parameters key."),
    "openai": ("OpenAI / ChatGPT", "OpenAI image-generation marker."),
    "dall-e": ("DALL-E", "DALL-E generation marker."),
    "midjourney": ("Midjourney", "Midjourney generation marker."),
    "firefly": ("Adobe Firefly", "Adobe Firefly generation marker."),
}

PNG_AI_VALUE_PATTERNS = [
    (re.compile(r"\bSteps:\s*\d+", re.I), "Stable Diffusion A1111 parameter block"),
    (re.compile(r"\bSampler:\s*[A-Za-z]", re.I), "Stable Diffusion sampler block"),
    (re.compile(r"\bModel hash:\s*[0-9a-f]{6,}", re.I), "Stable Diffusion model hash"),
    (re.compile(r"\bCFG scale:\s*[\d.]+", re.I), "Stable Diffusion CFG scale"),
    (re.compile(r'"class_type"\s*:', re.I), "ComfyUI node graph JSON"),
    (re.compile(r"midjourney|--ar\s+\d+:\d+|--v\s+\d", re.I), "Midjourney prompt block"),
    (re.compile(r"dall[\s\-]?e|gpt[\s\-]?image|openai", re.I), "OpenAI/DALL-E marker"),
    (re.compile(r"stable[\s\-]?diffusion|automatic1111|a1111", re.I), "Stable Diffusion marker"),
    (re.compile(r"firefly|adobe stock", re.I), "Adobe marker"),
    (re.compile(r"trainedalgorithmicmedia|compositewithtrainedalgorithmicmedia", re.I),
     "IPTC DigitalSourceType = AI-generated"),
]

XMP_AI_PATTERNS = [
    (re.compile(r"Iptc4xmpExt:DigitalSourceType[^<]*trainedAlgorithmicMedia", re.I),
     "IPTC DigitalSourceType = trainedAlgorithmicMedia"),
    (re.compile(r"Iptc4xmpExt:DigitalSourceType[^<]*compositeWithTrainedAlgorithmicMedia", re.I),
     "IPTC DigitalSourceType = compositeWithTrainedAlgorithmicMedia"),
    (re.compile(r"<xmpMM:History>.*?</xmpMM:History>", re.S | re.I),
     "Adobe XMP edit history (may leak editor + actions)"),
    (re.compile(r"<photoshop:[A-Z]\w+>", re.I), "Photoshop XMP tag"),
    (re.compile(r"firefly|adobe stock|adobestock", re.I), "Adobe Firefly/Stock marker"),
    (re.compile(r"openai|chatgpt|dall[\s\-]?e", re.I), "OpenAI/DALL-E marker"),
    (re.compile(r"midjourney", re.I), "Midjourney marker"),
    (re.compile(r"stable[\s\-]?diffusion|stability\.ai", re.I), "Stable Diffusion marker"),
    (re.compile(r"generativeAI|generative-ai", re.I), "Generic generative-AI marker"),
]

JUMBF_MAGIC = b"jumb"
C2PA_LABELS = [b"c2pa", b"c2ma", b"c2as", b"c2cl"]
PHOTOSHOP_HEADER = b"Photoshop 3.0\x00"
ADOBE_APP14 = b"Adobe\x00"

EXIF_LEAK_TAGS = {
    "Make", "Model", "Software", "BodySerialNumber", "SerialNumber",
    "LensSerialNumber", "OwnerName", "Artist", "Copyright", "UserComment",
    "ImageUniqueID", "DateTimeOriginal", "DateTimeDigitized", "ImageDescription",
    "HostComputer", "CameraOwnerName",
}
GPS_TAGS_PREFIX = "GPS"

PNG_CRITICAL_CHUNKS = {b"IHDR", b"PLTE", b"IDAT", b"IEND"}
PNG_SAFE_ANCILLARY = {
    b"tRNS", b"gAMA", b"cHRM", b"sBIT", b"bKGD", b"hIST", b"sRGB",
    b"acTL", b"fcTL", b"fdAT",  # APNG animation
}

PNG_HEADER = b"\x89PNG\r\n\x1a\n"
SOI = b"\xff\xd8"
EOI = b"\xff\xd9"
SOS = 0xDA


# ══════════════════════════════════════════════════════════════════════════════
# detect/png.py — PNG chunk-level inspector
# ══════════════════════════════════════════════════════════════════════════════


def _iter_chunks(data: bytes):
    """Yield (offset, type, data) for each PNG chunk. Tolerates trailing bytes."""
    if not data.startswith(PNG_HEADER):
        return
    off = len(PNG_HEADER)
    n = len(data)
    while off + 8 <= n:
        (length,) = struct.unpack(">I", data[off:off + 4])
        ctype = data[off + 4:off + 8]
        body = data[off + 8:off + 8 + length]
        yield off, ctype, body
        off += 12 + length
        if ctype == b"IEND":
            break


def _decode_png_text(body: bytes, chunk_type: bytes):
    """Decode a tEXt/zTXt/iTXt chunk body into (key, value)."""
    try:
        if chunk_type == b"tEXt":
            k, _, v = body.partition(b"\x00")
            return k.decode("latin1", "replace"), v.decode("latin1", "replace")
        if chunk_type == b"zTXt":
            k, _, rest = body.partition(b"\x00")
            if not rest:
                return k.decode("latin1", "replace"), ""
            comp = rest[1:]  # rest[0] is compression method (0 = zlib)
            try:
                v = zlib.decompress(comp).decode("utf-8", "replace")
            except zlib.error:
                v = f"<zlib decode error, {len(comp)} bytes>"
            return k.decode("latin1", "replace"), v
        if chunk_type == b"iTXt":
            try:
                k, rest = body.split(b"\x00", 1)
                comp_flag = rest[0]
                lang, rest2 = rest[2:].split(b"\x00", 1)
                trans, text = rest2.split(b"\x00", 1)
                if comp_flag:
                    try:
                        text = zlib.decompress(text)
                    except zlib.error:
                        pass
                return k.decode("utf-8", "replace"), text.decode("utf-8", "replace")
            except (ValueError, IndexError):
                return "<malformed iTXt>", body[:200].decode("utf-8", "replace")
    except Exception as exc:
        return f"<error: {exc}>", ""
    return "<unknown>", ""


def _match_ai_keys(key: str, value: str):
    """Return (label, why) if the text chunk looks like an AI fingerprint."""
    kl = key.lower().strip()
    if kl in PNG_AI_TEXT_KEYS:
        return PNG_AI_TEXT_KEYS[kl]
    for pat, why in PNG_AI_VALUE_PATTERNS:
        if pat.search(value):
            return "AI generation marker", why
    for pat, why in XMP_AI_PATTERNS:
        if pat.search(value):
            return "AI generation marker (XMP)", why
    return None, None


def _inspect_png(data: bytes, report: InspectReport) -> None:
    if not data.startswith(PNG_HEADER):
        report.notes.append("PNG magic missing — not a PNG.")
        return

    last_end = len(PNG_HEADER)
    saw_iend = False
    for off, ctype, body in _iter_chunks(data):
        last_end = off + 12 + len(body)
        ctype_s = ctype.decode("latin1", "replace")
        if ctype in PNG_CRITICAL_CHUNKS or ctype in PNG_SAFE_ANCILLARY:
            if ctype == b"IEND":
                saw_iend = True
            continue

        if ctype in (b"tEXt", b"zTXt", b"iTXt"):
            key, value = _decode_png_text(body, ctype)
            label, why = _match_ai_keys(key, value)
            severity = Severity.HIGH if label else Severity.MED
            category = Category.AI_PROMPT if label else Category.PNG_TEXT
            preview = value[:240].replace("\n", " ")
            detail = f"PNG {ctype_s} key={key!r} ({len(value)} chars)" + (f" — {why}" if why else "")
            report.findings.append(Finding(
                category=category, severity=severity,
                location=f"PNG {ctype_s} @offset={off}",
                name=label or f"PNG text chunk: {key}",
                detail=detail, size_bytes=len(body), value_preview=preview, source="png.text",
            ))
            continue

        if ctype == b"eXIf":
            report.findings.append(Finding(
                category=Category.EXIF, severity=Severity.HIGH,
                location=f"PNG eXIf @offset={off}", name="Embedded EXIF block in PNG",
                detail="PNG carries a full EXIF block (may include camera Make/Model, GPS, serial).",
                size_bytes=len(body), source="png.exif",
            ))
            continue

        if ctype == b"iCCP":
            name, _, _ = body.partition(b"\x00")
            report.findings.append(Finding(
                category=Category.ICC, severity=Severity.LOW,
                location=f"PNG iCCP @offset={off}",
                name=f"ICC profile: {name.decode('latin1', 'replace')}",
                detail="Embedded ICC color profile — may carry custom strings or UUIDs.",
                size_bytes=len(body), source="png.iccp",
            ))
            continue

        if ctype == b"tIME":
            if len(body) == 7:
                y, mo, d, h, mi, s = struct.unpack(">HBBBBB", body[:7])
                report.findings.append(Finding(
                    category=Category.OTHER, severity=Severity.LOW,
                    location=f"PNG tIME @offset={off}", name="PNG last-modification timestamp",
                    detail=f"{y:04d}-{mo:02d}-{d:02d} {h:02d}:{mi:02d}:{s:02d} UTC",
                    size_bytes=len(body), source="png.time",
                ))
            continue

        if ctype == b"pHYs":
            report.findings.append(Finding(
                category=Category.OTHER, severity=Severity.INFO,
                location=f"PNG pHYs @offset={off}", name="Pixel-density chunk",
                detail="Editor-set DPI/aspect — mild fingerprint.",
                size_bytes=len(body), source="png.phys",
            ))
            continue

        if ctype == b"caBX" or (JUMBF_MAGIC in body[:64] and any(lbl in body[:200] for lbl in C2PA_LABELS)):
            report.findings.append(Finding(
                category=Category.C2PA, severity=Severity.CRITICAL,
                location=f"PNG {ctype_s} @offset={off}",
                name="C2PA / Content Credentials manifest",
                detail=("JUMBF box containing C2PA manifest — encodes signing entity "
                        "(camera, OpenAI, Adobe, Google), assertions, edit history, "
                        "and a content-binding hash."),
                size_bytes=len(body),
                value_preview=body[:200].decode("latin1", "replace"), source="png.c2pa",
            ))
            continue

        report.findings.append(Finding(
            category=Category.PNG_CHUNK, severity=Severity.MED,
            location=f"PNG {ctype_s} @offset={off}",
            name=f"Non-standard PNG chunk: {ctype_s}",
            detail="Unknown ancillary chunk — may carry vendor identifiers.",
            size_bytes=len(body),
            value_preview=body[:200].decode("latin1", "replace"), source="png.unknown",
        ))

    if saw_iend and last_end < len(data):
        trailing = len(data) - last_end
        report.findings.append(Finding(
            category=Category.TRAILING, severity=Severity.HIGH,
            location=f"PNG trailing bytes @offset={last_end}",
            name="Trailing bytes after IEND",
            detail=f"{trailing} extra bytes after PNG end-of-file marker — possible appended payload.",
            size_bytes=trailing,
            value_preview=data[last_end:last_end + 200].decode("latin1", "replace"),
            source="png.trailing",
        ))


# ══════════════════════════════════════════════════════════════════════════════
# detect/jpeg.py — JPEG marker-level inspector
# ══════════════════════════════════════════════════════════════════════════════


def _iter_segments(data: bytes):
    """Yield (offset, marker_code, segment_body) for each JPEG marker segment."""
    if not data.startswith(SOI):
        return
    off = 2
    n = len(data)
    while off < n:
        if data[off] != 0xFF:
            return
        while off < n and data[off] == 0xFF:
            off += 1
        if off >= n:
            return
        marker = data[off]
        off += 1
        if marker in (0xD0, 0xD1, 0xD2, 0xD3, 0xD4, 0xD5, 0xD6, 0xD7, 0x01, 0xD8, 0xD9):
            yield off - 2, marker, b""
            if marker == 0xD9:  # EOI
                return
            continue
        if off + 2 > n:
            return
        (length,) = struct.unpack(">H", data[off:off + 2])
        body = data[off + 2:off + length]
        yield off - 2, marker, body
        off += length
        if marker == SOS:
            while off < n - 1:
                if data[off] == 0xFF and data[off + 1] != 0x00 and data[off + 1] not in (
                    0xD0, 0xD1, 0xD2, 0xD3, 0xD4, 0xD5, 0xD6, 0xD7
                ):
                    break
                off += 1


def _check_xmp(value: str):
    for pat, why in XMP_AI_PATTERNS:
        if pat.search(value):
            return "AI generation marker (XMP)", why
    return None, None


def _parse_exif(body: bytes):
    """Return (notable tag list, has_gps, has_thumbnail). Best-effort minimal parser."""
    if not body.startswith(b"Exif\x00\x00"):
        return [], False, False
    tiff = body[6:]
    if len(tiff) < 8:
        return [], False, False
    endian = tiff[:2]
    if endian == b"II":
        fmt = "<"
    elif endian == b"MM":
        fmt = ">"
    else:
        return [], False, False
    magic = struct.unpack(fmt + "H", tiff[2:4])[0]
    if magic != 0x002A:
        return [], False, False
    ifd0_off = struct.unpack(fmt + "I", tiff[4:8])[0]

    TAG_NAMES = {
        0x010F: "Make", 0x0110: "Model", 0x0131: "Software",
        0x013B: "Artist", 0x8298: "Copyright", 0x9286: "UserComment",
        0xA420: "ImageUniqueID", 0xC62F: "BodySerialNumber",
        0xA431: "SerialNumber", 0xA432: "LensSerialNumber",
        0x9003: "DateTimeOriginal", 0x9004: "DateTimeDigitized",
        0x010E: "ImageDescription", 0x013C: "HostComputer",
        0x8825: "GPSIFD", 0x8769: "ExifIFD",
    }

    tags = []
    has_gps = False
    has_thumb = False

    def read_ifd(ifd_off: int, depth: int = 0) -> int:
        nonlocal has_gps, has_thumb
        if ifd_off + 2 > len(tiff) or depth > 3:
            return 0
        (count,) = struct.unpack(fmt + "H", tiff[ifd_off:ifd_off + 2])
        entries_off = ifd_off + 2
        for i in range(count):
            e = entries_off + i * 12
            if e + 12 > len(tiff):
                return 0
            (tag, typ, num, val) = struct.unpack(fmt + "HHII", tiff[e:e + 12])
            name = TAG_NAMES.get(tag)
            if tag == 0x8825:
                has_gps = True
            if name and name not in ("GPSIFD", "ExifIFD"):
                tv = ""
                if typ == 2:  # ASCII
                    if num <= 4:
                        tv = struct.pack(fmt + "I", val).rstrip(b"\x00").decode("utf-8", "replace")
                    elif val + num <= len(tiff):
                        tv = tiff[val:val + num].rstrip(b"\x00").decode("utf-8", "replace")
                tags.append((name, tv))
            if tag == 0x8769 and val < len(tiff):
                read_ifd(val, depth + 1)
        next_off_pos = entries_off + count * 12
        if next_off_pos + 4 <= len(tiff):
            (next_off,) = struct.unpack(fmt + "I", tiff[next_off_pos:next_off_pos + 4])
            if depth == 0 and next_off:
                has_thumb = True
                read_ifd(next_off, depth + 1)
            return next_off
        return 0

    try:
        read_ifd(ifd0_off)
    except Exception:
        pass
    return tags, has_gps, has_thumb


def _inspect_jpeg(data: bytes, report: InspectReport) -> None:
    if not data.startswith(SOI):
        report.notes.append("JPEG SOI marker missing.")
        return

    for off, marker, body in _iter_segments(data):
        if marker == SOS:
            break

        if marker == 0xFE:  # COM
            txt = body.decode("utf-8", "replace")
            report.findings.append(Finding(
                category=Category.JPEG_COMMENT, severity=Severity.MED,
                location=f"JPEG COM @offset={off}", name="JPEG free-form comment",
                detail=f"{len(body)} bytes", size_bytes=len(body),
                value_preview=txt[:240], source="jpeg.com",
            ))
            continue

        if marker == 0xDB:  # DQT
            h = hashlib.sha1(body).hexdigest()[:12]
            report.findings.append(Finding(
                category=Category.JPEG_QT, severity=Severity.LOW,
                location=f"JPEG DQT @offset={off}", name=f"Quantization table (hash {h})",
                detail="Camera / encoder fingerprint — survives EXIF strip.",
                size_bytes=len(body), source="jpeg.dqt", extra={"sha1_12": h},
            ))
            continue

        if 0xE0 <= marker <= 0xEF:
            seg_name = f"APP{marker - 0xE0}"

            if marker == 0xE1:
                if body.startswith(b"Exif\x00\x00"):
                    tags, has_gps, has_thumb = _parse_exif(body)
                    leaked = [(k, v) for k, v in tags if k in EXIF_LEAK_TAGS]
                    report.findings.append(Finding(
                        category=Category.EXIF,
                        severity=Severity.HIGH if leaked or has_gps else Severity.MED,
                        location=f"JPEG {seg_name} EXIF @offset={off}", name="EXIF metadata block",
                        detail=(f"{len(tags)} known tags"
                                + ("; GPS present" if has_gps else "")
                                + ("; embedded thumbnail" if has_thumb else "")),
                        size_bytes=len(body),
                        value_preview="; ".join(f"{k}={v[:60]}" for k, v in leaked[:6]),
                        source="jpeg.exif",
                    ))
                    if has_gps:
                        report.findings.append(Finding(
                            category=Category.GPS, severity=Severity.CRITICAL,
                            location=f"JPEG {seg_name} EXIF GPS IFD @offset={off}",
                            name="GPS location embedded",
                            detail="EXIF GPS IFD present — reveals capture location.",
                            source="jpeg.gps",
                        ))
                    if has_thumb:
                        report.findings.append(Finding(
                            category=Category.THUMBNAIL, severity=Severity.MED,
                            location=f"JPEG {seg_name} EXIF IFD1 @offset={off}",
                            name="Embedded thumbnail",
                            detail="EXIF IFD1 thumbnail — may show original pre-edit pixels.",
                            source="jpeg.thumb",
                        ))
                    for k, v in tags:
                        if k.startswith(GPS_TAGS_PREFIX):
                            continue
                        if "Serial" in k and v:
                            report.findings.append(Finding(
                                category=Category.SERIAL, severity=Severity.CRITICAL,
                                location=f"JPEG EXIF tag {k}", name=f"Device serial: {k}",
                                detail=v, source="jpeg.serial",
                            ))
                    continue
                if body.startswith(b"http://ns.adobe.com/xap/1.0/\x00"):
                    xmp = body[29:].decode("utf-8", "replace")
                    label, why = _check_xmp(xmp)
                    report.findings.append(Finding(
                        category=Category.AI_TAG if label else Category.XMP,
                        severity=Severity.HIGH if label else Severity.MED,
                        location=f"JPEG {seg_name} XMP @offset={off}",
                        name=label or "XMP packet",
                        detail=why or f"{len(xmp)} chars of XMP metadata",
                        size_bytes=len(body), value_preview=xmp[:240], source="jpeg.xmp",
                    ))
                    continue
                if body.startswith(b"http://ns.adobe.com/xmp/extension/\x00"):
                    report.findings.append(Finding(
                        category=Category.XMP, severity=Severity.MED,
                        location=f"JPEG {seg_name} XMP-extended @offset={off}",
                        name="XMP extension packet",
                        detail=f"{len(body)} bytes (continuation of XMP)",
                        size_bytes=len(body), source="jpeg.xmp_ext",
                    ))
                    continue

            if marker == 0xE2:
                if body.startswith(b"ICC_PROFILE\x00"):
                    report.findings.append(Finding(
                        category=Category.ICC, severity=Severity.LOW,
                        location=f"JPEG {seg_name} ICC @offset={off}",
                        name="ICC color profile segment",
                        detail="Embedded ICC profile — may carry custom strings/UUIDs.",
                        size_bytes=len(body), source="jpeg.icc",
                    ))
                    continue
                if body.startswith(b"MPF\x00"):
                    report.findings.append(Finding(
                        category=Category.THUMBNAIL, severity=Severity.MED,
                        location=f"JPEG {seg_name} MPF @offset={off}",
                        name="Multi-picture (MPF) — embedded extra images",
                        detail="Often contains an additional preview/original frame.",
                        size_bytes=len(body), source="jpeg.mpf",
                    ))
                    continue

            if marker == 0xEB and JUMBF_MAGIC in body[:64]:
                report.findings.append(Finding(
                    category=Category.C2PA, severity=Severity.CRITICAL,
                    location=f"JPEG {seg_name} @offset={off}",
                    name="C2PA / Content Credentials manifest (JUMBF)",
                    detail="Encodes signing entity and edit history.",
                    size_bytes=len(body),
                    value_preview=body[:200].decode("latin1", "replace"), source="jpeg.c2pa",
                ))
                continue

            if marker == 0xED and body.startswith(PHOTOSHOP_HEADER):
                report.findings.append(Finding(
                    category=Category.IPTC, severity=Severity.HIGH,
                    location=f"JPEG {seg_name} Photoshop IRB @offset={off}",
                    name="Photoshop 8BIM / IPTC block",
                    detail="Photoshop resource block — may include original filename, paths, URL, IPTC.",
                    size_bytes=len(body), source="jpeg.psd",
                ))
                continue

            if marker == 0xEE and body.startswith(ADOBE_APP14):
                report.findings.append(Finding(
                    category=Category.JPEG_APP, severity=Severity.LOW,
                    location=f"JPEG {seg_name} Adobe @offset={off}",
                    name="Adobe APP14 marker (DCT transform)",
                    detail="Identifies file as having passed through an Adobe encoder.",
                    size_bytes=len(body), source="jpeg.adobe",
                ))
                continue

            if marker == 0xE0 and body.startswith(b"JFIF\x00"):
                continue
            if marker == 0xE0 and body.startswith(b"JFXX\x00"):
                report.findings.append(Finding(
                    category=Category.THUMBNAIL, severity=Severity.MED,
                    location=f"JPEG APP0 JFXX @offset={off}", name="JFIF extension thumbnail",
                    detail="JFXX block carries an embedded thumbnail.",
                    size_bytes=len(body), source="jpeg.jfxx",
                ))
                continue

            report.findings.append(Finding(
                category=Category.JPEG_APP, severity=Severity.MED,
                location=f"JPEG {seg_name} @offset={off}", name=f"Unknown {seg_name} segment",
                detail=f"{len(body)} bytes of vendor-specific data.",
                size_bytes=len(body),
                value_preview=body[:60].decode("latin1", "replace"), source="jpeg.appn",
            ))

    eoi_idx = data.rfind(EOI)
    if eoi_idx != -1 and eoi_idx + 2 < len(data):
        trailing = len(data) - eoi_idx - 2
        report.findings.append(Finding(
            category=Category.TRAILING, severity=Severity.HIGH,
            location=f"JPEG trailing bytes @offset={eoi_idx + 2}",
            name="Trailing bytes after EOI",
            detail=f"{trailing} extra bytes after JPEG end-of-image marker.",
            size_bytes=trailing,
            value_preview=data[eoi_idx + 2:eoi_idx + 2 + 200].decode("latin1", "replace"),
            source="jpeg.trailing",
        ))


# ══════════════════════════════════════════════════════════════════════════════
# clean/png.py — PNG cleaner
# ══════════════════════════════════════════════════════════════════════════════


def _png_crc(chunk_type: bytes, body: bytes) -> bytes:
    return struct.pack(">I", zlib.crc32(chunk_type + body) & 0xFFFFFFFF)


def _encode_chunk(chunk_type: bytes, body: bytes) -> bytes:
    return struct.pack(">I", len(body)) + chunk_type + body + _png_crc(chunk_type, body)


def _png_strip_chunks(data: bytes) -> bytes:
    """Safe mode: keep only IHDR/PLTE/IDAT/IEND + rendering-relevant ancillaries.
    Pixel data (IDAT) copied byte-for-byte. Trailing bytes dropped."""
    out = io.BytesIO()
    out.write(PNG_HEADER)
    saw_iend = False
    for _off, ctype, body in _iter_chunks(data):
        if ctype in PNG_CRITICAL_CHUNKS or ctype in PNG_SAFE_ANCILLARY:
            out.write(_encode_chunk(ctype, body))
            if ctype == b"IEND":
                saw_iend = True
    if not saw_iend:
        out.write(_encode_chunk(b"IEND", b""))
    return out.getvalue()


def _png_reencode_pixels(data: bytes, noise_sigma: float = 0.0) -> bytes:
    im = Image.open(io.BytesIO(data))
    im.load()
    if im.mode not in ("RGB", "RGBA", "L", "LA"):
        im = im.convert("RGBA" if "A" in im.getbands() else "RGB")
    arr = np.asarray(im, dtype=np.int16)
    if noise_sigma > 0:
        rng = np.random.default_rng()
        noise = rng.normal(0.0, noise_sigma, arr.shape)
        arr = np.clip(arr + noise.round().astype(np.int16), 0, 255)
    arr = arr.astype(np.uint8)
    out_im = Image.fromarray(arr, mode=im.mode)
    buf = io.BytesIO()
    out_im.save(buf, format="PNG", optimize=True)
    return _png_strip_chunks(buf.getvalue())


def _png_nuclear_pixels(data: bytes) -> bytes:
    """JPEG round-trip + slight resize + 2px crop + color shift to disrupt
    frequency-domain watermarks (visible-but-mild loss)."""
    im = Image.open(io.BytesIO(data))
    im.load()
    has_alpha = im.mode in ("RGBA", "LA") or (im.mode == "P" and "transparency" in im.info)
    if not has_alpha:
        if im.mode != "RGB":
            im = im.convert("RGB")
        buf = io.BytesIO()
        im.save(buf, format="JPEG", quality=92, subsampling=2, optimize=True)
        im = Image.open(buf)
        im.load()

    w, h = im.size
    new_w = max(1, int(round(w * 0.997)))
    new_h = max(1, int(round(h * 0.997)))
    im = im.resize((new_w, new_h), Image.LANCZOS)
    if new_w > 4 and new_h > 4:
        im = im.crop((2, 2, new_w - 2, new_h - 2))

    arr = np.asarray(im, dtype=np.int16)
    rng = np.random.default_rng()
    arr = arr + rng.normal(0.0, 0.7, arr.shape).round().astype(np.int16)
    if arr.shape[-1] >= 3:
        bias = rng.integers(-1, 2, size=arr.shape[-1]).astype(np.int16)
        arr = arr + bias
    arr = np.clip(arr, 0, 255).astype(np.uint8)

    final = Image.fromarray(arr, mode=im.mode)
    buf = io.BytesIO()
    final.save(buf, format="PNG", optimize=True)
    return _png_strip_chunks(buf.getvalue())


def _clean_png(data: bytes, mode: str) -> bytes:
    if mode == "safe":
        return _png_strip_chunks(data)
    if mode == "paranoid":
        return _png_reencode_pixels(data, noise_sigma=0.5)
    if mode == "nuclear":
        return _png_nuclear_pixels(data)
    raise ValueError(mode)


# ══════════════════════════════════════════════════════════════════════════════
# clean/jpeg.py — JPEG cleaner
# ══════════════════════════════════════════════════════════════════════════════

_KEEP_APP = {0xE0}  # keep only APP0 (JFIF basic) of the APPn range


def _jpeg_strip_segments(data: bytes) -> bytes:
    """Rewrite JPEG keeping all SOFn/DQT/DHT/SOS + entropy scan, dropping every
    APP1..APP15 and COM segment."""
    out = io.BytesIO()
    out.write(SOI)
    sos_offset = None
    for off, marker, body in _iter_segments(data):
        if marker == 0xDA:  # SOS
            sos_offset = off
            out.write(bytes([0xFF, marker]))
            out.write(struct.pack(">H", len(body) + 2))
            out.write(body)
            break
        if marker == 0xFE:  # COM
            continue
        if 0xE1 <= marker <= 0xEF:  # APP1..APP15
            continue
        if 0xE0 <= marker <= 0xEF and marker not in _KEEP_APP:
            continue
        out.write(bytes([0xFF, marker]))
        if body:
            out.write(struct.pack(">H", len(body) + 2))
            out.write(body)

    if sos_offset is None:
        return data

    scan_start = None
    for off, marker, body in _iter_segments(data):
        if marker == 0xDA:
            scan_start = off + 2 + 2 + len(body)  # 0xFF DA + length(2) + body
            break
    if scan_start is None:
        return data

    eoi = data.rfind(EOI)
    if eoi == -1:
        return data
    out.write(data[scan_start:eoi])
    out.write(EOI)
    return out.getvalue()


def _jpeg_reencode_pixels(data: bytes, quality: int, noise_sigma: float) -> bytes:
    im = Image.open(io.BytesIO(data))
    im.load()
    if im.mode != "RGB":
        im = im.convert("RGB")
    if noise_sigma > 0:
        arr = np.asarray(im, dtype=np.int16)
        rng = np.random.default_rng()
        arr = arr + rng.normal(0.0, noise_sigma, arr.shape).round().astype(np.int16)
        arr = np.clip(arr, 0, 255).astype(np.uint8)
        im = Image.fromarray(arr, mode="RGB")
    buf = io.BytesIO()
    im.save(buf, format="JPEG", quality=quality, subsampling=2, optimize=True)
    return _jpeg_strip_segments(buf.getvalue())


def _jpeg_nuclear_pixels(data: bytes) -> bytes:
    im = Image.open(io.BytesIO(data))
    im.load()
    if im.mode != "RGB":
        im = im.convert("RGB")
    w, h = im.size
    new_w = max(1, int(round(w * 0.997)))
    new_h = max(1, int(round(h * 0.997)))
    im = im.resize((new_w, new_h), Image.LANCZOS)
    if new_w > 4 and new_h > 4:
        im = im.crop((2, 2, new_w - 2, new_h - 2))
    arr = np.asarray(im, dtype=np.int16)
    rng = np.random.default_rng()
    arr = arr + rng.normal(0.0, 0.7, arr.shape).round().astype(np.int16)
    arr = arr + rng.integers(-1, 2, size=(arr.shape[-1],)).astype(np.int16)
    arr = np.clip(arr, 0, 255).astype(np.uint8)
    final = Image.fromarray(arr, mode="RGB")
    buf = io.BytesIO()
    final.save(buf, format="JPEG", quality=88, subsampling=2, optimize=True)
    return _jpeg_strip_segments(buf.getvalue())


def _clean_jpeg(data: bytes, mode: str) -> bytes:
    if mode == "safe":
        return _jpeg_strip_segments(data)
    if mode == "paranoid":
        return _jpeg_reencode_pixels(data, quality=92, noise_sigma=0.5)
    if mode == "nuclear":
        return _jpeg_nuclear_pixels(data)
    raise ValueError(mode)


# ══════════════════════════════════════════════════════════════════════════════
# format dispatch
# ══════════════════════════════════════════════════════════════════════════════


def _detect_format(data: bytes) -> str | None:
    if data.startswith(PNG_HEADER):
        return "png"
    if data.startswith(SOI):
        return "jpeg"
    return None


def _inspect(data: bytes) -> InspectReport:
    fmt = _detect_format(data) or "unknown"
    report = InspectReport(format=fmt, file_size=len(data))
    if fmt == "png":
        _inspect_png(data, report)
    elif fmt == "jpeg":
        _inspect_jpeg(data, report)
    else:
        report.notes.append("Unsupported format — only PNG and JPEG are supported.")
    return report


def _clean(data: bytes, fmt: str, mode: str) -> bytes:
    if fmt == "png":
        return _clean_png(data, mode)
    if fmt == "jpeg":
        return _clean_jpeg(data, mode)
    raise ValueError(f"Unsupported format: {fmt}")


# ══════════════════════════════════════════════════════════════════════════════
# Streamlit UI
# ══════════════════════════════════════════════════════════════════════════════

_MODE_LABELS = {
    "safe": "Safe — strip metadata only (pixels byte-identical)",
    "paranoid": "Paranoid — safe + light noise re-encode (kills quantization-table fingerprints)",
    "nuclear": "Nuclear — paranoid + resize/crop/color-shift (disrupts robust watermarks; visible-but-mild)",
}
_MODE_KEYS = list(_MODE_LABELS.keys())


def _human_size(n: int) -> str:
    if n < 1024:
        return f"{n} B"
    if n < 1024 * 1024:
        return f"{n / 1024:.1f} KB"
    return f"{n / (1024 * 1024):.2f} MB"


def _report_dataframe(report: InspectReport) -> pd.DataFrame:
    rows = []
    for f in sorted(report.findings, key=lambda x: -_SEV_RANK[x.severity]):
        rows.append({
            "Severity": f"{_SEV_ICON[f.severity]} {f.severity.value}",
            "Category": _CAT_LABELS.get(f.category, f.category.value),
            "Finding": f.name,
            "Detail": f.detail,
            "Size": _human_size(f.size_bytes) if f.size_bytes else "",
            "Preview": (f.value_preview[:120] + "…") if len(f.value_preview) > 120 else f.value_preview,
        })
    return pd.DataFrame(rows)


def render():
    st.header("🧹 Remove Image Fingerprints & Watermarks")
    st.caption(
        "Scans uploaded **JPEG / PNG** images for identifying metadata — EXIF (camera "
        "Make/Model, serial, GPS), C2PA / Content Credentials, AI-generation prompts "
        "(Stable Diffusion, ComfyUI, Midjourney, DALL·E, Firefly), Photoshop/IPTC blocks, "
        "ICC profiles, embedded thumbnails and trailing payloads — then strips them. "
        "Everything runs locally in this app; no image is uploaded anywhere. "
        "Ported from the open-source [image-fingerprint-remover](https://github.com/lhfer/image-fingerprint-remover)."
    )
    st.info(
        "**Limits:** robust/frequency-domain watermarks (Google SynthID, Digimarc, IMATAG) "
        "are trained to survive edits — only *nuclear* mode attempts them, and success isn't "
        "guaranteed. C2PA server-side hashes and PRNU sensor noise can't be removed locally.",
        icon="⚠️",
    )

    files = st.file_uploader(
        "Upload image(s)",
        type=["jpg", "jpeg", "png"],
        accept_multiple_files=True,
        help="JPEG and PNG only. Multiple files supported.",
    )

    mode = st.radio(
        "Cleaning mode",
        _MODE_KEYS,
        format_func=lambda k: _MODE_LABELS[k],
        index=0,
    )
    if mode in ("paranoid", "nuclear"):
        st.caption(
            "⚠️ This mode **re-encodes pixels** — the output is no longer bit-identical to "
            "the input. Keep your original if you need it."
        )

    if not files:
        return

    cleaned_files: list[tuple[str, bytes]] = []  # (filename, bytes) for bulk ZIP

    for uf in files:
        data = uf.getvalue()
        fmt = _detect_format(data)
        with st.expander(f"📄 {uf.name}  ·  {_human_size(len(data))}", expanded=len(files) == 1):
            if fmt is None:
                st.error("Unsupported or corrupt file — only PNG and JPEG are supported.")
                continue

            report = _inspect(data)

            if report.is_clean and not report.findings:
                st.success(f"✅ No fingerprints found — **{uf.name}** is already clean.")
            elif report.is_clean:
                st.success(
                    f"✅ No medium/high/critical findings in **{uf.name}**. "
                    "Only residual low-level fingerprints remain (see table)."
                )
            else:
                crit = sum(1 for f in report.findings if f.severity == Severity.CRITICAL)
                high = sum(1 for f in report.findings if f.severity == Severity.HIGH)
                msg = f"Found **{len(report.findings)}** finding(s)"
                if crit or high:
                    msg += f" — {crit} critical, {high} high"
                st.warning(msg + ".")

            if report.findings:
                st.dataframe(_report_dataframe(report), use_container_width=True, hide_index=True)
            for note in report.notes:
                st.caption(note)

            try:
                cleaned = _clean(data, fmt, mode)
            except Exception as e:
                st.error(f"Cleaning failed: {e}")
                continue

            saved = len(data) - len(cleaned)
            same_pixels = ""
            if mode == "safe":
                same_pixels = " · pixels byte-identical (SHA256-verifiable)"
            st.markdown(
                f"**Cleaned:** {_human_size(len(cleaned))} "
                f"({'−' if saved >= 0 else '+'}{_human_size(abs(saved))}{same_pixels})"
            )

            ext = "png" if fmt == "png" else "jpg"
            base = uf.name.rsplit(".", 1)[0]
            out_name = f"{base}_cleaned.{ext}"
            cleaned_files.append((out_name, cleaned))
            st.download_button(
                f"⬇️ Download cleaned {uf.name}",
                data=cleaned,
                file_name=out_name,
                mime=f"image/{ 'png' if fmt == 'png' else 'jpeg' }",
                key=f"dl_{uf.name}_{mode}",
            )

    # Bulk download — all cleaned images in one ZIP.
    if len(cleaned_files) > 1:
        zip_buf = io.BytesIO()
        with zipfile.ZipFile(zip_buf, "w", zipfile.ZIP_STORED) as zf:
            used: dict[str, int] = {}
            for name, blob in cleaned_files:
                # Guard against duplicate names collapsing in the archive.
                if name in used:
                    used[name] += 1
                    stem, _, e = name.rpartition(".")
                    name = f"{stem}_{used[name]}.{e}"
                else:
                    used[name] = 0
                zf.writestr(name, blob)
        st.divider()
        st.download_button(
            f"📦 Download all {len(cleaned_files)} cleaned images (ZIP)",
            data=zip_buf.getvalue(),
            file_name=f"cleaned_images_{mode}.zip",
            mime="application/zip",
            type="primary",
            key=f"dl_all_{mode}",
        )
