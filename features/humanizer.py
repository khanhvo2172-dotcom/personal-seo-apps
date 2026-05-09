import re
import os
import json
from dataclasses import dataclass

import requests
import streamlit as st
import pandas as pd


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
    PatternRule("Chatbot artifacts", r"\b(i hope this help|of course|certainly|great question|you're absolutely right|let me know if|here is an overview)\b", "Looks pasted from a chatbot response."),
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
    (r"[--]", ", "),
    (r'["]', '"'),
    (r"[']", "'"),
]


def render():
    st.header("Humanizer")
    st.caption("Detects AI-writing patterns sentence by sentence and suggests cleaner rewrites.")

    text = st.text_area(
        "Paste text to analyze",
        height=280,
        placeholder="Paste AI-sounding text here...",
    )

    if not st.button("Analyze", type="primary"):
        return

    if not text.strip():
        st.error("Please paste text first.")
        return

    sentences = _split_sentences(text)
    flagged = _flag_sentences(sentences)

    if not flagged:
        st.success("No obvious AI-writing patterns found.")
        return

    with st.spinner("Generating suggestions..."):
        results = _get_suggestions(flagged)

    st.subheader("Analysis Results")
    df = pd.DataFrame([
        {
            "Current Sentence": r["sentence"],
            "Suggestions": r["suggestion"],
            "Issue": r["issue"],
        }
        for r in results
    ])
    st.dataframe(
        df,
        use_container_width=True,
        hide_index=True,
        column_config={
            "Current Sentence": st.column_config.TextColumn(width="large"),
            "Suggestions": st.column_config.TextColumn(width="large"),
            "Issue": st.column_config.TextColumn(width="medium"),
        },
    )


def _split_sentences(text: str) -> list[str]:
    return [s.strip() for s in re.split(r"(?<=[.!?])\s+", text) if s.strip()]


def _flag_sentences(sentences: list[str]) -> list[dict]:
    flagged = []
    seen: set[str] = set()
    for sentence in sentences:
        matched_issue = None
        for rule in RULES:
            if re.search(rule.pattern, sentence, flags=re.IGNORECASE | re.DOTALL):
                matched_issue = rule.issue
                break
        if matched_issue is None:
            em_dashes = sentence.count("—") + sentence.count("–")
            if em_dashes:
                matched_issue = "Frequent long dashes can make text feel AI-written."
        if matched_issue and sentence not in seen:
            seen.add(sentence)
            flagged.append({"sentence": sentence, "issue": matched_issue, "suggestion": ""})
    return flagged


def _get_suggestions(flagged: list[dict]) -> list[dict]:
    api_key = _private_value("DEEPSEEK_API_KEY")
    if api_key:
        result = _suggest_with_deepseek(flagged, api_key)
        if result:
            return result
    for item in flagged:
        item["suggestion"] = _apply_replacements(item["sentence"])
    return flagged


def _apply_replacements(sentence: str) -> str:
    out = sentence
    for pattern, replacement in REPLACEMENTS:
        out = re.sub(pattern, replacement, out, flags=re.IGNORECASE)
    out = re.sub(r"[ \t]+", " ", out)
    out = re.sub(r"\s+([,.!?;:])", r"\1", out)
    return out.strip()


def _suggest_with_deepseek(flagged: list[dict], api_key: str) -> list[dict] | None:
    sentences_block = "\n".join(
        f"{i + 1}. {item['sentence']}" for i, item in enumerate(flagged)
    )

    system_prompt = """You are an expert human writing editor. Your job is to rewrite AI-sounding sentences so they read as natural, specific, and genuinely human-written.

Follow this process for each sentence:
1. Identify which AI-writing patterns are present.
2. Rewrite to fix them while preserving the original meaning, facts, and intent.
3. Inject specificity and personality — avoid bland, generic phrasing.
4. Apply a final anti-AI check before returning.

Remove ALL of the following patterns:

CONTENT PATTERNS:
- Inflated significance: "pivotal moment", "testament to", "indelible mark", "lasting impact"
- Undue notability emphasis: "renowned", "groundbreaking", "breathtaking"
- Superficial -ing analyses: "showcasing", "highlighting", "underscoring" used as filler
- Promotional phrasing: "vibrant", "rich cultural", "must-visit", "stunning"
- Vague attribution: "industry reports", "experts argue", "many believe", "several sources"
- Formulaic challenge sections that state problems without specifics

LANGUAGE PATTERNS:
- Overused AI vocabulary: "additionally", "crucial", "delve", "foster", "garner", "enhance", "align with", "key role", "tapestry", "valuable insights", "intricate"
- Copula avoidance: replace "serves as", "stands as", "represents a", "marks a" with plain "is"
- Negative parallelisms: "not only...but also", "not just...it's also"
- Rule-of-three forcing: avoid padding lists to exactly three items
- Passive voice when active is clearer
- Hedging: "could potentially", "might perhaps", "it appears that", "it seems that"

STYLE ISSUES:
- Em dash overuse (— or –): replace with comma or rewrite
- Excessive boldface or emoji in prose
- Curly/smart quotes: replace with straight quotes
- Filler transitions: "in order to", "due to the fact that", "it is important to note that"

COMMUNICATION ARTIFACTS:
- Chatbot pleasantries: "Of course!", "Certainly!", "Great question!", "I hope this helps"
- Knowledge-cutoff disclaimers: "as of my last update", "based on available information"
- Sycophantic openers or closers
- Generic conclusions that restate without adding value

WHAT GOOD REWRITING LOOKS LIKE:
- Use plain verbs: "is", "has", "shows", "uses" instead of inflated alternatives
- Be specific: replace vague claims with concrete details where possible
- Vary sentence structure — not every sentence needs the same rhythm
- Add "soul": a clear point of view, an opinion, or a specific observation beats a safe generality
- First-person is fine where it fits naturally

Return ONLY a JSON array. Each element: {"index": <1-based int>, "suggestion": "<rewritten sentence>"}.
Do not explain your changes. Return valid JSON only."""

    user_prompt = f"Rewrite these sentences:\n{sentences_block}"

    try:
        response = requests.post(
            "https://api.deepseek.com/chat/completions",
            headers={
                "Authorization": f"Bearer {api_key}",
                "Content-Type": "application/json",
            },
            json={
                "model": "deepseek-v4-pro",
                "messages": [
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": user_prompt},
                ],
                "thinking": {"type": "disabled"},
                "temperature": 0.4,
                "max_tokens": 2500,
                "stream": False,
            },
            timeout=60,
        )
        response.raise_for_status()
        data = response.json()
        raw = data["choices"][0]["message"]["content"].strip()
        raw = re.sub(r"^```(?:json)?\s*", "", raw)
        raw = re.sub(r"\s*```$", "", raw)
        parsed = json.loads(raw)
        for i, item in enumerate(flagged):
            match = next((x for x in parsed if x.get("index") == i + 1), None)
            item["suggestion"] = match["suggestion"] if match else _apply_replacements(item["sentence"])
        return flagged
    except Exception as exc:
        st.warning(f"DeepSeek request failed: {exc}. Falling back to rule-based suggestions.")
        return None


def _private_value(key: str) -> str:
    value = os.getenv(key, "").strip()
    if value:
        return value
    try:
        return str(st.secrets.get(key, "")).strip()
    except Exception:
        return ""