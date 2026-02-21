"""
Translation engine — supports both OpenAI API key and OAuth Codex modes.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass

DOMAIN_PROMPTS = {
    "의료/병원": (
        "You are a professional medical translator with expertise in hospital administration, "
        "clinical terminology, health checkup packages, and medical documentation. "
        "Use standard medical terminology (e.g., '혈액검사' → 'Blood Test', '초음파' → 'Ultrasound', "
        "'내시경' → 'Endoscopy', '종합검진' → 'Comprehensive Health Checkup', '수진자' → 'Patient/Examinee', "
        "'판정' → 'Assessment/Diagnosis', '소견' → 'Findings', '정상' → 'Normal', '이상소견' → 'Abnormal Findings'). "
        "Maintain clinical accuracy and professional tone throughout."
    ),
    "법률": (
        "You are a professional legal translator. Use standard legal terminology "
        "and maintain the formal tone appropriate for legal documents."
    ),
    "기술/IT": (
        "You are a professional technical translator specializing in IT and software. "
        "Use standard technical terminology and maintain precision."
    ),
    "비즈니스": (
        "You are a professional business translator. Use standard business terminology "
        "and maintain a professional, formal tone."
    ),
    "일반": (
        "You are a professional translator. Provide accurate and natural translations."
    ),
}

LANGUAGE_MAP = {
    "한국어": "Korean",
    "영어": "English",
    "일본어": "Japanese",
    "중국어 (간체)": "Simplified Chinese",
    "중국어 (번체)": "Traditional Chinese",
    "스페인어": "Spanish",
    "프랑스어": "French",
    "독일어": "German",
    "베트남어": "Vietnamese",
    "태국어": "Thai",
}

# --- Auth mode constants ---
AUTH_MODE_API_KEY = "api_key"
AUTH_MODE_OAUTH = "oauth"


@dataclass
class TranslatorConfig:
    """Holds auth info for either API key or OAuth mode."""

    auth_mode: str  # "api_key" or "oauth"
    # API key mode
    api_key: str | None = None
    model: str = "gpt-4o-mini"
    # OAuth mode
    access_token: str | None = None
    account_id: str | None = None
    oauth_model: str = "gpt-5.1-codex-mini"


_NEWLINE_PLACEHOLDER = " ∥ "

_NEWLINE_RE = re.compile(r'_x000D_|\r\n|\r|\n')


def _flatten(text: str) -> str:
    return _NEWLINE_RE.sub(_NEWLINE_PLACEHOLDER, text)


def _unflatten(text: str) -> str:
    return text.replace(_NEWLINE_PLACEHOLDER, "\n")


def _build_system_prompt(domain: str, source_lang: str, target_lang: str) -> str:
    domain_prompt = DOMAIN_PROMPTS.get(domain, DOMAIN_PROMPTS["일반"])
    src = LANGUAGE_MAP.get(source_lang, source_lang)
    tgt = LANGUAGE_MAP.get(target_lang, target_lang)
    return (
        f"{domain_prompt}\n\n"
        f"Translate the following texts from {src} to {tgt}.\n"
        f"Rules:\n"
        f"- Each item is on ONE line, prefixed with [number].\n"
        f"- Return ONLY the translations, one per line, prefixed with the SAME [number] tag.\n"
        f"- The separator ∥ represents a line break — preserve it in your output.\n"
        f"- Do NOT add explanations, notes, or extra text.\n"
        f"- Preserve numbers, dates, units, proper nouns, and formatting as-is.\n"
        f"- If a text is already in {tgt} or is a number/symbol only, return it unchanged.\n"
        f"- Maintain the same level of formality as the source."
    )


_TAGGED_LINE = re.compile(r'^\[(\d+)\]\s*(.*)')


def _parse_numbered_response(reply: str, results: list[str]) -> list[int]:
    """Parse [idx] prefixed lines. Returns list of indices that were NOT parsed."""
    parsed: set[int] = set()
    for line in reply.split("\n"):
        line = line.strip()
        if not line:
            continue
        m = _TAGGED_LINE.match(line)
        if not m:
            continue
        idx = int(m.group(1))
        translated = m.group(2).strip()
        if 0 <= idx < len(results) and translated:
            results[idx] = translated
            parsed.add(idx)
    return [i for i in range(len(results)) if i not in parsed]


def _translate_batch_api_key(
    numbered: str, system_msg: str, api_key: str, model: str,
) -> str:
    """Translate via OpenAI API key (Chat Completions)."""
    from openai import OpenAI

    client = OpenAI(api_key=api_key)
    response = client.chat.completions.create(
        model=model,
        messages=[
            {"role": "system", "content": system_msg},
            {"role": "user", "content": numbered},
        ],
        temperature=0.1,
    )
    content = response.choices[0].message.content
    return content.strip() if content else ""


def _translate_batch_oauth(
    numbered: str, system_msg: str, access_token: str, account_id: str, model: str,
) -> str:
    """Translate via OAuth Codex API."""
    from codex_client import query_codex

    return query_codex(
        access_token=access_token,
        account_id=account_id,
        model=model,
        system_prompt=system_msg,
        user_message=numbered,
    )


_MAX_RETRY = 2


def _call_llm(numbered: str, system_msg: str, config: TranslatorConfig) -> str:
    if config.auth_mode == AUTH_MODE_OAUTH:
        if not config.access_token or not config.account_id:
            raise ValueError("OAuth mode requires access_token and account_id")
        return _translate_batch_oauth(
            numbered, system_msg,
            config.access_token, config.account_id, config.oauth_model,
        )
    if not config.api_key:
        raise ValueError("API key mode requires an API key")
    return _translate_batch_api_key(
        numbered, system_msg, config.api_key, config.model,
    )


def translate_texts(
    texts: list[str],
    source_lang: str,
    target_lang: str,
    domain: str = "일반",
    config: TranslatorConfig | None = None,
    batch_size: int = 50,
    cache: dict[str, str] | None = None,
    model: str = "gpt-4o-mini",
    api_key: str | None = None,
) -> list[str]:
    if config is None:
        key = api_key or os.getenv("OPENAI_API_KEY", "")
        config = TranslatorConfig(auth_mode=AUTH_MODE_API_KEY, api_key=key, model=model)
    if cache is None:
        cache = {}

    system_msg = _build_system_prompt(domain, source_lang, target_lang)

    indexed_texts = [(i, t) for i, t in enumerate(texts) if t and t.strip()]
    if not indexed_texts:
        return texts

    results = list(texts)

    uncached = [(i, t) for i, t in indexed_texts if t not in cache]
    for i, t in indexed_texts:
        if t in cache:
            results[i] = cache[t]

    if not uncached:
        return results

    for batch_start in range(0, len(uncached), batch_size):
        batch = uncached[batch_start: batch_start + batch_size]
        flat_map = {idx: _flatten(text) for idx, text in batch}
        numbered = "\n".join(f"[{idx}] {flat_map[idx]}" for idx, _ in batch)

        reply = _call_llm(numbered, system_msg, config)
        missed = _parse_numbered_response(reply, results)

        retry_items = [(idx, flat_map[idx]) for idx in missed if idx in flat_map]
        for attempt in range(_MAX_RETRY):
            if not retry_items:
                break
            retry_numbered = "\n".join(f"[{idx}] {text}" for idx, text in retry_items)
            retry_reply = _call_llm(retry_numbered, system_msg, config)
            still_missed = _parse_numbered_response(retry_reply, results)
            retry_items = [(idx, flat_map[idx]) for idx in still_missed if idx in flat_map]

        for idx, original in batch:
            results[idx] = _unflatten(results[idx])
            if results[idx] != original:
                cache[original] = results[idx]

    return results
