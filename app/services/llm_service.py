"""Groq chat-completions client (OpenAI-compatible REST endpoint)."""
import json
import re
from typing import Optional

from app.config import settings
from app.errors import AppError

GROQ_URL = "https://api.groq.com/openai/v1/chat/completions"


def is_configured() -> bool:
    return bool(settings.groq_api_key)


def require_key() -> None:
    if not is_configured():
        raise AppError(
            "groq_not_configured",
            "Groq API key is missing. Set GROQ_API_KEY in .env and restart the server.",
            503,
        )


async def complete(
    messages: list[dict], json_mode: bool = False, temperature: float = 0.2, max_tokens: int = 900
) -> str:
    require_key()
    import httpx

    payload = {
        "model": settings.groq_model,
        "messages": messages,
        "temperature": temperature,
        "max_tokens": max_tokens,
    }
    if json_mode:
        payload["response_format"] = {"type": "json_object"}
    try:
        async with httpx.AsyncClient(timeout=45) as client:
            r = await client.post(
                GROQ_URL, headers={"Authorization": f"Bearer {settings.groq_api_key}"}, json=payload
            )
    except httpx.HTTPError:
        raise AppError("llm_unavailable", "Could not reach Groq. Check your connection and try again.", 502)

    if r.status_code in (401, 403):
        raise AppError("llm_auth", "Groq rejected the API key. Check GROQ_API_KEY.", 502)
    if r.status_code == 429:
        raise AppError("llm_rate_limited", "Groq rate limit reached. Wait a moment and try again.", 502)
    if r.status_code >= 400:
        raise AppError("llm_failure", f"Groq returned an error (HTTP {r.status_code}). Check GROQ_MODEL.", 502)
    try:
        return r.json()["choices"][0]["message"]["content"].strip()
    except (KeyError, IndexError, ValueError):
        raise AppError("llm_failure", "Groq returned an unexpected response.", 502)


def parse_json(text: str) -> Optional[dict]:
    """Parse a JSON object from model output, tolerating code fences and stray prose."""
    cleaned = re.sub(r"^```(?:json)?|```$", "", text.strip(), flags=re.M).strip()
    for candidate in (cleaned, cleaned[cleaned.find("{") : cleaned.rfind("}") + 1]):
        try:
            data = json.loads(candidate)
            if isinstance(data, dict):
                return data
        except (ValueError, TypeError):
            continue
    return None
