import itertools
import json

import pandas as pd
import streamlit as st
import streamlit.components.v1 as components

MAX_LISTS = 8
MAX_COMBINATIONS = 200_000  # safety cap so a huge product can't freeze the app


def render():
    st.header("Keyword Combiner")
    st.caption(
        "Combine multiple lists into every possible combination (Cartesian product) — "
        "great for generating keyword variations (e.g. modifiers × core terms)."
    )
    _render_quick_guide()

    # Kept OUTSIDE the form so the number of text areas updates live.
    num_lists = int(
        st.number_input(
            "How many lists do you want to combine?",
            min_value=2,
            max_value=MAX_LISTS,
            value=2,
            step=1,
            help="Each list is one set of values. The result is every combination across all lists.",
        )
    )

    with st.form("cartesian_product_form"):
        n_cols = min(num_lists, 4)
        cols = st.columns(n_cols)
        list_inputs = []
        for i in range(num_lists):
            with cols[i % n_cols]:
                list_inputs.append(
                    st.text_area(
                        f"List {i + 1}",
                        key=f"cp_list_{i}",
                        placeholder="One value per line",
                        height=200,
                    )
                )

        opt1, opt2 = st.columns(2)
        with opt1:
            separator = st.text_input(
                "Separator between combined items",
                value="",
                help="Leave empty to concatenate directly (like the original script). "
                "Type a space to build keyword phrases.",
            )
        with opt2:
            trim = st.checkbox("Trim spaces from each item", value=True)

        submitted = st.form_submit_button("🧮 Generate combinations", type="primary")

    if submitted:
        st.session_state.cp_result = _generate(list_inputs, separator, trim)

    result = st.session_state.get("cp_result")
    if result is not None:
        _render_results(result)


def _render_quick_guide():
    with st.expander("How this works"):
        st.markdown(
            """
1. Pick how many lists you want to combine (2–8).
2. Paste values into each list, **one per line**.
3. (Optional) Set a **separator** — leave empty to join directly, or use a space for phrases.
4. Click **Generate combinations** to get every combination, then download as CSV or TXT.

Notes:
- The result is the **Cartesian product**: every item of List 1 paired with every item of
  List 2, and so on. `[a, b] × [1, 2] → a1, a2, b1, b2`.
- Blank lines are ignored; duplicates and order are preserved.
- To avoid freezing, the total is capped at **200,000** combinations.
            """.strip()
        )


# ── core logic (mirrors the Apps Script) ─────────────────────

def _parse_list(text: str, trim: bool) -> list[str]:
    """One value per line; drop blanks (like the script's filter(String))."""
    out = []
    for line in (text or "").splitlines():
        val = line.strip() if trim else line
        if val:
            out.append(val)
    return out


def _generate(list_inputs: list[str], separator: str, trim: bool):
    lists = [_parse_list(t, trim) for t in list_inputs]

    empty = [i + 1 for i, lst in enumerate(lists) if not lst]
    if empty:
        st.error(
            f"List(s) {', '.join(map(str, empty))} are empty. "
            "Please add at least one value to every list."
        )
        return None

    total = 1
    for lst in lists:
        total *= len(lst)

    if total > MAX_COMBINATIONS:
        st.error(
            f"That would generate **{total:,}** combinations, above the safety limit of "
            f"{MAX_COMBINATIONS:,}. Remove some items so the product is smaller."
        )
        return None

    combos = [separator.join(parts) for parts in itertools.product(*lists)]
    sizes = " × ".join(str(len(lst)) for lst in lists)
    return {"combos": combos, "sizes": sizes}


def _render_results(result: dict):
    combos = result["combos"]
    df = pd.DataFrame({"#": range(1, len(combos) + 1), "Combination": combos})

    st.success(f"✅ Generated **{len(combos):,}** combinations  ({result['sizes']})")
    st.dataframe(df, use_container_width=True, hide_index=True, height=460)

    txt = "\n".join(combos)
    c1, c2, c3 = st.columns(3)
    with c1:
        _copy_button(txt)
    with c2:
        st.download_button(
            "⬇️ Download as CSV",
            data=df[["Combination"]].to_csv(index=False).encode("utf-8"),
            file_name="cartesian_product.csv",
            mime="text/csv",
            use_container_width=True,
        )
    with c3:
        st.download_button(
            "⬇️ Download as TXT",
            data=txt.encode("utf-8"),
            file_name="cartesian_product.txt",
            mime="text/plain",
            use_container_width=True,
        )


def _copy_button(text: str):
    """Clipboard 'Copy all' button. Runs inside a component iframe, so it is not
    affected by the app-wide Ctrl+C guard; falls back to execCommand when the
    async clipboard API is blocked by the iframe permissions policy."""
    payload = json.dumps(text)
    components.html(
        f"""
        <style>
        .cp-btn {{
            width: 100%;
            height: 38px;
            font-family: 'Google Sans','Roboto',sans-serif;
            font-weight: 500;
            font-size: 14px;
            letter-spacing: .25px;
            color: #1d2939;
            background: #fff;
            border: 1px solid #dadce0;
            border-radius: 4px;
            cursor: pointer;
            transition: background .18s, box-shadow .18s, border-color .18s;
        }}
        .cp-btn:hover {{
            background: #f8f9fa;
            border-color: #4285F4;
            box-shadow: 0 1px 2px rgba(60,64,67,.15);
        }}
        .cp-btn.copied {{ color: #0c9d61; border-color: #0c9d61; }}
        </style>
        <button class="cp-btn" id="cpbtn">📋 Copy all</button>
        <script>
        const data = {payload};
        const btn = document.getElementById('cpbtn');
        btn.addEventListener('click', async () => {{
            let ok = false;
            try {{
                await navigator.clipboard.writeText(data);
                ok = true;
            }} catch (e) {{
                try {{
                    const ta = document.createElement('textarea');
                    ta.value = data;
                    ta.style.position = 'fixed';
                    ta.style.opacity = '0';
                    document.body.appendChild(ta);
                    ta.focus(); ta.select();
                    ok = document.execCommand('copy');
                    ta.remove();
                }} catch (e2) {{ ok = false; }}
            }}
            btn.textContent = ok ? '✅ Copied!' : '⚠️ Press Ctrl+C';
            btn.classList.toggle('copied', ok);
            setTimeout(() => {{
                btn.textContent = '📋 Copy all';
                btn.classList.remove('copied');
            }}, 1600);
        }});
        </script>
        """,
        height=46,
    )
