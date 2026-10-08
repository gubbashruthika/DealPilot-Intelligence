"""Orchestrates retain -> recall -> generate. Hindsight is the memory; this file holds no memory logic.

The only local state is a small interaction log (data/interactions.json) used to draw
the timeline and interaction count in the UI. It is never used to answer questions.
"""
import asyncio
import json
import logging
import re
import threading
import uuid
from datetime import datetime, timezone

from app.config import settings
from app.errors import AppError
from app.services import hindsight_service as hs
from app.services import llm_service as llm
from app.services import serpapi_service

log = logging.getLogger("dealpilot.agent")

DEALS = {"acme-corp": {"id": "acme-corp", "name": "Acme Corp"}}

DEMO_INTERACTIONS = [
    "Acme Corp says our pricing is too high. Their CTO likes the product, but the CFO is "
    "concerned about ROI. They are comparing us with DataFlow.",
    "Acme's security team requires SSO and SOC 2. The CTO said they could move forward if "
    "security requirements are satisfied.",
    "The CFO asked for proof of ROI and wants to understand the expected payback period.",
]

PREP_QUESTION = "Tell me about Acme Corp and prepare me for the next meeting."
MAX_LEN = 10_000
EMPTY_RECALL_MSG = (
    "Hindsight has no memories for this yet. Add an interaction or load the Acme demo first. "
    "Newly retained notes can take a few seconds to become searchable."
)

_lock = threading.Lock()
_BRIEF_RE = re.compile(r"\b(prepare|prep|brief|next meeting|get ready)\b", re.I)
_WIDE_RE = re.compile(r"\b(happen|previous|history|timeline|summar|status|overview|focus|next)\b", re.I)

BRIEF_KEYS = [
    "deal_status", "key_stakeholders", "top_objections",
    "requirements", "competitor", "open_issues", "recommended_next_action",
]

BRIEF_SYSTEM = (
    "You are DealPilot, a sales intelligence agent. Build a concise meeting brief using ONLY the "
    "recalled memories provided. Never invent facts. Respond with a single JSON object with keys: "
    'deal_status (string, 1-2 sentences), key_stakeholders (array of {"role": string, "stance": string}), '
    "top_objections (array of strings), requirements (array of strings), competitor (array of strings), "
    "open_issues (array of strings), recommended_next_action (string). "
    "Use an empty array when memories say nothing about a field."
)
CHAT_SYSTEM = (
    "You are DealPilot, a sales intelligence agent. Answer using ONLY the recalled memories provided. "
    "Be concise (under 150 words), use short bullets where helpful, and say so plainly if the memories "
    "do not answer the question. Never invent facts."
)
NO_MEMORY_SYSTEM = (
    "You are DealPilot, an AI sales assistant. In this mode you have NO access to stored notes, CRM "
    "data or prior conversations for this account. Answer as well as you can; if you lack specifics, "
    "say so plainly instead of inventing them. Keep it under 150 words."
)
DEAL_INTEL_SYSTEM = (
    "You are DealPilot's deal intelligence engine. Combine the historical memory and current live intelligence "
    "provided by the user. Clearly separate what is memory-derived from what is live, and never invent facts. "
    "Return a single JSON object with keys: customer, deal_stage, past_context, live_intelligence, "
    "what_changed, deal_risks, opportunities, next_best_action, why_this_action, sources. "
    "For deal_risks, use an array of objects with level and reason. For opportunities, use objects with title and why. "
    "For live_intelligence, use objects with signal, source, detail, and why_it_matters. Keep it concise and factual."
)


# ------------------------------------------------------------ local UI timeline
def _path():
    return settings.data_dir / "interactions.json"


def _load() -> dict:
    try:
        return json.loads(_path().read_text())
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def _append(deal_id: str, content: str, source: str) -> dict:
    record = {
        "id": uuid.uuid4().hex[:12],
        "content": content,
        "source": source,
        "created_at": datetime.now(timezone.utc).isoformat(),
    }
    with _lock:
        data = _load()
        data.setdefault(deal_id, []).append(record)
        _path().parent.mkdir(parents=True, exist_ok=True)
        _path().write_text(json.dumps(data, indent=2))
    return record


def timeline(deal_id: str) -> list[dict]:
    with _lock:
        return _load().get(deal_id, [])


def get_deal(deal_id: str) -> dict:
    deal = DEALS.get(deal_id)
    if not deal:
        raise AppError("deal_not_found", f"Unknown deal '{deal_id}'.", 404)
    return deal


def list_deals() -> list[dict]:
    out = []
    for deal in DEALS.values():
        items = timeline(deal["id"])
        out.append({**deal, "interaction_count": len(items),
                    "last_interaction_at": items[-1]["created_at"] if items else None,
                    "demo_loaded": any(i["source"] == "demo" for i in items)})
    return out


async def deal_detail(deal_id: str) -> dict:
    deal = get_deal(deal_id)
    items = timeline(deal_id)
    return {**deal, "interaction_count": len(items), "interactions": items,
            "demo_loaded": any(i["source"] == "demo" for i in items),
            "memory_count": await hs.memory_count()}


# ------------------------------------------------------------------- RETAIN
async def add_interaction(deal_id: str, content: str, source: str = "user") -> dict:
    deal = get_deal(deal_id)
    content = (content or "").strip()
    if not content:
        raise AppError("empty_interaction", "Paste some meeting notes or conversation first.", 400)
    if len(content) > MAX_LEN:
        raise AppError("interaction_too_long", f"Keep interactions under {MAX_LEN:,} characters.", 400)
    record_id = uuid.uuid4().hex[:8]
    await hs.retain(deal["name"], content, document_id=f"{deal_id}-{record_id}")
    return _append(deal_id, content, source)


async def load_demo(deal_id: str) -> dict:
    get_deal(deal_id)
    if any(i["source"] == "demo" for i in timeline(deal_id)):
        raise AppError("demo_already_loaded", "The Acme demo is already loaded.", 409)
    for text in DEMO_INTERACTIONS:
        await add_interaction(deal_id, text, source="demo")
    return {"loaded": len(DEMO_INTERACTIONS)}


# ------------------------------------------------------------------- RECALL
def _queries(name: str, question: str, wide: bool) -> list[str]:
    if not wide:
        return [f"{name}: {question}"]
    return [
        f"{name} pricing objections, concerns and competitors",
        f"{name} stakeholders, their roles and stance",
        f"{name} requirements, commitments, decisions and unresolved issues",
        f"{name}: {question}",
    ]


async def _recall_many(queries: list[str]) -> list[dict]:
    batches = await asyncio.gather(*(hs.recall(q) for q in queries))
    seen, merged = set(), []
    for batch in batches:
        for m in batch:
            key = m["id"] or m["text"]
            if key not in seen:
                seen.add(key)
                merged.append(m)
    return merged[:25]


def _format_memories(memories: list[dict]) -> str:
    lines = []
    for i, m in enumerate(memories, 1):
        tag = f" ({m['type']})" if m.get("type") else ""
        ctx = f" [context: {m['context']}]" if m.get("context") else ""
        lines.append(f"{i}.{tag} {m['text']}{ctx}")
    return "\n".join(lines)


async def _fetch_live_intelligence(company_name: str, memories: list[dict], question: str) -> dict:
    if not serpapi_service.is_configured():
        return {
            "status": "serpapi_missing",
            "message": "Live intelligence is temporarily unavailable. Set SERPAPI_API_KEY to enable current company research.",
            "intelligence": [],
        }
    try:
        data = await asyncio.to_thread(serpapi_service.search_company_intelligence, company_name, memories, question)
        results = data.get("results") or []
        if not results:
            return {
                "status": "no_results",
                "message": "No reliable current signals were found for this account right now.",
                "intelligence": [],
            }
        return {
            "status": "ok",
            "message": "Live intelligence retrieved from current company and market signals.",
            "intelligence": results,
        }
    except AppError as exc:
        return {"status": "degraded", "message": exc.message, "intelligence": []}
    except Exception as exc:  # pragma: no cover - defensive
        log.warning("Live intelligence failed for %s: %s", company_name, exc)
        return {
            "status": "degraded",
            "message": "Live intelligence is temporarily unavailable. Showing historical deal context.",
            "intelligence": [],
        }


async def _build_deal_intelligence(company_name: str, memories: list[dict], live_intelligence: list[dict], question: str) -> dict:
    past_context = [m.get("text", "") for m in memories[:6] if m.get("text")]
    live_context = []
    for signal in live_intelligence[:4]:
        live_context.append(
            {
                "signal": signal.get("title") or "Current signal",
                "source": signal.get("source") or "Web source",
                "detail": signal.get("snippet") or "",
                "why_it_matters": signal.get("why_it_matters") or "This current signal may affect the deal because it overlaps with the account's recent concern areas.",
                "link": signal.get("link") or "",
            }
        )

    if not llm.is_configured():
        deal_health = _deal_health(memories, live_intelligence)
        evolution = _historical_vs_current(memories, live_intelligence)
        return {
            "customer": company_name,
            "deal_stage": "Negotiation",
            "past_context": past_context,
            "live_intelligence": live_context,
            "what_changed": ["Historical context is available; no live intelligence was available for the final recommendation."] if live_context else ["No live intelligence was available for this deal."],
            "deal_risks": [{"level": "Medium", "reason": "The deal still needs current market validation before the next step."}] if not live_context else [{"level": "Medium", "reason": "Current information should be reviewed before finalizing the next sales action."}],
            "opportunities": [{"title": "Keep the deal moving", "why": "The account history still gives a clear set of requirements to address."}] if past_context else [],
            "next_best_action": "Review the deal history and current account signals before the next meeting.",
            "why_this_action": "The recommendation is based on the sales history and any available live search updates.",
            "sources": [item.get("link") for item in live_intelligence if item.get("link")][:5],
            "deal_health": deal_health,
            "historical_vs_current": evolution,
        }

    prompt = {
        "question": question,
        "company": company_name,
        "past_context": past_context,
        "live_intelligence": live_context,
    }
    raw = await llm.complete(
        [{
            "role": "system",
            "content": DEAL_INTEL_SYSTEM,
        }, {
            "role": "user",
            "content": json.dumps(prompt, ensure_ascii=False),
        }],
        json_mode=True,
        max_tokens=1200,
    )
    parsed = llm.parse_json(raw)
    if isinstance(parsed, dict):
        if not parsed.get("customer"):
            parsed["customer"] = company_name
        if not parsed.get("deal_stage"):
            parsed["deal_stage"] = "Negotiation"
        if not parsed.get("past_context"):
            parsed["past_context"] = past_context
        if not parsed.get("live_intelligence"):
            parsed["live_intelligence"] = live_context
        if not parsed.get("sources"):
            parsed["sources"] = [item.get("link") for item in live_intelligence if item.get("link")][:5]
        parsed["deal_health"] = parsed.get("deal_health") or _deal_health(memories, live_intelligence)
        parsed["historical_vs_current"] = parsed.get("historical_vs_current") or _historical_vs_current(memories, live_intelligence)
        return parsed

    fallback = {
        "customer": company_name,
        "deal_stage": "Negotiation",
        "past_context": past_context,
        "live_intelligence": live_context,
        "what_changed": ["A live intelligence review was requested but the model did not return a structured summary."],
        "deal_risks": [{"level": "Medium", "reason": "Current market signals still need a human review."}],
        "opportunities": [{"title": "Review current signals", "why": "Use the active account context to validate the next action."}],
        "next_best_action": "Validate the most relevant current signal with the account team before the next meeting.",
        "why_this_action": "The current information is incomplete, so the next step is to confirm the latest development with the customer and internal stakeholders.",
        "sources": [item.get("link") for item in live_intelligence if item.get("link")][:5],
        "deal_health": _deal_health(memories, live_intelligence),
        "historical_vs_current": _historical_vs_current(memories, live_intelligence),
    }
    return fallback


# ----------------------------------------------------------------- GENERATE
def _as_list(value) -> list:
    if value in (None, "", "Not in memory"):
        return []
    return value if isinstance(value, list) else [value]


def _deal_health(memories: list[dict], live_intelligence: list[dict]) -> dict:
    evidence = " ".join(
        (m.get("text") or "") + " " + (m.get("context") or "")
        for m in memories
    ).lower()
    evidence += " " + " ".join(
        (item.get("title") or "") + " " + (item.get("snippet") or "")
        for item in live_intelligence
    ).lower()

    if not evidence.strip():
        return {"score": None, "status": "Insufficient evidence", "factors": []}

    factors = []
    score = 70

    if any(term in evidence for term in ["pricing", "budget", "roi", "payback", "cost"]):
        score -= 15
        factors.append({"label": "Pricing concern", "level": "MEDIUM", "direction": "negative"})
    if any(term in evidence for term in ["security", "sso", "soc 2", "compliance", "privacy"]):
        score -= 18
        factors.append({"label": "Security requirement", "level": "HIGH", "direction": "negative"})
    if any(term in evidence for term in ["competitor", "comparison", "alternative", "dataflow", "rival"]):
        score -= 14
        factors.append({"label": "Competitive pressure", "level": "MEDIUM", "direction": "negative"})
    if any(term in evidence for term in ["expansion", "growth", "funding", "leadership", "acquisition"]):
        score += 10
        factors.append({"label": "Growth signal", "level": "POSITIVE", "direction": "positive"})
    if any(term in evidence for term in ["product", "launch", "feature", "roadmap", "innovation"]):
        score += 8
        factors.append({"label": "Product momentum", "level": "POSITIVE", "direction": "positive"})

    if not factors:
        return {"score": None, "status": "Insufficient evidence", "factors": []}

    score = max(0, min(100, score))
    if score >= 75:
        status = "Healthy"
    elif score >= 55:
        status = "Watchlist"
    elif score >= 35:
        status = "At Risk"
    else:
        status = "Critical"

    return {"score": score, "status": status, "factors": factors}


def _historical_vs_current(memories: list[dict], live_intelligence: list[dict]) -> dict:
    before = []
    now = []
    impact = []

    for memory in memories[:5]:
        text = (memory.get("text") or "").strip()
        if not text:
            continue
        if any(term in text.lower() for term in ["pricing", "budget", "roi", "cost", "security", "sso", "soc 2", "competitor", "dataflow"]):
            before.append(text)

    for item in live_intelligence[:4]:
        text = (item.get("title") or item.get("signal") or "").strip()
        if text:
            now.append(text)

    if before:
        impact.append("The historical concerns remain the same core factors shaping the deal.")
    if now:
        impact.append("Current signals add urgency by showing a changing market, competitor behavior, or new customer demand.")
    if not before and not now:
        impact = ["Insufficient evidence to determine a significant change."]

    return {"before": before[:4], "now": now[:4], "impact": impact[:3]}


def _normalize_brief(raw: dict) -> dict:
    brief = {k: raw.get(k) for k in BRIEF_KEYS}
    for k in BRIEF_KEYS:
        if k not in ("deal_status", "recommended_next_action"):
            brief[k] = _as_list(brief[k])
    for k in ("deal_status", "recommended_next_action"):
        brief[k] = str(brief[k] or "")
    return brief


async def ask(deal_id: str, question: str, brief: bool | None = None) -> dict:
    deal = get_deal(deal_id)
    question = (question or "").strip()
    if not question:
        raise AppError("empty_question", "Type a question for DealPilot first.", 400)
    want_brief = bool(_BRIEF_RE.search(question)) if brief is None else brief
    wide = want_brief or bool(_WIDE_RE.search(question))
    memories = await _recall_many(_queries(deal["name"], question, wide))
    live = await _fetch_live_intelligence(deal["name"], memories, question)

    result = {
        "question": question,
        "used_memory": True,
        "memories": memories,
        "memory_count": len(memories),
        "brief": None,
        "empty": False,
        "live_intelligence": live["intelligence"],
        "live_status": live["status"],
        "live_message": live["message"],
    }
    result["deal_intelligence"] = await _build_deal_intelligence(deal["name"], memories, live["intelligence"], question)
    result["deal_health"] = result["deal_intelligence"].get("deal_health", {"score": None, "status": "Insufficient evidence", "factors": []})
    result["historical_vs_current"] = result["deal_intelligence"].get("historical_vs_current", {"before": [], "now": [], "impact": ["Insufficient evidence to determine a significant change."]})

    if not memories:
        result["answer"] = EMPTY_RECALL_MSG
        result["empty"] = True
        return result

    user_msg = f"Recalled memories from Hindsight:\n{_format_memories(memories)}\n\nRequest: {question}"
    if want_brief:
        raw = await llm.complete(
            [{"role": "system", "content": BRIEF_SYSTEM}, {"role": "user", "content": user_msg}],
            json_mode=True, max_tokens=1000)
        parsed = llm.parse_json(raw)
        if parsed:
            result["brief"] = _normalize_brief(parsed)
            result["answer"] = result["brief"]["deal_status"] or "Your meeting brief is ready."
        else:
            result["answer"] = raw
    else:
        result["answer"] = await llm.complete(
            [{"role": "system", "content": CHAT_SYSTEM}, {"role": "user", "content": user_msg}])
    return result


async def ask_without_memory(deal_id: str, question: str) -> dict:
    deal = get_deal(deal_id)
    question = (question or "").strip()
    if not question:
        raise AppError("empty_question", "Type a question for DealPilot first.", 400)
    llm.require_key()
    live = await _fetch_live_intelligence(deal["name"], [], question)
    prompt = question
    if live["intelligence"]:
        prompt = (
            f"{question}\n\nCurrent company intelligence:\n"
            + "\n".join(f"- {item.get('title', '')}: {item.get('snippet', '')}" for item in live["intelligence"][:3])
        )
    answer = await llm.complete(
        [{"role": "system", "content": NO_MEMORY_SYSTEM}, {"role": "user", "content": prompt}])
    return {
        "question": question,
        "answer": answer,
        "used_memory": False,
        "memories": [],
        "memory_count": 0,
        "live_intelligence": live["intelligence"],
        "live_status": live["status"],
        "live_message": live["message"],
        "deal_intelligence": await _build_deal_intelligence(deal["name"], [], live["intelligence"], question),
    }


async def list_memories(deal_id: str) -> dict:
    deal = get_deal(deal_id)
    memories = await _recall_many(_queries(deal["name"], "everything we know about this deal", True))
    return {"memories": memories, "recalled": len(memories), "memory_count": await hs.memory_count()}
