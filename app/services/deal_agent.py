"""Orchestrates retain -> recall -> generate. Hindsight is the memory; this file holds no memory logic.

The only local state is a small interaction log (data/interactions.json) used to draw
the timeline and interaction count in the UI. It is never used to answer questions.
"""
import asyncio
import json
import re
import threading
import uuid
from datetime import datetime, timezone

from app.config import settings
from app.errors import AppError
from app.services import hindsight_service as hs
from app.services import llm_service as llm

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
    hs.require_configured()
    record_id = uuid.uuid4().hex[:8]
    await hs.retain(deal["name"], content, document_id=f"{deal_id}-{record_id}")
    return _append(deal_id, content, source)  # logged only after Hindsight accepted it


async def load_demo(deal_id: str) -> dict:
    get_deal(deal_id)
    if any(i["source"] == "demo" for i in timeline(deal_id)):
        raise AppError("demo_already_loaded", "The Acme demo is already loaded.", 409)
    hs.require_configured()
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


# ----------------------------------------------------------------- GENERATE
def _as_list(value) -> list:
    if value in (None, "", "Not in memory"):
        return []
    return value if isinstance(value, list) else [value]


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
    llm.require_key()
    hs.require_configured()

    want_brief = bool(_BRIEF_RE.search(question)) if brief is None else brief
    wide = want_brief or bool(_WIDE_RE.search(question))
    memories = await _recall_many(_queries(deal["name"], question, wide))

    result = {"question": question, "used_memory": True, "memories": memories,
              "memory_count": len(memories), "brief": None, "empty": False}
    if not memories:
        return {**result, "answer": EMPTY_RECALL_MSG, "empty": True}

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
            result["answer"] = raw  # model ignored JSON; still show something useful
    else:
        result["answer"] = await llm.complete(
            [{"role": "system", "content": CHAT_SYSTEM}, {"role": "user", "content": user_msg}])
    return result


async def ask_without_memory(deal_id: str, question: str) -> dict:
    get_deal(deal_id)
    question = (question or "").strip()
    if not question:
        raise AppError("empty_question", "Type a question for DealPilot first.", 400)
    answer = await llm.complete(
        [{"role": "system", "content": NO_MEMORY_SYSTEM}, {"role": "user", "content": question}])
    return {"question": question, "answer": answer, "used_memory": False, "memories": [], "memory_count": 0}


async def list_memories(deal_id: str) -> dict:
    deal = get_deal(deal_id)
    hs.require_configured()
    memories = await _recall_many(_queries(deal["name"], "everything we know about this deal", True))
    return {"memories": memories, "recalled": len(memories), "memory_count": await hs.memory_count()}
