import asyncio

import httpx

from app.config import settings
from app.services import llm_service


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
