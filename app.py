from __future__ import annotations

import io

import streamlit as st

from excel_handler import translate_workbook, get_workbook_info
from translator import (
    DOMAIN_PROMPTS,
    LANGUAGE_MAP,
    AUTH_MODE_API_KEY,
    AUTH_MODE_OAUTH,
    TranslatorConfig,
)
from oauth_openai import get_reusable_auth, is_authenticated, normalize_model, AuthTokens

st.set_page_config(page_title="Excel Translator", page_icon="📊", layout="centered")

st.markdown(
    """
    <style>
    .main-header { font-size: 2.2rem; font-weight: 700; margin-bottom: 0.2rem; }
    .sub-header { font-size: 1.0rem; color: #888; margin-bottom: 2rem; }
    </style>
    """,
    unsafe_allow_html=True,
)

st.markdown('<div class="main-header">Excel Translator</div>', unsafe_allow_html=True)
st.markdown(
    '<div class="sub-header">Upload an Excel file, choose your settings, and get a fully translated file with formatting preserved.</div>',
    unsafe_allow_html=True,
)

# ── Sidebar: Auth ──
with st.sidebar:
    st.header("Authentication")
    auth_mode = st.radio(
        "Auth Method",
        ["ChatGPT OAuth (Free)", "API Key"],
        index=0,
        help="OAuth: use your ChatGPT Plus subscription (no extra cost). API Key: pay-per-use.",
    )

    config: TranslatorConfig | None = None

    if auth_mode == "ChatGPT OAuth (Free)":
        st.caption("Login with your ChatGPT account. Uses your existing subscription.")

        # Check saved auth
        if is_authenticated():
            st.success("Logged in (saved session)")
        else:
            st.info("Click below to login via browser.")

        login_url_container = st.empty()

        if st.button("Login with ChatGPT", use_container_width=True):
            def _show_url(url: str) -> None:
                login_url_container.markdown(f"[Click here to login with ChatGPT]({url})")

            try:
                with st.spinner("Waiting for login... (click the link above)"):
                    tokens = get_reusable_auth(
                        force_login=True, open_browser=False, url_callback=_show_url,
                    )
                login_url_container.empty()
                st.session_state["oauth_auth"] = tokens
                acct = tokens.accountId or "unknown"
                st.success(f"Logged in! (account: {acct[:8]}...)")
                st.rerun()
            except Exception as e:
                login_url_container.empty()
                st.error(f"Login failed: {e}")

        oauth_auth: AuthTokens | None = st.session_state.get("oauth_auth")  # type: ignore[assignment]
        if oauth_auth is None:
            try:
                oauth_auth = get_reusable_auth(force_login=False, open_browser=False)
                st.session_state["oauth_auth"] = oauth_auth
            except Exception:
                oauth_auth = None

        oauth_models = [
            ("gpt-5.1-codex-mini", "5.1-codex-mini — lightest, fastest"),
            ("gpt-5.1-codex", "5.1-codex — balanced"),
            ("gpt-5.2-codex", "5.2-codex — highest quality"),
        ]
        oauth_model = st.selectbox(
            "Model",
            [m[0] for m in oauth_models],
            index=0,
            format_func=lambda x: next(label for key, label in oauth_models if key == x),
        )

        if oauth_auth and oauth_auth.access:
            config = TranslatorConfig(
                auth_mode=AUTH_MODE_OAUTH,
                access_token=oauth_auth.access,
                account_id=oauth_auth.accountId or "",
                oauth_model=oauth_model,
            )

    else:
        api_key = st.text_input(
            "OpenAI API Key", type="password", placeholder="sk-...",
        )
        model = st.selectbox("Model", ["gpt-4o-mini", "gpt-4o"], index=0)
        st.caption("Your API key is used only for this session.")
        if api_key:
            config = TranslatorConfig(
                auth_mode=AUTH_MODE_API_KEY, api_key=api_key, model=model,
            )

    st.divider()
    batch_size = st.slider("Batch Size", min_value=10, max_value=100, value=50, step=10,
                           help="Cells per API call. Higher = fewer calls but longer per call.")
    if config:
        mode_label = "OAuth (ChatGPT)" if config.auth_mode == AUTH_MODE_OAUTH else "API Key"
        st.caption(f"Mode: {mode_label}")

# ── Main Area ──
col1, col2 = st.columns(2)

with col1:
    source_lang = st.selectbox("Source Language", list(LANGUAGE_MAP.keys()), index=0)

with col2:
    target_options = [lang for lang in LANGUAGE_MAP.keys() if lang != source_lang]
    default_idx = target_options.index("영어") if "영어" in target_options else 0
    target_lang = st.selectbox("Target Language", target_options, index=default_idx)

domain = st.selectbox(
    "Document Type",
    list(DOMAIN_PROMPTS.keys()),
    index=0,
    help="Helps the AI use domain-specific terminology.",
)

domain_descriptions = {
    "의료/병원": "Medical terminology (Blood Test, Ultrasound, Endoscopy, etc.)",
    "법률": "Legal terminology",
    "기술/IT": "Technical/IT terminology",
    "비즈니스": "Business terminology",
    "일반": "General translation",
}
st.caption(f"→ {domain_descriptions.get(domain, '')}")

# ── File Upload ──
st.divider()
uploaded_file = st.file_uploader(
    "Upload Excel File (.xlsx)", type=["xlsx"],
    help="All formatting, styles, merged cells, and layout will be preserved.",
)

if uploaded_file:
    file_bytes = io.BytesIO(uploaded_file.read())
    info = get_workbook_info(file_bytes, source_lang=source_lang)
    file_bytes.seek(0)

    st.markdown("#### File Summary")
    col_a, col_b, col_c, col_d = st.columns(4)
    col_a.metric("Sheets", len(info["sheets"]))
    col_b.metric("Total Text Cells", f"{info['total_all_cells']:,}")
    col_c.metric("To Translate", f"{info['total_text_cells']:,}")
    unique = info.get("unique_texts", info["total_text_cells"])
    cache_hit_pct = round((1 - unique / info["total_text_cells"]) * 100) if info["total_text_cells"] > 0 else 0
    col_d.metric("Unique Texts", f"{unique:,}", delta=f"-{cache_hit_pct}% cache", delta_color="inverse")

    skipped = info["total_all_cells"] - info["total_text_cells"]
    if skipped > 0:
        st.caption(f"Skipping {skipped:,} cells (numbers, symbols, non-{source_lang} text)")

    with st.expander("Sheet Details"):
        for sheet in info["sheets"]:
            st.text(f"  {sheet['name']}: {sheet['text_cells']}/{sheet['all_cells']} cells to translate")

    if info["total_text_cells"] == 0:
        st.warning("No text cells found to translate.")
    else:
        estimated_calls = (unique + batch_size - 1) // batch_size
        cost_label = f"~{estimated_calls} calls (Free via ChatGPT)" if config and config.auth_mode == AUTH_MODE_OAUTH else f"~{estimated_calls} API calls"
        st.caption(f"Estimated: {cost_label} (cache dedup: {info['total_text_cells']:,} → {unique:,} unique)")

        if st.button("Translate", type="primary", use_container_width=True):
            if not config:
                st.error("Please login (OAuth) or enter an API key in the sidebar.")
            elif source_lang == target_lang:
                st.error("Source and target languages must be different.")
            else:
                progress_bar = st.progress(0, text="Translating...")

                def update_progress(current, total):
                    progress_bar.progress(current / total, text=f"Translating... {current:,}/{total:,} unique texts")

                try:
                    result = translate_workbook(
                        input_path=file_bytes,
                        source_lang=source_lang,
                        target_lang=target_lang,
                        domain=domain,
                        config=config,
                        batch_size=batch_size,
                        progress_callback=update_progress,
                    )

                    progress_bar.progress(1.0, text="Complete!")

                    original_name = uploaded_file.name.rsplit(".", 1)[0]
                    lang_suffix = (LANGUAGE_MAP.get(target_lang) or target_lang).lower()[:2]
                    output_name = f"{original_name}_translated_{lang_suffix}.xlsx"

                    st.success(f"Translation complete! {info['total_text_cells']:,} cells translated ({unique:,} unique, {cache_hit_pct}% cache hit)")

                    st.download_button(
                        label="Download Translated File",
                        data=result,
                        file_name=output_name,
                        mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                        type="primary",
                        use_container_width=True,
                    )

                except Exception as e:
                    progress_bar.empty()
                    st.error(f"Translation failed: {str(e)}")
                    if "auth" in str(e).lower() or "token" in str(e).lower():
                        st.info("Try re-logging in via the sidebar.")
