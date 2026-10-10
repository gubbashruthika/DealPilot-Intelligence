import asyncio
import json

import httpx
import pytest

from app.config import settings
from app.errors import AppError
from app.services import deal_agent, llm_service


def test_complete_uses_configured_bearer_key(monkeypatch):
    test_key = "dummy-groq-test-key"
    original_key = settings.groq_api_key
    original_model = settings.groq_model
    object.__setattr__(settings, "groq_api_key", test_key)
    object.__setattr__(settings, "groq_model", "test-model")
    received_headers = {}

    class FakeResponse:
        status_code = 200

        @staticmethod
        def json():
            return {"choices": [{"message": {"content": "summary"}}]}

    class FakeClient:
        def __init__(self, *args, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb):
            return False

        async def post(self, url, headers, json):
            received_headers.update(headers)
            return FakeResponse()

    monkeypatch.setattr(httpx, "AsyncClient", FakeClient)

    try:
        result = asyncio.run(llm_service.complete([{"role": "user", "content": "summarize"}]))
        assert result == "summary"
        assert received_headers["Authorization"] == f"Bearer {test_key}"
    finally:
        object.__setattr__(settings, "groq_api_key", original_key)
        object.__setattr__(settings, "groq_model", original_model)


def test_meeting_summary_returns_structured_unsaved_draft(monkeypatch):
    fields = {
        "requirements": ["SSO"],
        "objections": ["Price"],
        "stakeholders": ["CFO"],
        "commitments": ["Send proposal"],
        "deadlines": ["Friday"],
        "unresolved_questions": ["Who approves?"],
    }
    monkeypatch.setattr(llm_service, "is_configured", lambda: True)

    async def fake_complete(messages, **kwargs):
        assert "SSO" in messages[-1]["content"]
        assert kwargs["json_mode"] is True
        return json.dumps(fields)

    monkeypatch.setattr(llm_service, "complete", fake_complete)
    result = asyncio.run(deal_agent.summarize_meeting("acme-corp", "They require SSO."))

    assert result == {
        "deal_id": "acme-corp",
        "summary": fields,
        "provider": "groq",
        "saved": False,
    }


def test_meeting_summary_rejects_malformed_model_output(monkeypatch):
    monkeypatch.setattr(llm_service, "is_configured", lambda: True)

    async def fake_complete(*args, **kwargs):
        return "not valid JSON"

    monkeypatch.setattr(llm_service, "complete", fake_complete)

    with pytest.raises(AppError, match="meeting summary could not be checked"):
        asyncio.run(deal_agent.summarize_meeting("acme-corp", "The customer asked about SSO."))


def test_meeting_summary_requires_configured_groq(monkeypatch):
    monkeypatch.setattr(llm_service, "is_configured", lambda: False)

    with pytest.raises(AppError, match="Groq is not configured"):
        asyncio.run(deal_agent.summarize_meeting("acme-corp", "The customer asked about SSO."))


def test_meeting_summary_rejects_empty_transcript_without_calling_model(monkeypatch):
    def unexpected_call(*args, **kwargs):
        raise AssertionError("The model must not be called for an empty transcript.")

    monkeypatch.setattr(llm_service, "complete", unexpected_call)

    with pytest.raises(AppError, match="transcript before reviewing"):
        asyncio.run(deal_agent.summarize_meeting("acme-corp", " \n "))
