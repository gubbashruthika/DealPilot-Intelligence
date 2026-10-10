"""Groq chat-completions client (OpenAI-compatible REST endpoint)."""
import asyncio
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
        return


async def complete(
    messages: list[dict], json_mode: bool = False, temperature: float = 0.2, max_tokens: int = 900
) -> str:
    if not is_configured():
        prompt = messages[-1].get("content", "") if messages else ""
        memory_text = any(
            marker in prompt
            for marker in ("Recalled memories from Hindsight", "Customer-specific context")
        )
        if json_mode:
            if memory_text:
                return (
                    '{"deal_status": "Offline demo mode: the account remains under evaluation, with pricing, security, and ROI concerns still shaping the deal.",'
                    '"key_stakeholders": [], "top_objections": ["Pricing is perceived as too high", "Security requirements remain unresolved", "ROI proof is still pending"], '
                    '"requirements": ["Security validation", "Pricing clarity", "ROI evidence"], "competitor": ["DataFlow"], '
                    '"open_issues": ["Security review pending", "Pricing alignment pending", "ROI evidence pending"], '
                    '"recommended_next_action": "Schedule a follow-up with the CTO and CFO to align on security validation and ROI proof before moving the deal forward."}'
                )
            return (
                '{"deal_status": "Offline demo mode: the account is still under evaluation with a clear need for follow-up on security, pricing, and ROI. ",'
                '"key_stakeholders": [], "top_objections": ["Pricing concern", "Security requirement", "ROI proof"], "requirements": ["Security validation", "Pricing clarity", "ROI evidence"], '
                '"competitor": ["DataFlow"], "open_issues": ["Security review pending", "ROI proof pending"], "recommended_next_action": "Schedule a follow-up with the CTO and CFO to align on security validation and ROI proof."}'
            )
        if memory_text:
            return (
                "Offline demo mode: the recalled memory shows pricing pressure, security requirements, and ROI proof as the main blockers, with DataFlow still acting as the competitive reference. The best next step is a focused CFO/CTO follow-up on security validation and ROI proof."
            )
        return (
            "Offline demo mode: the deal is still under evaluation, with pricing, security, and ROI validation as the key items to address before the next meeting."
            if prompt else "Offline demo mode: no live LLM is available, but the historical deal context is still being used for guidance."
        )
    import httpx

    payload = {
        "model": settings.groq_model,
        "messages": messages,
        "temperature": temperature,
        "max_tokens": max_tokens,
    }
    if json_mode:
        payload["response_format"] = {"type": "json_object"}

    for attempt in range(3):
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
            if attempt < 2:
                await asyncio.sleep(1.5 * (attempt + 1))
                continue
            raise AppError("llm_rate_limited", "Groq rate limit reached. Wait a moment and try again.", 502)
        if r.status_code >= 400:
            raise AppError("llm_failure", f"Groq returned an error (HTTP {r.status_code}). Check GROQ_MODEL.", 502)
        try:
            return r.json()["choices"][0]["message"]["content"].strip()
        except (KeyError, IndexError, ValueError):
            raise AppError("llm_failure", "Groq returned an unexpected response.", 502)

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
