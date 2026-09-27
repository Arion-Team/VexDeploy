"""Gemini AI client — stdlib only (urllib), no extra requirements."""

from __future__ import annotations

import asyncio
import json
import logging
import re
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


def _generate(
    model: str, prompt: str, system: str | None, timeout: int
) -> str:
    """Single blocking generateContent call. Raises AIError on any failure."""
    if not GEMINI_API_KEY:
        raise AIError("GEMINI_API_KEY is not set")
    url = f"{API_BASE}/{model}:generateContent?key={GEMINI_API_KEY}"
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
        detail = exc.read().decode("utf-8", errors="replace")[:400]
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


def _call(
    prompt: str,
    system: str | None = None,
    timeout: int = DEFAULT_TIMEOUT,
    _retry: bool = True,
) -> str:
    try:
        return _generate(GEMINI_MODEL, prompt, system, timeout)
    except AIError as exc:
        # Retired model (e.g. gemini-2.5-flash → gemini-3.8-flash):
        # follow Google's suggestion once, then give up.
        if _retry:
            m = re.search(r"use models/([A-Za-z0-9._-]+)", str(exc))
            if m and m.group(1) != GEMINI_MODEL:
                log.warning(
                    "model %s unavailable — retrying with %s",
                    GEMINI_MODEL,
                    m.group(1),
                )
                return _generate(m.group(1), prompt, system, timeout)
        raise


async def chat(prompt: str, system: str | None = None, timeout: int = DEFAULT_TIMEOUT) -> str:
    """Async wrapper around the blocking Gemini call."""
    return await asyncio.to_thread(_call, prompt, system, timeout)


def extract_json(text: str):
    """Pull the first JSON object/array out of a model reply (tolerates fences/prose)."""
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
