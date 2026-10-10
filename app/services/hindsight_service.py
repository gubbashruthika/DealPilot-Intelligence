"""All Hindsight access lives here (retain / recall / bank setup).

The official `hindsight-client` SDK is called through `_call`, which only passes
keyword arguments the installed SDK version actually accepts, so minor signature
differences between SDK releases don't break the app.
"""
import asyncio
import inspect
import logging
import re
from datetime import datetime, timezone
from typing import Any, Optional

from app.config import settings
from app.errors import AppError

log = logging.getLogger("dealpilot.hindsight")

_client = None
_bank_ready = False
_OFFLINE_MEMORIES: dict[str, list[dict]] = {}

BANK_MISSION = (
    "Sales intelligence memory for a B2B salesperson. Track each prospect's "
    "objections, stakeholders and their concerns, pricing discussions, competitors, "
    "requirements, commitments, unresolved issues and decisions across meetings."
)


def is_configured() -> bool:
    return bool(settings.hindsight_api_key)


def require_configured() -> None:
    if not is_configured():
        log.info("Hindsight not configured; continuing in offline demo mode.")


def _call(fn, **kwargs):
    """Call an SDK function, dropping kwargs it doesn't accept."""
    try:
        params = inspect.signature(fn).parameters
        if not any(p.kind is p.VAR_KEYWORD for p in params.values()):
            kwargs = {k: v for k, v in kwargs.items() if k in params}
    except (TypeError, ValueError):
        pass
    return fn(**kwargs)


def _supports_parameter(fn, name: str) -> bool:
    try:
        parameters = inspect.signature(fn).parameters
    except (TypeError, ValueError):
        return False
    return name in parameters or any(
        parameter.kind is parameter.VAR_KEYWORD for parameter in parameters.values()
    )


def _deal_tag(deal_id: str) -> str:
    return f"dealpilot_deal_{deal_id}"


def _field(obj: Any, name: str, default=None):
    if isinstance(obj, dict):
        return obj.get(name, default)
    return getattr(obj, name, default)


def _get_client():
    global _client
    require_configured()
    if _client is None:
        try:
            from hindsight_client import Hindsight
        except ImportError:
            raise AppError(
                "hindsight_sdk_missing",
                "The hindsight-client package is not installed. Run: pip install -r requirements.txt",
                500,
            )
        _client = _call(
            Hindsight, base_url=settings.hindsight_api_url, api_key=settings.hindsight_api_key
        )
    return _client


async def _ensure_bank(client) -> None:
    """Create the dedicated memory bank once per process (idempotent upsert)."""
    global _bank_ready
    if _bank_ready:
        return
    create = getattr(client, "acreate_bank", None)
    bank_args = {
        "bank_id": settings.hindsight_bank_id,
        "name": "DealPilot",
        "mission": BANK_MISSION,
        "background": BANK_MISSION,
    }
    if create:
        try:
            await _call(create, **bank_args)
        except Exception as exc:  # bank may already exist; retain/recall will surface real problems
            log.info("create_bank skipped: %s", type(exc).__name__)
    else:
        create_sync = getattr(client, "create_bank", None)
        if create_sync:
            try:
                def create_with_isolated_client():
                    from hindsight_client import Hindsight

                    sync_client = _call(
                        Hindsight,
                        base_url=settings.hindsight_api_url,
                        api_key=settings.hindsight_api_key,
                    )
                    _call(sync_client.create_bank, **bank_args)

                await asyncio.to_thread(create_with_isolated_client)
            except Exception as exc:  # bank may already exist; retain/recall will surface real problems
                log.info("create_bank skipped: %s", type(exc).__name__)
    _bank_ready = True


def _translate(exc: Exception) -> AppError:
    if isinstance(exc, AppError):
        return exc
    text = str(exc).lower()
    log.warning("Hindsight call failed: %s: %s", type(exc).__name__, str(exc))
    if "401" in text or "403" in text or "unauthor" in text or "forbidden" in text:
        return AppError("hindsight_auth", "Hindsight rejected the API key. Check HINDSIGHT_API_KEY.", 502)
    return AppError(
        "hindsight_unavailable",
        "Could not reach Hindsight. Check HINDSIGHT_API_URL and your connection, then try again.",
        502,
    )


# ---------------------------------------------------------------- RETAIN

async def retain(deal_id: str, deal_name: str, content: str, document_id: Optional[str] = None) -> None:
    """Store one sales interaction in Hindsight or in an in-memory fallback when keys are absent."""
    if not is_configured():
        key = deal_id
        _OFFLINE_MEMORIES.setdefault(key, [])
        _OFFLINE_MEMORIES[key].append(
            {
                "id": document_id or f"offline-{len(_OFFLINE_MEMORIES[key])}",
                "text": content,
                "type": "offline",
                "context": f"Sales interaction for the deal with {deal_name}",
                "date": datetime.now(timezone.utc).isoformat(),
            }
        )
        return
    try:
        client = _get_client()
        if not _supports_parameter(client.aretain, "tags"):
            raise AppError(
                "hindsight_scoping_unsupported",
                "The installed Hindsight client cannot tag memories by customer, so saving was stopped to prevent cross-customer recall.",
                503,
            )
        await _ensure_bank(client)
        await _call(
            client.aretain,
            bank_id=settings.hindsight_bank_id,
            content=content,
            context=f"Sales interaction for the deal with {deal_name}",
            document_id=document_id,
            timestamp=datetime.now(timezone.utc),
            tags=[_deal_tag(deal_id)],
        )
    except Exception as exc:
        raise _translate(exc)


# ---------------------------------------------------------------- RECALL

def _normalize(resp: Any) -> list[dict]:
    results = _field(resp, "results")
    if results is None and isinstance(resp, (list, tuple)):
        results = resp
    out = []
    for r in results or []:
        text = _field(r, "text") or _field(r, "content") or ""
        if not text:
            continue
        date = _field(r, "occurred_start") or _field(r, "mentioned_at")
        out.append(
            {
                "id": str(_field(r, "id", "") or ""),
                "text": text,
                "type": _field(r, "type") or _field(r, "fact_type"),
                "context": _field(r, "context"),
                "date": str(date) if date else None,
                "source": "hindsight",
            }
        )
    return out


async def recall(query: str, deal_id: str) -> list[dict]:
    """Recall memories relevant to `query` from the DealPilot bank or the offline demo store."""
    if not is_configured():
        raw = (query or "").lower()
        tokens = {t for t in re.findall(r"[a-z0-9]+", raw) if len(t) > 2}
        out: list[dict] = []
        for item in _OFFLINE_MEMORIES.get(deal_id, []):
            text = (item.get("text") or "") + " " + (item.get("context") or "")
            haystack = text.lower()
            if not tokens or any(token in haystack for token in tokens):
                out.append({
                    "id": str(item.get("id") or len(out)),
                    "text": item.get("text") or "",
                    "type": item.get("type") or "offline",
                    "context": item.get("context"),
                    "date": item.get("date"),
                    "source": "offline",
                })
        return out[:25]
    try:
        client = _get_client()
        if not _supports_parameter(client.arecall, "tags") or not _supports_parameter(client.arecall, "tags_match"):
            raise AppError(
                "hindsight_scoping_unsupported",
                "The installed Hindsight client cannot filter memories by customer, so recall was stopped to prevent cross-customer results.",
                503,
            )
        await _ensure_bank(client)
        resp = await _call(
            client.arecall,
            bank_id=settings.hindsight_bank_id,
            query=query,
            budget="mid",
            tags=[_deal_tag(deal_id)],
            tags_match="exact",
        )
        results = _field(resp, "results")
        if results is None and isinstance(resp, (list, tuple)):
            results = resp
        scoped_tag = _deal_tag(deal_id)
        scoped_results = [
            result
            for result in results or []
            if isinstance(_field(result, "tags"), list)
            and set(_field(result, "tags")) == {scoped_tag}
        ]
        return _normalize(scoped_results)
    except Exception as exc:
        raise _translate(exc)


def _count_sync() -> Optional[int]:
    from hindsight_client import Hindsight

    client = _call(
        Hindsight,
        base_url=settings.hindsight_api_url,
        api_key=settings.hindsight_api_key,
    )
    fn = getattr(client, "list_memories", None)
    if not fn:
        return None
    resp = _call(fn, bank_id=settings.hindsight_bank_id, limit=1)
    total = _field(resp, "total")
    return int(total) if total is not None else None


async def memory_count() -> Optional[int]:
    """Total memories Hindsight holds in the bank or the fallback demo store."""
    if not is_configured():
        total = sum(len(v) for v in _OFFLINE_MEMORIES.values())
        return total or 0
    try:
        client = _get_client()
        await _ensure_bank(client)
        fn = getattr(client, "alist_memories", None)
        if fn:
            resp = await _call(fn, bank_id=settings.hindsight_bank_id, limit=1)
            total = _field(resp, "total")
            return int(total) if total is not None else None
        return await asyncio.to_thread(_count_sync)
    except Exception:
        return None
