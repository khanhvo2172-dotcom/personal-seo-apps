import re
import os
import json
import html
from dataclasses import dataclass

import requests
import streamlit as st
import streamlit.components.v1 as components


@dataclass(frozen=True)
class PatternRule:
    name: str
    pattern: str
    issue: str


MODEL_OPTIONS = {
    "deepseek": {
        "label": "DeepSeek V4 Pro",
        "api_key": "DEEPSEEK_API_KEY",
        "model_key": "DEEPSEEK_MODEL",
        "default_model": "deepseek-v4-pro",
    },
    "claude": {
        "label": "Claude 4.6",
        "api_key": "ANTHROPIC_API_KEY",
        "model_key": "ANTHROPIC_MODEL",
        "default_model": "claude-sonnet-4-20250514",
    },
    "openai": {
        "label": "ChatGPT 5.5",
        "api_key": "OPENAI_API_KEY",
        "model_key": "OPENAI_MODEL",
        "default_model": "gpt-5.5",
    },
}


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
    st.header("Humanizer - AI detection")
    st.caption("Detects AI-writing patterns sentence by sentence and suggests cleaner rewrites.")
    _render_quick_guide()

    model_choice = st.radio(
        "AI model",
        options=list(MODEL_OPTIONS.keys()),
        format_func=lambda key: MODEL_OPTIONS[key]["label"],
        horizontal=True,
        help="Choose which model generates the rewrite suggestions.",
    )

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
        results = _get_suggestions(flagged, model_choice)

    st.subheader("Analysis Results")
    _render_results_table(results)
    _copy_button(results)


def _render_quick_guide():
    with st.expander("How this works"):
        st.markdown(
            """
1. Paste text you want to check.
2. Click **Analyze**.
3. The app scans each sentence for common AI-writing signals such as vague claims, inflated wording, filler phrases, formulaic structures, and chatbot-style artifacts.
4. For each flagged sentence, the app shows the original sentence, the issue, and a cleaner rewrite.
5. Use **Copy table** to paste the results into Google Docs, Sheets, Excel, or your editor.

If `DEEPSEEK_API_KEY` is available, suggestions use DeepSeek. If not, the app falls back to local rule-based rewrites.
            """.strip()
        )


def _render_results_table(results: list[dict]) -> None:
    st.markdown(
        f"""
        <style>
            .humanizer-results {{
                width: 100%;
                border-collapse: collapse;
                table-layout: fixed;
                font-size: 0.92rem;
            }}
            .humanizer-results th,
            .humanizer-results td {{
                border: 1px solid rgba(49, 51, 63, 0.18);
                padding: 0.65rem 0.75rem;
                text-align: left;
                vertical-align: top;
                white-space: pre-wrap;
                overflow-wrap: anywhere;
                word-break: break-word;
            }}
            .humanizer-results th {{
                background: rgba(49, 51, 63, 0.06);
                font-weight: 600;
            }}
            .humanizer-results col:nth-child(1),
            .humanizer-results col:nth-child(2) {{
                width: 40%;
            }}
            .humanizer-results col:nth-child(3) {{
                width: 20%;
            }}
        </style>
        {_results_table_html(results, include_styles=False)}
        """,
        unsafe_allow_html=True,
    )


def _copy_button(results: list[dict]) -> None:
    plain_json = json.dumps(_results_table_tsv(results))
    html_json = json.dumps(_results_table_html(results, include_styles=True))
    components.html(
        f"""
        <button id="copy-btn" onclick="copyText()" style="
            background:#FF4B4B;color:white;border:none;
            padding:8px 18px;border-radius:6px;cursor:pointer;
            font-size:14px;font-family:sans-serif;margin-top:4px;">
            Copy table
        </button>
        <span id="copy-msg" style="
            margin-left:10px;color:green;font-family:sans-serif;
            font-size:14px;display:none;">
            Copied!
        </span>
        <script>
        function copyText() {{
            var plainText = {plain_json};
            var htmlText = {html_json};
            if (navigator.clipboard && window.ClipboardItem) {{
                var item = new ClipboardItem({{
                    'text/html': new Blob([htmlText], {{ type: 'text/html' }}),
                    'text/plain': new Blob([plainText], {{ type: 'text/plain' }})
                }});
                navigator.clipboard.write([item]).then(showMsg).catch(function() {{
                    copyFallback(htmlText, plainText);
                }});
            }} else {{
                copyFallback(htmlText, plainText);
            }}
        }}
        function copyFallback(htmlText, plainText) {{
            var container = document.createElement('div');
            container.contentEditable = true;
            container.style.cssText = 'position:fixed;left:-9999px;top:0';
            container.innerHTML = htmlText;
            document.body.appendChild(container);

            var range = document.createRange();
            range.selectNodeContents(container);
            var selection = window.getSelection();
            selection.removeAllRanges();
            selection.addRange(range);

            var copied = document.execCommand('copy');
            selection.removeAllRanges();
            document.body.removeChild(container);

            if (!copied) {{
                var ta = document.createElement('textarea');
                ta.value = plainText;
                ta.style.cssText = 'position:fixed;opacity:0';
                document.body.appendChild(ta);
                ta.select();
                document.execCommand('copy');
                document.body.removeChild(ta);
            }}
            showMsg();
        }}
        function showMsg() {{
            var msg = document.getElementById('copy-msg');
            msg.style.display = 'inline';
            setTimeout(function() {{ msg.style.display = 'none'; }}, 2000);
        }}
        </script>
        """,
        height=50,
    )


def _results_table_html(results: list[dict], include_styles: bool) -> str:
    style_attr = ""
    cell_style = ""
    header_style = ""
    if include_styles:
        style_attr = (
            ' style="border-collapse:collapse;width:100%;table-layout:fixed;'
            'font-family:Arial,sans-serif;font-size:11pt;"'
        )
        cell_style = (
            ' style="border:1px solid #d0d0d0;padding:6px;vertical-align:top;'
            'white-space:pre-wrap;overflow-wrap:anywhere;word-break:break-word;"'
        )
        header_style = (
            ' style="border:1px solid #d0d0d0;padding:6px;vertical-align:top;'
            'white-space:pre-wrap;overflow-wrap:anywhere;word-break:break-word;'
            'font-weight:bold;background:#f2f2f2;"'
        )

    rows = [
        "<tr>"
        f"<th{header_style}>Current Sentence</th>"
        f"<th{header_style}>Suggestions</th>"
        f"<th{header_style}>Issue</th>"
        "</tr>"
    ]
    for r in results:
        rows.append(
            "<tr>"
            f"<td{cell_style}>{_html_cell(r['sentence'])}</td>"
            f"<td{cell_style}>{_html_cell(r['suggestion'])}</td>"
            f"<td{cell_style}>{_html_cell(r['issue'])}</td>"
            "</tr>"
        )
    return (
        f"<table class=\"humanizer-results\"{style_attr}>"
        "<colgroup><col><col><col></colgroup>"
        f"{''.join(rows)}"
        "</table>"
    )


def _html_cell(value: str) -> str:
    return html.escape(str(value)).replace("\n", "<br>")


def _results_table_tsv(results: list[dict]) -> str:
    rows = ["\t".join(["Current Sentence", "Suggestions", "Issue"])]
    for r in results:
        rows.append(
            "\t".join(
                [
                    _plain_cell(r["sentence"]),
                    _plain_cell(r["suggestion"]),
                    _plain_cell(r["issue"]),
                ]
            )
        )
    return "\n".join(rows)


def _plain_cell(value: str) -> str:
    return re.sub(r"[\t\r\n]+", " ", str(value)).strip()


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


def _get_suggestions(flagged: list[dict], model_choice: str) -> list[dict]:
    model_config = MODEL_OPTIONS.get(model_choice, MODEL_OPTIONS["deepseek"])
    api_key = _private_value(model_config["api_key"])
    if api_key:
        result = _suggest_with_model(flagged, model_choice, api_key)
        if result:
            return result
    else:
        st.info(
            f"{model_config['label']} API key not found. "
            f"Set {model_config['api_key']} in Streamlit secrets or environment variables. "
            "Using rule-based suggestions for now."
        )
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


def _suggest_with_model(flagged: list[dict], model_choice: str, api_key: str) -> list[dict] | None:
    if model_choice == "claude":
        return _suggest_with_claude(flagged, api_key)
    if model_choice == "openai":
        return _suggest_with_openai(flagged, api_key)
    return _suggest_with_deepseek(flagged, api_key)


def _build_prompts(flagged: list[dict]) -> tuple[str, str]:
    sentences_block = "\n".join(
        f"{i + 1}. {item['sentence']}" for i, item in enumerate(flagged)
    )

    system_prompt = """You are an expert human writing editor. Rewrite AI-sounding sentences to sound natural, specific, and genuinely human-written.

For each sentence: identify the AI patterns present, rewrite to fix them while preserving meaning and intent, then do a final anti-AI check for any remaining tells.

## CONTENT PATTERNS

1. INFLATED SIGNIFICANCE — Remove: "pivotal", "testament", "indelible mark", "lasting impact", "evolving landscape", "underscores/highlights its importance", "reflects broader", "setting the stage for", "key turning point", "focal point", "deeply rooted", "stands/serves as a reminder". Replace puffed-up statements with plain facts.

2. UNDUE NOTABILITY — Remove vague prestige claims ("active social media presence", "written by a leading expert"). Replace with specific facts.

3. SUPERFICIAL -ING PHRASES — Remove tacked-on participial padding: "symbolizing...", "reflecting...", "contributing to...", "showcasing...", "highlighting...", "emphasizing...", "ensuring...", "fostering...", "cultivating...". Cut them or turn them into real clauses.

4. PROMOTIONAL LANGUAGE — Remove: "boasts", "vibrant", "rich cultural", "profound", "renowned", "breathtaking", "must-visit", "stunning", "groundbreaking", "nestled", "in the heart of", "commitment to excellence". Replace with plain, factual description.

5. VAGUE ATTRIBUTIONS — Remove: "industry reports", "observers have cited", "experts argue", "some critics argue", "several sources", "many believe". Use named specific sources or cut the attribution.

6. FORMULAIC CHALLENGES SECTIONS — Remove "Despite X challenges... continues to thrive" structures. Replace with specific, concrete facts about what actually happened.

## LANGUAGE & GRAMMAR PATTERNS

7. AI VOCABULARY — Replace or cut: "additionally", "actually", "align with", "crucial", "delve", "emphasizing", "enduring", "enhance", "fostering", "garner", "highlight (verb)", "interplay", "intricate/intricacies", "key (adjective)", "landscape (abstract noun)", "pivotal", "showcase", "tapestry", "testament", "underscore (verb)", "valuable", "vibrant".

8. COPULA AVOIDANCE — Replace "serves as", "stands as", "marks a", "represents a", "boasts", "features a", "offers a" with plain "is", "has", "are".

9. NEGATIVE PARALLELISMS — Rewrite "not only...but also", "not just X; it's Y", "not merely X, it's also Y". Remove tailing negation fragments like "no guessing", "no wasted motion" — write them as real clauses instead.

10. RULE OF THREE — Break up ideas forced into exactly three items when the grouping is artificial.

11. SYNONYM CYCLING — Collapse synonym-rotated references ("the protagonist... the main character... the central figure... the hero") back to one consistent term.

12. FALSE RANGES — Rewrite "from X to Y, from A to B" when X/Y and A/B are not on a meaningful scale.

13. PASSIVE VOICE / SUBJECTLESS FRAGMENTS — "No configuration needed" → "You don't need a configuration file". Make the actor explicit when active voice is clearer.

## STYLE PATTERNS

14. EM DASH OVERUSE — Replace em dashes (— or –) with commas, periods, or parentheses.

15. BOLDFACE OVERUSE — Strip **bold** markdown from inline prose. Bold is for UI labels or warnings, not emphasis in running text.

16. INLINE-HEADER LISTS — Convert "- **Header:** description" bullet lists into flowing prose sentences.

17. TITLE CASE HEADINGS — "## Strategic Negotiations And Partnerships" → "## Strategic negotiations and partnerships".

18. EMOJIS IN PROSE — Remove 🚀 💡 ✅ and similar decorative emojis from prose and bullet points.

19. CURLY QUOTES — Replace curly/smart quotes (" " ' ') with straight quotes (" ').

## COMMUNICATION PATTERNS

20. CHATBOT ARTIFACTS — Remove: "I hope this helps", "Of course!", "Certainly!", "Great question!", "You're absolutely right", "Let me know if you'd like", "Here is an overview of", "Would you like me to expand".

21. KNOWLEDGE-CUTOFF DISCLAIMERS — Remove: "as of my last update", "up to my last training", "while specific details are limited", "based on available information". Either state what is known or cut it.

22. SYCOPHANTIC TONE — Remove people-pleasing openers, unnecessary affirmations, and servile closers.

## FILLER & HEDGING

23. FILLER PHRASES:
- "in order to" → "to"
- "due to the fact that" → "because"
- "at this point in time" → "now"
- "in the event that" → "if"
- "has the ability to" → "can"
- "it is important to note that" → (remove; start with the actual point)

24. EXCESSIVE HEDGING — Collapse "could potentially possibly be argued that... might have some effect" → "may affect".

25. GENERIC CONCLUSIONS — Remove: "the future looks bright", "exciting times lie ahead", "continues their journey toward excellence", "this represents a major step forward". Replace with a concrete next fact or cut.

26. HYPHENATED WORD PAIR OVERUSE — Remove hyphens from over-hyphenated common compounds: "cross functional", "data driven", "client facing", "decision making", "high quality", "well known", "real time", "long term", "end to end". (Technical or uncommon compound modifiers are fine to hyphenate.)

27. PERSUASIVE AUTHORITY TROPES — Rewrite "The real question is...", "At its core...", "What really matters...", "Fundamentally...", "The heart of the matter...". These announce depth but usually just restate an obvious point.

28. SIGNPOSTING / ANNOUNCEMENTS — Remove "Let's dive in", "Let's explore", "Let's break this down", "Here's what you need to know", "Now let's look at", "Without further ado". Just say the thing directly.

29. FRAGMENTED HEADERS — Remove one-line warm-up sentences that merely restate the heading (e.g. "Speed matters." as the only content under "## Performance").

## HOW TO ADD SOUL

After removing AI patterns, do not produce sterile or soulless text. Good rewriting:
- Has opinions: react to facts, don't just report them.
- Varies rhythm: mix short sentences with longer ones.
- Acknowledges complexity: real humans have mixed feelings.
- Uses "I" when it fits naturally.
- Is specific: replace vague claims with concrete details.
- Uses plain verbs: "is", "has", "shows", "uses" over inflated alternatives.

Return ONLY a JSON array. Each element: {"index": <1-based int>, "suggestion": "<rewritten sentence>"}.
Do not explain your changes. Return valid JSON only."""

    user_prompt = f"Rewrite these sentences:\n{sentences_block}"
    return system_prompt, user_prompt


def _apply_model_suggestions(flagged: list[dict], raw: str) -> list[dict]:
    raw = raw.strip()
    raw = re.sub(r"^```(?:json)?\s*", "", raw)
    raw = re.sub(r"\s*```$", "", raw)
    parsed = json.loads(raw)
    for i, item in enumerate(flagged):
        match = next((x for x in parsed if x.get("index") == i + 1), None)
        item["suggestion"] = match["suggestion"] if match else _apply_replacements(item["sentence"])
    return flagged


def _suggest_with_deepseek(flagged: list[dict], api_key: str) -> list[dict] | None:
    system_prompt, user_prompt = _build_prompts(flagged)
    model = _private_value("DEEPSEEK_MODEL") or MODEL_OPTIONS["deepseek"]["default_model"]

    try:
        response = requests.post(
            "https://api.deepseek.com/chat/completions",
            headers={
                "Authorization": f"Bearer {api_key}",
                "Content-Type": "application/json",
            },
            json={
                "model": model,
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
        return _apply_model_suggestions(flagged, raw)
    except Exception as exc:
        st.warning(f"DeepSeek request failed: {exc}. Falling back to rule-based suggestions.")
        return None


def _suggest_with_claude(flagged: list[dict], api_key: str) -> list[dict] | None:
    system_prompt, user_prompt = _build_prompts(flagged)
    model = _private_value("ANTHROPIC_MODEL") or MODEL_OPTIONS["claude"]["default_model"]

    try:
        response = requests.post(
            "https://api.anthropic.com/v1/messages",
            headers={
                "x-api-key": api_key,
                "anthropic-version": "2023-06-01",
                "Content-Type": "application/json",
            },
            json={
                "model": model,
                "system": system_prompt,
                "messages": [
                    {"role": "user", "content": user_prompt},
                ],
                "temperature": 0.4,
                "max_tokens": 2500,
            },
            timeout=60,
        )
        response.raise_for_status()
        data = response.json()
        raw = "".join(
            block.get("text", "")
            for block in data.get("content", [])
            if block.get("type") == "text"
        )
        return _apply_model_suggestions(flagged, raw)
    except Exception as exc:
        st.warning(f"Claude request failed: {exc}. Falling back to rule-based suggestions.")
        return None


def _suggest_with_openai(flagged: list[dict], api_key: str) -> list[dict] | None:
    system_prompt, user_prompt = _build_prompts(flagged)
    model = _private_value("OPENAI_MODEL") or MODEL_OPTIONS["openai"]["default_model"]

    try:
        response = requests.post(
            "https://api.openai.com/v1/chat/completions",
            headers={
                "Authorization": f"Bearer {api_key}",
                "Content-Type": "application/json",
            },
            json={
                "model": model,
                "messages": [
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": user_prompt},
                ],
                "temperature": 0.4,
                "max_completion_tokens": 2500,
            },
            timeout=60,
        )
        response.raise_for_status()
        data = response.json()
        raw = data["choices"][0]["message"]["content"].strip()
        return _apply_model_suggestions(flagged, raw)
    except Exception as exc:
        st.warning(f"ChatGPT request failed: {exc}. Falling back to rule-based suggestions.")
        return None


def _private_value(key: str) -> str:
    value = os.getenv(key, "").strip()
    if value:
        return value
    try:
        return str(st.secrets.get(key, "")).strip()
    except Exception:
        return ""
