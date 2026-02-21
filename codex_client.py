"""
Synchronous Codex API client for ChatGPT backend.
Uses urllib (stdlib) — no extra dependencies needed.
"""

from __future__ import annotations

import json
import logging
import time
from typing import Any
from urllib.error import HTTPError
from urllib.request import Request, urlopen

logger = logging.getLogger(__name__)

CODEX_URL = "https://chatgpt.com/backend-api/codex/responses"

MAX_RETRIES = 5
INITIAL_BACKOFF = 2.0  # seconds
MAX_BACKOFF = 60.0


def parse_sse_text(raw: str) -> str:
    delta_parts: list[str] = []
    final_text: str | None = None

    for line in raw.split("\n"):
        if not line.startswith("data:"):
            continue
        payload = line[5:].strip()
        if not payload or payload == "[DONE]":
            continue

        try:
            data = json.loads(payload)
        except json.JSONDecodeError:
            continue

        if data.get("type") == "response.output_text.delta" and isinstance(data.get("delta"), str):
            delta_parts.append(data["delta"])

        output = data.get("output")
        if isinstance(output, list):
            for out in output:
                content = out.get("content") if isinstance(out, dict) else None
                if not isinstance(content, list):
                    continue
                for item in content:
                    if isinstance(item, dict) and item.get("type") == "output_text" and isinstance(item.get("text"), str):
                        final_text = item["text"]

    reply = "".join(delta_parts) if delta_parts else (final_text or "")
    return reply.strip()


def query_codex(
    access_token: str,
    account_id: str,
    model: str,
    system_prompt: str,
    user_message: str,
    timeout: float = 120.0,
) -> str:
    if not access_token or not account_id:
        raise ValueError("Missing access_token or account_id. Please login first.")

    body: dict[str, Any] = {
        "model": model,
        "stream": True,
        "store": False,
        "instructions": system_prompt,
        "reasoning": {"effort": "low", "summary": "auto"},
        "text": {"verbosity": "medium"},
        "include": ["reasoning.encrypted_content"],
        "input": [
            {
                "role": "user",
                "content": [{"type": "input_text", "text": user_message}],
            }
        ],
    }

    req_body = json.dumps(body).encode("utf-8")

    backoff = INITIAL_BACKOFF
    last_error: Exception | None = None

    for attempt in range(MAX_RETRIES + 1):
        req = Request(CODEX_URL, data=req_body, method="POST")
        req.add_header("Content-Type", "application/json")
        req.add_header("Authorization", f"Bearer {access_token}")
        req.add_header("chatgpt-account-id", account_id)
        req.add_header("OpenAI-Beta", "responses=experimental")
        req.add_header("originator", "opencode")
        req.add_header("accept", "text/event-stream")

        try:
            with urlopen(req, timeout=int(timeout)) as resp:
                raw = resp.read().decode("utf-8", errors="replace")

            result = parse_sse_text(raw)
            if not result:
                raise RuntimeError("Empty response from Codex API")
            return result

        except HTTPError as e:
            last_error = e
            error_body = ""
            try:
                error_body = e.read().decode("utf-8", errors="replace")
            except Exception:
                pass

            if e.code == 429 and attempt < MAX_RETRIES:
                retry_after = e.headers.get("Retry-After")
                wait = float(retry_after) if retry_after else backoff
                wait = min(wait, MAX_BACKOFF)
                logger.warning("Rate limited (429). Waiting %.1fs (attempt %d/%d)", wait, attempt + 1, MAX_RETRIES)
                time.sleep(wait)
                backoff = min(backoff * 2, MAX_BACKOFF)
                continue
            raise RuntimeError(f"Codex API error: HTTP {e.code} — {error_body[:500]}") from e

        except Exception as e:
            last_error = e
            if attempt < MAX_RETRIES:
                logger.warning("Request failed: %s. Retrying in %.1fs (attempt %d/%d)", str(e), backoff, attempt + 1, MAX_RETRIES)
                time.sleep(backoff)
                backoff = min(backoff * 2, MAX_BACKOFF)
                continue
            raise

    raise RuntimeError(f"Failed after {MAX_RETRIES} retries: {last_error}")
