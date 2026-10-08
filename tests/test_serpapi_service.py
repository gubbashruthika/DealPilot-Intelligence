import asyncio

import httpx

from app.config import settings
from app.services import deal_agent, llm_service
from app.services.serpapi_service import build_search_queries


def test_build_search_queries_prioritizes_customer_risks():
    memories = [
        {"text": "Acme's security team requires SSO and SOC 2 compliance."},
        {"text": "The CFO is comparing us with DataFlow and wants pricing clarity."},
    ]

    queries = build_search_queries("Acme Corp", memories, "Prepare me for my next Acme meeting.")

    assert any("security" in query.lower() or "soc 2" in query.lower() for query in queries)
    assert any("competitor" in query.lower() or "dataflow" in query.lower() for query in queries)
    assert len(queries) >= 2


def test_complete_retries_after_groq_rate_limit(monkeypatch):
    object.__setattr__(settings, "groq_api_key", "test-key")
    object.__setattr__(settings, "groq_model", "llama-3.1-8b-instant")

    responses = iter([
        type("Resp", (), {"status_code": 429, "json": lambda self: {}})(),
        type(
            "Resp",
            (),
            {"status_code": 200, "json": lambda self: {"choices": [{"message": {"content": "Recovered brief"}}]}},
        )(),
    ])

    class FakeClient:
        def __init__(self, *args, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb):
            return False

        async def post(self, *args, **kwargs):
            return next(responses)

    async def fake_sleep(delay):
        return None

    monkeypatch.setattr(httpx, "AsyncClient", FakeClient)
    monkeypatch.setattr(llm_service.asyncio, "sleep", fake_sleep)

    result = asyncio.run(llm_service.complete([{"role": "user", "content": "brief me"}], json_mode=False))

    assert result == "Recovered brief"


def test_build_search_queries_uses_competitor_context_from_memories():
    memories = [
        {"text": "The CFO is comparing us with DataFlow and wants pricing clarity."},
        {"text": "Security team requires SSO and SOC 2 compliance."},
    ]

    queries = build_search_queries("Acme Corp", memories, "Prepare me for my next Acme meeting.")

    assert any("DataFlow" in query for query in queries)
    assert any("security" in query.lower() for query in queries)
    assert any("pricing" in query.lower() for query in queries)


def test_deal_health_and_change_summary_are_explained():
    memories = [{"text": "Acme's security team requires SSO and SOC 2 and the CFO worries about pricing and ROI."}]
    live = [{"title": "Acme competitor launches new enterprise security feature", "snippet": "The market competitor is pushing security capability."}]

    health = deal_agent._deal_health(memories, live)
    evolution = deal_agent._historical_vs_current(memories, live)

    assert health["score"] is not None
    assert any(f["label"] == "Security requirement" for f in health["factors"])
    assert evolution["before"]
    assert evolution["now"]
    assert evolution["impact"]
