"""Gemini AI client — stdlib only (urllib), no extra requirements."""

from __future__ import annotations

import asyncio
import json
import logging
import urllib.error
import urllib.request

from config import GEMINI_API_KEY, GEMINI_MODEL

log = logging.getLogger("vexdeploy.ai")

API_BASE = "https://generativelanguage.googleapis.com/v1beta/models"
DEFAULT_TIMEOUT = 25


class AIError(RuntimeError):
    """Gemini call failed (network, quota, safety block, bad key)."""


def is_configured() -> bool:
    return bool(GEMINI_API_KEY)


def _call(prompt: str, system: str | None = None, timeout: int = DEFAULT_TIMEOUT) -> str:
    if not GEMINI_API_KEY:
        raise AIError("GEMINI_API_KEY is not set")
    url = f"{API_BASE}/{GEMINI_MODEL}:generateContent?key={GEMINI_API_KEY}"
    body: dict = {
        "contents": [{"role": "user", "parts": [{"text": prompt}]}],
        "generationConfig": {
            "temperature": 0.5,
            "maxOutputTokens": 1500,
            "topP": 0.95,
        },
    }
    if system:
        body["systemInstruction"] = {"parts": [{"text": system}]}
    req = urllib.request.Request(
        url,
        data=json.dumps(body).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            data = json.loads(resp.read().decode("utf-8", errors="replace") or "{}")
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")[:300]
        raise AIError(f"Gemini HTTP {exc.code}: {detail}") from exc
    except AIError:
        raise
    except Exception as exc:  # noqa: BLE001 — surface any transport error to caller
        raise AIError(f"Gemini request failed: {exc}") from exc

    try:
        candidates = data.get("candidates") or []
        parts = (candidates[0].get("content") or {}).get("parts") or []
        text = "".join(p.get("text", "") for p in parts).strip()
    except (IndexError, AttributeError, TypeError):
        text = ""
    if not text:
        raise AIError(f"empty Gemini response: {json.dumps(data)[:200]}")
    return text


async def chat(prompt: str, system: str | None = None, timeout: int = DEFAULT_TIMEOUT) -> str:
    """Async wrapper around the blocking Gemini call."""
    return await asyncio.to_thread(_call, prompt, system, timeout)


def extract_json(text: str):
    """Pull the first JSON object/array out of a model reply (tolerates fences/prose)."""
    import re

    if not text:
        return None
    cleaned = re.sub(r"```(?:json)?", "", text, flags=re.IGNORECASE)
    m = re.search(r"[\[{]", cleaned)
    if not m:
        return None
    try:
        obj, _ = json.JSONDecoder().raw_decode(cleaned[m.start() :])
        return obj
    except ValueError:
        return None
