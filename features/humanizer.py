import re
from dataclasses import dataclass

import streamlit as st


@dataclass(frozen=True)
class PatternRule:
    name: str
    pattern: str
    issue: str


RULES = [
    PatternRule("Inflated significance", r"\b(serves as|stands as|testament|pivotal|crucial|underscores?|highlights?|broader|evolving landscape|lasting impact|indelible mark)\b", "Adds fake importance or broad claims."),
    PatternRule("Promotional language", r"\b(boasts?|vibrant|rich cultural|profound|showcas(?:e|es|ing)|renowned|breathtaking|must-visit|stunning|groundbreaking)\b", "Sounds like marketing copy."),
    PatternRule("Vague attribution", r"\b(industry reports|observers have cited|experts argue|some critics argue|several sources|many believe)\b", "Uses vague sources instead of specific evidence."),
    PatternRule("AI vocabulary", r"\b(additionally|delve|fostering|garner|interplay|intricate|intricacies|key role|tapestry|valuable insights|align with|enhance)\b", "Common AI phrasing."),
    PatternRule("Copula avoidance", r"\b(serves as|stands as|represents a|marks a|boasts|features a|offers a)\b", "Uses inflated verbs instead of simple wording."),
    PatternRule("Negative parallelism", r"\b(not only\b.*?\bbut also\b|not just\b.*?\bit'?s\b|not merely\b.*?\bit'?s\b)", "Formulaic contrast structure."),
    PatternRule("Filler", r"\b(in order to|due to the fact that|at this point in time|in the event that|has the ability to|it is important to note that)\b", "Unneeded filler."),
    PatternRule("Hedging", r"\b(could potentially|possibly be argued|might perhaps|it appears that|it seems that)\b", "Over-qualifies the point."),
    PatternRule("Chatbot artifacts", r"\b(i hope this helps|of course|certainly|great question|you'?re absolutely right|let me know if|here is an overview)\b", "Looks pasted from a chatbot response."),
    PatternRule("Knowledge disclaimer", r"\b(as of my last|up to my last|training update|based on available information|specific details are limited)\b", "AI-style knowledge disclaimer."),
]


REPLACEMENTS = [
    (r"\bAdditionally,\s*", "Also, "),
    (r"\bAdditionally\b", "Also"),
    (r"\bin order to\b", "to"),
    (r"\bDue to the fact that\b", "Because"),
    (r"\bdue to the fact that\b", "because"),
    (r"\bat this point in time\b", "now"),
    (r"\bin the event that\b", "if"),
    (r"\bhas the ability to\b", "can"),
    (r"\bIt is important to note that\s*", ""),
    (r"\bit is important to note that\s*", ""),
    (r"\bserves as\b", "is"),
    (r"\bstands as\b", "is"),
    (r"\bboasts\b", "has"),
    (r"\bfeatures a\b", "has a"),
    (r"\boffers a\b", "has a"),
    (r"\bshowcases\b", "shows"),
    (r"\bshowcasing\b", "showing"),
    (r"\bhighlights\b", "shows"),
    (r"\bunderscores\b", "shows"),
    (r"\bcrucial\b", "important"),
    (r"\bpivotal\b", "important"),
    (r"\bvibrant\b", "active"),
    (r"\bdelve into\b", "look at"),
    (r"\bI hope this helps!?\b", ""),
    (r"\bLet me know if you'd like[^.]*\.", ""),
    (r"\bOf course!?\s*", ""),
    (r"\bCertainly!?\s*", ""),
    (r"\bGreat question!?\s*", ""),
    (r"\*\*([^*]+)\*\*", r"\1"),
    (r"[–—]", ", "),
    (r"[“”]", '"'),
    (r"[‘’]", "'"),
]


def render():
    st.header("Humanizer")
    st.caption("Detects common AI-writing patterns and rewrites text into a cleaner, more natural draft.")

    text = st.text_area(
        "Paste text to humanize",
        height=280,
        placeholder="Paste AI-sounding text here...",
    )

    col1, col2 = st.columns([1, 1])
    with col1:
        mode = st.selectbox("Rewrite strength", ["Light cleanup", "Stronger cleanup"])
    with col2:
        show_findings = st.checkbox("Show detected patterns", value=True)

    if not st.button("Humanize", type="primary"):
        return

    if not text.strip():
        st.error("Please paste text first.")
        return

    findings = _find_patterns(text)
    rewritten = _humanize(text, aggressive=mode == "Stronger cleanup")

    st.subheader("Humanized text")
    st.text_area("Result", value=rewritten, height=300)
    st.download_button(
        "Download .txt",
        data=rewritten.encode("utf-8"),
        file_name="humanized_text.txt",
        mime="text/plain",
    )

    if show_findings:
        st.subheader("Detected patterns")
        if not findings:
            st.success("No obvious AI-writing patterns found.")
        else:
            st.dataframe(findings, use_container_width=True)


def _find_patterns(text: str) -> list[dict[str, str | int]]:
    rows = []
    for rule in RULES:
        matches = re.findall(rule.pattern, text, flags=re.IGNORECASE | re.DOTALL)
        if matches:
            rows.append({
                "Pattern": rule.name,
                "Count": len(matches),
                "Why it matters": rule.issue,
            })
    em_dash_count = text.count("—") + text.count("–")
    if em_dash_count:
        rows.append({
            "Pattern": "Dash overuse",
            "Count": em_dash_count,
            "Why it matters": "Frequent long dashes can make text feel AI-written.",
        })
    return rows


def _humanize(text: str, aggressive: bool) -> str:
    out = text.strip()
    for pattern, replacement in REPLACEMENTS:
        out = re.sub(pattern, replacement, out, flags=re.IGNORECASE)

    out = _remove_formulaic_sentences(out)
    if aggressive:
        out = _simplify_aggressive(out)

    out = re.sub(r"[ \t]+", " ", out)
    out = re.sub(r"\s+\n", "\n", out)
    out = re.sub(r"\n{3,}", "\n\n", out)
    out = re.sub(r"\s+([,.!?;:])", r"\1", out)
    return out.strip()


def _remove_formulaic_sentences(text: str) -> str:
    sentences = re.split(r"(?<=[.!?])\s+", text)
    blocked = [
        r"^This (?:serves|stands) as",
        r"^The future looks bright",
        r"^Exciting times lie ahead",
        r"^This represents a major step",
        r"^In conclusion,",
    ]
    kept = []
    for sentence in sentences:
        if any(re.search(p, sentence, flags=re.IGNORECASE) for p in blocked):
            continue
        kept.append(sentence)
    return " ".join(kept)


def _simplify_aggressive(text: str) -> str:
    out = re.sub(r"\bnot only ([^.;]+?) but also ([^.;]+)", r"\1 and \2", text, flags=re.IGNORECASE)
    out = re.sub(r"\bIt's not just about ([^.;]+?);?\s*it's about ([^.;]+)", r"It is about \1 and \2", out, flags=re.IGNORECASE)
    out = re.sub(r"\b(could potentially|might possibly|may potentially)\b", "may", out, flags=re.IGNORECASE)
    out = re.sub(r"\b(in today'?s (?:fast-paced|ever-changing|digital) world),?\s*", "", out, flags=re.IGNORECASE)
    out = re.sub(r"\b(seamless, intuitive, and powerful|innovation, inspiration, and industry insights)\b", "", out, flags=re.IGNORECASE)
    return out
