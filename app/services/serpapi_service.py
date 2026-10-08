import logging
import re
from typing import Any

from app.config import settings
from app.errors import AppError

log = logging.getLogger("dealpilot.serpapi")


def is_configured() -> bool:
    return bool(settings.serpapi_api_key)


def require_key() -> None:
    if not is_configured():
        raise AppError(
            "serpapi_not_configured",
            "SerpApi API key is missing. Set SERPAPI_API_KEY in .env and restart the server.",
            503,
        )


def _normalize_query(query: str) -> str:
    return " ".join(query.split())


def _safe_value(item: dict, *keys: str, default: str = "") -> str:
    for key in keys:
        value = item.get(key)
        if value:
            return str(value)
    return default


def _extract_competitors(memories: list[dict]) -> list[str]:
    memory_text = " ".join((m.get("text") or "") for m in memories if m.get("text"))
    text = memory_text or ""
    if not text:
        return []

    matches = set()
    for token in re.findall(r"\b[A-Z][a-z0-9]+(?:[A-Z][a-z0-9]+)+\b", text):
        if token.lower() in {"acme", "corp", "company", "customer", "deal", "sales", "team", "cfo", "cto", "vp", "ceo", "chro", "founder"}:
            continue
        matches.add(token)

    for competitor in ["DataFlow", "Salesforce", "HubSpot", "Oracle", "SAP", "Snowflake", "Workday", "Gong", "ZoomInfo", "Chorus"]:
        if competitor.lower() in text.lower():
            matches.add(competitor)

    return [name for name in matches if len(name) > 2][:3]


def _build_context_reason(query: str, memories: list[dict]) -> str:
    memory_text = " ".join((m.get("text") or "") for m in memories if m.get("text")).lower()
    query_lower = (query or "").lower()
    if any(term in query_lower for term in ["security", "soc 2", "sso", "compliance", "privacy"]):
        return "This may matter because it speaks directly to the customer's security and compliance requirements."
    if any(term in query_lower for term in ["pricing", "roi", "budget", "cost", "payback", "value"]):
        return "This may matter because it affects pricing pressure, ROI proof, or budget approval for the deal."
    if any(term in query_lower for term in ["competitor", "comparison", "rival", "alternative"]):
        return "This may matter because it changes the competitive context and buying decision for the account."
    if any(term in query_lower for term in ["product", "launch", "roadmap", "feature", "innovation"]):
        return "This may matter because it signals product momentum or feature changes relevant to the opportunity."
    if any(term in query_lower for term in ["funding", "growth", "expansion", "acquisition", "merger", "leadership"]):
        return "This may matter because it signals business momentum or strategic shifts that could affect the account."
    if any(term in memory_text for term in ["security", "pricing", "roi", "budget", "competitor", "dataflow"]):
        return "This aligns with the deal's recent concern areas and could affect the next conversation."
    return "This is a current signal to validate against the account's latest priorities and objections."


def build_search_queries(company_name: str, memories: list[dict], question: str) -> list[str]:
    company = (company_name or "").strip()
    if not company:
        return []

    memory_text = " ".join((m.get("text") or "") for m in memories if m.get("text"))
    combined = f"{question} {memory_text}".lower()
    competitors = _extract_competitors(memories)
    queries: list[str] = []

    if any(term in combined for term in ["security", "soc 2", "sso", "compliance", "data privacy"]):
        queries.append(f"{company} security compliance SOC 2")
    if any(term in combined for term in ["pricing", "roi", "budget", "cost", "payback", "value"]):
        queries.append(f"{company} pricing ROI payback")
    if any(term in combined for term in ["competitor", "comparison", "dataflow", "alternative", "rival"]):
        competitor = competitors[0] if competitors else "competitors"
        queries.append(f"{company} {competitor} comparison")
        queries.append(f"{company} {competitor} product launch")
    if any(term in combined for term in ["product", "launch", "feature", "roadmap", "innovation"]):
        queries.append(f"{company} product launch roadmap")
    if any(term in combined for term in ["funding", "growth", "expansion", "leadership", "acquisition", "merger"]):
        queries.append(f"{company} funding growth leadership")

    for competitor in competitors[:2]:
        queries.insert(0, f"{company} {competitor} latest news")
        queries.insert(0, f"{company} {competitor} security compliance")

    if not competitors:
        queries.append(f"{company} competitors comparison")

    queries.extend([
        f"{company} latest news",
        f"{company} business updates",
    ])

    unique: list[str] = []
    seen: set[str] = set()
    for q in queries:
        qn = _normalize_query(q)
        if qn.lower() not in seen:
            seen.add(qn.lower())
            unique.append(qn)
    return unique[:6]


def search_company_intelligence(company_name: str, memories: list[dict], question: str) -> dict[str, Any]:
    if not is_configured():
        raise AppError(
            "serpapi_not_configured",
            "SerpApi is not configured. Set SERPAPI_API_KEY to enable live company intelligence.",
            503,
        )

    queries = build_search_queries(company_name, memories, question)
    if not queries:
        return {"queries": [], "results": []}

    try:
        from serpapi import GoogleSearch
    except ImportError as exc:  # pragma: no cover - import error should fail fast in deployment
        raise AppError(
            "serpapi_sdk_missing",
            "The SerpApi Python package is not installed. Run: pip install -r requirements.txt",
            500,
        ) from exc

    all_results: list[dict[str, str]] = []
    seen_links: set[str] = set()

    for query in queries:
        params = {
            "engine": "google",
            "q": query,
            "tbm": "nws",
            "num": 5,
            "api_key": settings.serpapi_api_key,
            "hl": "en",
            "gl": "us",
        }
        try:
            data = GoogleSearch(params).get_dict()
        except Exception as exc:  # pragma: no cover - network/path failures handled by caller
            log.warning("SerpApi query failed for %s: %s", query, exc)
            continue

        for result in data.get("news_results") or data.get("organic_results") or []:
            title = _safe_value(result, "title")
            if not title:
                continue
            link = _safe_value(result, "link", "source", default="")
            if link and link in seen_links:
                continue
            seen_links.add(link)
            summary = _safe_value(result, "snippet", "snippet_highlighted_words", default="")
            all_results.append(
                {
                    "title": title,
                    "snippet": summary,
                    "source": _safe_value(result, "source", "displayed_link", default="Web"),
                    "link": link,
                    "why_it_matters": _build_context_reason(query, memories),
                }
            )
            if len(all_results) >= 6:
                break
        if len(all_results) >= 6:
            break

    return {"queries": queries, "results": all_results[:6]}
