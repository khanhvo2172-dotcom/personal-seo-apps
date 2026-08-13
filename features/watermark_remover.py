"""Remove AI "watermarks" from a single Google Docs tab.

The cleaning logic (Layer A: invisible/format Unicode + space homoglyphs) is
ported from the open-source project guillaumemeyer/watermarks-remover
(skills/remove-ai-marks/scripts/text_unicode.py). We keep the code-point tables
verbatim and apply the same deterministic scrub, then write the changes back
in place to the chosen tab via the Google Docs API (batchUpdate).

Layer B (statistical, token-sampling watermarks removed by rewriting the prose)
is intentionally NOT included here — it degrades the original writing and needs
an LLM. This feature is purely the safe, lossless Layer A cleanup.
"""

import unicodedata
from urllib.parse import urlparse, parse_qs

import pandas as pd
import streamlit as st

from features.auth import get_credentials, require_auth

# ── Layer A tables (verbatim from watermarks-remover / text_unicode.py) ──────────

# Format / invisible controls commonly used for steganography or broken pastes.
STRIP_CODEPOINTS: frozenset[int] = frozenset(
    {
        0x00AD,  # soft hyphen
        0x034F,  # combining grapheme joiner
        0x061C,  # Arabic letter mark
        0x115F,  # Hangul choseong filler
        0x1160,  # Hangul jungseong filler
        0x17B4,  # Khmer vowel inherent AQ
        0x17B5,  # Khmer vowel inherent AA
        0x180B,  # Mongolian free variation selector-1
        0x180C,
        0x180D,
        0x180E,  # Mongolian vowel separator
        0x200B,  # zero width space
        0x200C,  # zero width non-joiner
        0x200D,  # zero width joiner
        0x200E,  # LRM
        0x200F,  # RLM
        0x202A,  # LRE
        0x202B,  # RLE
        0x202C,  # PDF
        0x202D,  # LRO
        0x202E,  # RLO
        0x2060,  # word joiner
        0x2061,  # function application
        0x2062,  # invisible times
        0x2063,  # invisible separator
        0x2064,  # invisible plus
        0x2066,  # LRI
        0x2067,  # RLI
        0x2068,  # FSI
        0x2069,  # PDI
        0x206A,  # inhibit symmetric swapping
        0x206B,
        0x206C,
        0x206D,
        0x206E,
        0x206F,
        0xFEFF,  # BOM / ZWNBSP
        0xFE00,  # variation selectors
        0xFE01,
        0xFE02,
        0xFE03,
        0xFE04,
        0xFE05,
        0xFE06,
        0xFE07,
        0xFE08,
        0xFE09,
        0xFE0A,
        0xFE0B,
        0xFE0C,
        0xFE0D,
        0xFE0E,
        0xFE0F,
        0xFFF9,  # interlinear annotation
        0xFFFA,
        0xFFFB,
    }
)

# Spaces that look like (or substitute for) U+0020.
SPACE_HOMOGLYPHS: dict[int, str] = {
    0x00A0: " ",  # no-break space
    0x1680: " ",  # Ogham space mark
    0x2000: " ",  # en quad
    0x2001: " ",  # em quad
    0x2002: " ",  # en space
    0x2003: " ",  # em space
    0x2004: " ",  # three-per-em space
    0x2005: " ",  # four-per-em space
    0x2006: " ",  # six-per-em space
    0x2007: " ",  # figure space
    0x2008: " ",  # punctuation space
    0x2009: " ",  # thin space
    0x200A: " ",  # hair space
    0x202F: " ",  # narrow no-break space
    0x205F: " ",  # medium mathematical space
    0x3000: " ",  # ideographic space
}

# Optional confusable Latin lookalikes (aggressive mode only).
LATIN_CONFUSABLES: dict[int, str] = {
    0x0410: "A",  # Cyrillic
    0x0412: "B",
    0x0415: "E",
    0x041A: "K",
    0x041C: "M",
    0x041D: "H",
    0x041E: "O",
    0x0420: "P",
    0x0421: "C",
    0x0422: "T",
    0x0425: "X",
    0x0430: "a",
    0x0435: "e",
    0x043E: "o",
    0x0440: "p",
    0x0441: "c",
    0x0443: "y",
    0x0445: "x",
    0x0456: "i",
    0xFF21: "A",  # fullwidth
    0xFF22: "B",
    0xFF23: "C",
    0xFF24: "D",
    0xFF25: "E",
    0xFF26: "F",
    0xFF27: "G",
    0xFF28: "H",
    0xFF29: "I",
    0xFF2A: "J",
    0xFF2B: "K",
    0xFF2C: "L",
    0xFF2D: "M",
    0xFF2E: "N",
    0xFF2F: "O",
    0xFF30: "P",
    0xFF31: "Q",
    0xFF32: "R",
    0xFF33: "S",
    0xFF34: "T",
    0xFF35: "U",
    0xFF36: "V",
    0xFF37: "W",
    0xFF38: "X",
    0xFF39: "Y",
    0xFF3A: "Z",
    0xFF41: "a",
    0xFF42: "b",
    0xFF43: "c",
    0xFF44: "d",
    0xFF45: "e",
    0xFF46: "f",
    0xFF47: "g",
    0xFF48: "h",
    0xFF49: "i",
    0xFF4A: "j",
    0xFF4B: "k",
    0xFF4C: "l",
    0xFF4D: "m",
    0xFF4E: "n",
    0xFF4F: "o",
    0xFF50: "p",
    0xFF51: "q",
    0xFF52: "r",
    0xFF53: "s",
    0xFF54: "t",
    0xFF55: "u",
    0xFF56: "v",
    0xFF57: "w",
    0xFF58: "x",
    0xFF59: "y",
    0xFF5A: "z",
}

# Variation selectors beyond FE0x (VS17-VS256 in Supplementary Special-purpose)
_VS_SUPPLEMENT = range(0xE0100, 0xE01F0)

_BIDI_CPS: frozenset[int] = frozenset(
    {0x061C, 0x200E, 0x200F, 0x202A, 0x202B, 0x202C, 0x202D, 0x202E, 0x2066, 0x2067, 0x2068, 0x2069}
)
_ZW_FAMILY: frozenset[int] = frozenset({0x200B, 0x200C, 0x200D, 0x2060, 0xFEFF, 0x180E})


def _is_strip_cp(cp: int) -> bool:
    if cp in STRIP_CODEPOINTS:
        return True
    if cp in _VS_SUPPLEMENT:
        return True
    # Tag characters used in some stego schemes (U+E0001-U+E007F)
    if 0xE0001 <= cp <= 0xE007F:
        return True
    return False


def _strip_kind(cp: int) -> str:
    if 0xE0001 <= cp <= 0xE007F:
        return "tag_chars"
    if cp in _VS_SUPPLEMENT or 0xFE00 <= cp <= 0xFE0F or 0x180B <= cp <= 0x180D:
        return "variation_selector"
    if cp in _BIDI_CPS:
        return "bidi"
    if cp in _ZW_FAMILY:
        return "zwj_family"
    return "strip"


# Friendly names for the report grouping.
_KIND_LABELS = {
    "zwj_family": "Zero-width / joiner",
    "bidi": "Bidirectional control",
    "tag_chars": "Unicode tag character",
    "variation_selector": "Variation selector",
    "strip": "Other invisible/format",
    "other_cf": "Other format (Cf)",
    "space": "Look-alike space",
    "confusable": "Look-alike letter",
}


def _char_label(ch: str) -> str:
    cp = ord(ch)
    name = unicodedata.name(ch, "UNKNOWN")
    cat = unicodedata.category(ch)
    return f"U+{cp:04X} {name} ({cat})"


def _classify(ch: str, *, normalize_spaces: bool, aggressive: bool):
    """Return (kind, replacement) for a suspicious char, else None.

    replacement is None for pure removals, or the ASCII string to substitute.
    """
    cp = ord(ch)
    if _is_strip_cp(cp):
        return _strip_kind(cp), None
    if normalize_spaces and cp in SPACE_HOMOGLYPHS:
        return "space", SPACE_HOMOGLYPHS[cp]
    if aggressive and cp in LATIN_CONFUSABLES:
        return "confusable", LATIN_CONFUSABLES[cp]
    # Other format chars (Cf) not already covered — strip for hygiene, matching
    # watermarks-remover's clean_text default.
    if unicodedata.category(ch) == "Cf" and cp not in SPACE_HOMOGLYPHS:
        return "other_cf", None
    return None


def _utf16_len(ch: str) -> int:
    """Google Docs indices are in UTF-16 code units; supplementary chars take 2."""
    return 2 if ord(ch) > 0xFFFF else 1


# ── document walk ────────────────────────────────────────────────────────────────

def _walk_text_runs(elements, out):
    """Yield every textRun element (with its startIndex) in document order.

    Recurses into tables and the table of contents — the same reach the
    Format-Google-Docs feature edits.
    """
    for el in elements or []:
        if "paragraph" in el:
            for pe in el["paragraph"].get("elements", []) or []:
                tr = pe.get("textRun")
                if tr and pe.get("startIndex") is not None:
                    out.append((pe["startIndex"], tr.get("content", "") or ""))
        elif "table" in el:
            for row in el["table"].get("tableRows", []) or []:
                for cell in row.get("tableCells", []) or []:
                    _walk_text_runs(cell.get("content", []), out)
        elif "tableOfContents" in el:
            _walk_text_runs(el["tableOfContents"].get("content", []), out)


def _scan(body_content, tab_id, *, normalize_spaces, aggressive):
    """Return (edits, stats).

    edits: list of dicts {start, end, insert} in absolute UTF-16 indices.
           insert is "" for removals, or the replacement string.
    stats: dict with per-kind counts and per-character label counts.
    """
    runs = []
    _walk_text_runs(body_content, runs)

    edits = []
    kind_counts: dict[str, int] = {}
    char_rows: dict[str, dict] = {}

    for run_start, content in runs:
        offset = 0  # UTF-16 units into this run
        for ch in content:
            w = _utf16_len(ch)
            hit = _classify(ch, normalize_spaces=normalize_spaces, aggressive=aggressive)
            if hit is not None:
                kind, repl = hit
                abs_start = run_start + offset
                edits.append({"start": abs_start, "end": abs_start + w, "insert": repl or ""})
                kind_counts[kind] = kind_counts.get(kind, 0) + 1
                label = _char_label(ch)
                row = char_rows.setdefault(
                    label, {"label": label, "kind": kind, "count": 0, "action": "→ space" if repl == " " else ("→ '" + repl + "'" if repl else "removed")}
                )
                row["count"] += 1
            offset += w

    stats = {
        "total": len(edits),
        "kind_counts": kind_counts,
        "char_rows": sorted(char_rows.values(), key=lambda r: -r["count"]),
    }
    return edits, stats


def _apply_edits(docs_service, doc_id, tab_id, edits):
    """Apply edits via batchUpdate. Process in descending start order so earlier
    (higher-index) edits never shift the indices of later (lower-index) ones."""
    if not edits:
        return
    requests = []
    for e in sorted(edits, key=lambda x: -x["start"]):
        rng = {"startIndex": e["start"], "endIndex": e["end"]}
        loc = {"index": e["start"]}
        if tab_id:
            rng["tabId"] = tab_id
            loc["tabId"] = tab_id
        # Delete the offending character(s) first...
        requests.append({"deleteContentRange": {"range": rng}})
        # ...then, for replacements, insert the ASCII substitute at the same spot.
        if e["insert"]:
            requests.append({"insertText": {"location": loc, "text": e["insert"]}})
    docs_service.documents().batchUpdate(documentId=doc_id, body={"requests": requests}).execute()


# ── tab / URL helpers (aligned with format_gdocs.py) ─────────────────────────────

def _extract_doc_id(url: str) -> str | None:
    import re
    m = re.search(r"/document/d/([a-zA-Z0-9_-]+)", url)
    return m.group(1) if m else None


def _extract_tab_id(url: str) -> str | None:
    parsed = urlparse(url)
    q = parse_qs(parsed.query)
    if "tab" in q:
        return q["tab"][0]
    frag_q = parse_qs(parsed.fragment.replace("?", "&"))
    if "tab" in frag_q:
        return frag_q["tab"][0]
    return None


def _find_tab(tabs, tab_id):
    for tab in tabs or []:
        if (tab.get("tabProperties", {}) or {}).get("tabId") == tab_id:
            return tab
        child = _find_tab(tab.get("childTabs", []), tab_id)
        if child:
            return child
    return None


def _resolve_tab(doc, url):
    """Return (body_content, tab_id, tab_name). tab_id is None for legacy docs."""
    tabs = doc.get("tabs", [])
    if not tabs:
        return doc.get("body", {}).get("content", []), None, None
    wanted = _extract_tab_id(url)
    target = _find_tab(tabs, wanted) if wanted else None
    if target is None:
        target = tabs[0]
    props = target.get("tabProperties", {}) or {}
    doc_tab = target.get("documentTab", {})
    return (
        doc_tab.get("body", {}).get("content", []),
        props.get("tabId"),
        props.get("title"),
    )


# ── scan-and-clean orchestration ─────────────────────────────────────────────────

def _fetch_doc(creds, doc_id):
    from googleapiclient.discovery import build
    docs_service = build("docs", "v1", credentials=creds)
    doc = docs_service.documents().get(documentId=doc_id, includeTabsContent=True).execute()
    return docs_service, doc


def render():
    st.header("🧹 Remove AI Watermarks from Google Doc")
    st.caption(
        "Strips invisible AI/steganographic marks — zero-width spaces & joiners, "
        "bidirectional controls, the byte-order mark, variation selectors and Unicode "
        "tag characters — and normalizes look-alike spaces back to a normal space, in "
        "the tab you point at. Lossless: visible wording is never changed."
    )

    if not require_auth():
        return

    with st.form("watermark_remover_form"):
        url = st.text_input(
            "Google Docs URL",
            placeholder="https://docs.google.com/document/d/.../edit?tab=t.0",
            help="Only the tab in the URL is cleaned. If no ?tab= is present, the first tab is used.",
        )
        normalize_spaces = st.checkbox(
            "Normalize look-alike spaces to a normal space",
            value=True,
            help="Converts non-breaking / thin / em / narrow spaces (etc.) to a plain space (U+0020). "
            "Safe — nothing visible changes. Recommended: leave on.",
        )
        aggressive = st.checkbox(
            "Map look-alike letters to ASCII (Cyrillic А→A, fullwidth Ａ→A …)",
            value=True,
            help="Fixes disguised letters that look like English but aren't. On by default for "
            "English-only content. Turn OFF if this doc genuinely contains Russian / Bulgarian / "
            "CJK / fullwidth text, or it would convert that real text to English letters.",
        )
        scan = st.form_submit_button("🔍 Scan document", type="primary")

    if scan:
        st.session_state.pop("wm_scan", None)
        if not url.strip():
            st.error("Please enter a Google Docs URL.")
            return
        doc_id = _extract_doc_id(url)
        if not doc_id:
            st.error("That doesn't look like a Google Docs URL (no /document/d/… id found).")
            return
        try:
            _, doc = _fetch_doc(get_credentials(), doc_id)
        except Exception as e:
            st.error(f"Couldn't open the document: {e}")
            return
        body_content, tab_id, tab_name = _resolve_tab(doc, url)
        edits, stats = _scan(
            body_content, tab_id, normalize_spaces=normalize_spaces, aggressive=aggressive
        )
        st.session_state["wm_scan"] = {
            "doc_id": doc_id,
            "url": url,
            "tab_id": tab_id,
            "tab_name": tab_name,
            "title": doc.get("title", "Untitled"),
            "normalize_spaces": normalize_spaces,
            "aggressive": aggressive,
            "stats": stats,
            "count": stats["total"],
        }

    scan_state = st.session_state.get("wm_scan")
    if not scan_state:
        return

    _render_report(scan_state)


def _render_report(s):
    where = s["title"] + (f" — tab “{s['tab_name']}”" if s.get("tab_name") else "")
    stats = s["stats"]

    if s["count"] == 0:
        st.success(f"✅ No watermarks found in {where}. The document is already clean.")
        return

    st.warning(f"Found **{s['count']}** watermark character(s) in {where}.")

    # Category summary
    kc = stats["kind_counts"]
    summary_rows = [
        {"Category": _KIND_LABELS.get(k, k), "Count": v}
        for k, v in sorted(kc.items(), key=lambda x: -x[1])
    ]
    st.markdown("**By category**")
    st.dataframe(pd.DataFrame(summary_rows), use_container_width=True, hide_index=True)

    # Per-character detail
    detail_rows = [
        {"Character": r["label"], "Category": _KIND_LABELS.get(r["kind"], r["kind"]), "Action": r["action"], "Count": r["count"]}
        for r in stats["char_rows"]
    ]
    st.markdown("**By character**")
    st.dataframe(pd.DataFrame(detail_rows), use_container_width=True, hide_index=True)

    st.caption(
        "Scope: body text, tables and table-of-contents of the selected tab. "
        "Edits are applied via the Google Docs API — you can always undo from the "
        "document's version history (File ▸ Version history)."
    )

    if st.button(f"🧹 Remove {s['count']} watermark(s)", type="primary"):
        _do_clean(s)


def _do_clean(s):
    from googleapiclient.errors import HttpError

    try:
        docs_service, doc = _fetch_doc(get_credentials(), s["doc_id"])
    except Exception as e:
        st.error(f"Couldn't re-open the document to apply changes: {e}")
        return

    # Re-scan the freshly fetched doc so indices are guaranteed current.
    body_content, tab_id, _ = _resolve_tab(doc, s["url"])
    edits, stats = _scan(
        body_content,
        tab_id,
        normalize_spaces=s["normalize_spaces"],
        aggressive=s["aggressive"],
    )

    if not edits:
        st.info("Nothing left to clean — the document is already watermark-free.")
        st.session_state.pop("wm_scan", None)
        return

    try:
        _apply_edits(docs_service, s["doc_id"], tab_id, edits)
    except HttpError as e:
        if getattr(e, "resp", None) is not None and e.resp.status == 403:
            st.error(
                "Permission denied (403). The signed-in Google account needs **edit** access "
                "to this document. Open it in Settings, re-authenticate if needed, and make "
                "sure your token includes Docs write access."
            )
        else:
            st.error(f"Google Docs API error: {e}")
        return
    except Exception as e:
        st.error(f"Failed to apply changes: {e}")
        return

    st.success(f"✅ Removed {len(edits)} watermark character(s) from the document.")
    st.balloons()
    st.session_state.pop("wm_scan", None)
