"""All Hindsight access lives here (retain / recall / bank setup).

The official `hindsight-client` SDK is called through `_call`, which only passes
keyword arguments the installed SDK version actually accepts, so minor signature
differences between SDK releases don't break the app.
"""
import asyncio
import inspect
import logging
from datetime import datetime, timezone
from typing import Any, Optional

from app.config import settings
from app.errors import AppError

log = logging.getLogger("dealpilot.hindsight")

_client = None
_bank_ready = False

BANK_MISSION = (
    "Sales intelligence memory for a B2B salesperson. Track each prospect's "
    "objections, stakeholders and their concerns, pricing discussions, competitors, "
    "requirements, commitments, unresolved issues and decisions across meetings."
)


def is_configured() -> bool:
    return bool(settings.hindsight_api_key)


def require_configured() -> None:
    if not is_configured():
        raise AppError(
            "hindsight_not_configured",
            "Hindsight API key is missing. Set HINDSIGHT_API_KEY in .env and restart the server.",
            503,
        )


def _call(fn, **kwargs):
    """Call an SDK function, dropping kwargs it doesn't accept."""
    try:
        params = inspect.signature(fn).parameters
        if not any(p.kind is p.VAR_KEYWORD for p in params.values()):
            kwargs = {k: v for k, v in kwargs.items() if k in params}
    except (TypeError, ValueError):
        pass
    return fn(**kwargs)


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


def _ensure_bank(client) -> None:
    """Create the dedicated memory bank once per process (idempotent upsert)."""
    global _bank_ready
    if _bank_ready:
        return
    create = getattr(client, "create_bank", None)
    if create:
        try:
            _call(
                create,
                bank_id=settings.hindsight_bank_id,
                name="DealPilot",
                mission=BANK_MISSION,
                background=BANK_MISSION,  # older SDKs call it `background`
            )
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

async def retain(deal_name: str, content: str, document_id: Optional[str] = None) -> None:
    """Store one sales interaction in Hindsight."""
    try:
        client = _get_client()
        await asyncio.to_thread(_ensure_bank, client)
        await _call(
            client.aretain,
            bank_id=settings.hindsight_bank_id,
            content=content,
            context=f"Sales interaction for the deal with {deal_name}",
            document_id=document_id,
            timestamp=datetime.now(timezone.utc),
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
            }
        )
    return out


async def recall(query: str) -> list[dict]:
    """Recall memories relevant to `query` from the DealPilot bank."""
    try:
        client = _get_client()
        await asyncio.to_thread(_ensure_bank, client)
        resp = await _call(
            client.arecall,
            bank_id=settings.hindsight_bank_id,
            query=query,
            budget="mid",
        )
        return _normalize(resp)
    except Exception as exc:
        raise _translate(exc)


def _count_sync() -> Optional[int]:
    client = _get_client()
    fn = getattr(client, "list_memories", None)
    if not fn:
        return None
    resp = _call(fn, bank_id=settings.hindsight_bank_id, limit=1)
    total = _field(resp, "total")
    return int(total) if total is not None else None


async def memory_count() -> Optional[int]:
    """Total memories Hindsight holds in the bank (cosmetic; None if unavailable)."""
    if not is_configured():
        return None
    try:
        return await asyncio.to_thread(_count_sync)
    except Exception:
        return None
