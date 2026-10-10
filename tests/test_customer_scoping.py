import asyncio
import json

import httpx
import pytest

from app.config import settings
from app.errors import AppError
from app.main import app
from app.services import deal_agent, hindsight_service, llm_service


@pytest.fixture
def isolated_data_dir(tmp_path):
    original = settings.data_dir
    object.__setattr__(settings, "data_dir", tmp_path)
    try:
        yield tmp_path
    finally:
        object.__setattr__(settings, "data_dir", original)


def test_customers_have_stable_ids_and_duplicate_names_are_rejected(isolated_data_dir):
    existing = deal_agent.list_deals()
    assert [deal["id"] for deal in existing] == ["acme-corp"]

    created = deal_agent.create_deal("Northwind")
    assert len(created["id"]) == 32
    assert deal_agent.get_deal(created["id"]) == created
    assert deal_agent.list_deals()[-1]["id"] == created["id"]

    with pytest.raises(AppError) as error:
        deal_agent.create_deal(" northwind ")
    assert error.value.status == 409
    assert json.loads((isolated_data_dir / "deals.json").read_text()) == [
        {"id": "acme-corp", "name": "Acme Corp"},
        created,
    ]


def test_customer_api_creates_selectable_customer_with_empty_history(isolated_data_dir):
    async def request_customer():
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="http://dealpilot.test",
        ) as client:
            initial = await client.get("/api/deals")
            created = await client.post("/api/deals", json={"name": "Northwind"})
            deal_id = created.json()["deal"]["id"]
            refreshed = await client.get("/api/deals")
            detail = await client.get(f"/api/deals/{deal_id}")
            duplicate = await client.post("/api/deals", json={"name": " northwind "})
            return initial, created, refreshed, detail, duplicate

    initial, created, refreshed, detail, duplicate = asyncio.run(request_customer())

    assert initial.status_code == 200
    assert [deal["id"] for deal in initial.json()["deals"]] == ["acme-corp"]
    assert created.status_code == 201
    deal = created.json()["deal"]
    assert deal["name"] == "Northwind"
    assert deal_agent.timeline(deal["id"]) == []
    assert any(item["id"] == deal["id"] for item in refreshed.json()["deals"])
    assert detail.status_code == 200
    assert detail.json()["id"] == deal["id"]
    assert detail.json()["interactions"] == []
    assert duplicate.status_code == 409
    assert duplicate.json()["error"] == "A customer with this name already exists."


def test_customer_api_returns_clear_message_for_empty_name(isolated_data_dir):
    async def request_empty_name():
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="http://dealpilot.test",
        ) as client:
            return await client.post("/api/deals", json={"name": "   "})

    response = asyncio.run(request_empty_name())

    assert response.status_code == 400
    assert response.json()["error"] == "Enter a customer name."


def test_interaction_history_is_partitioned_by_customer(isolated_data_dir, monkeypatch):
    customer_a = deal_agent.create_deal("Customer A")
    customer_b = deal_agent.create_deal("Customer B")

    async def fake_retain(deal_id, deal_name, content, document_id=None):
        return None

    monkeypatch.setattr(hindsight_service, "retain", fake_retain)
    asyncio.run(deal_agent.add_interaction(customer_a["id"], "Customer A confidential requirement."))
    asyncio.run(deal_agent.add_interaction(customer_a["id"], "Customer A updated requirement."))
    asyncio.run(deal_agent.add_interaction(customer_b["id"], "Customer B separate timeline."))

    history_b = deal_agent.timeline(customer_b["id"])
    assert [item["content"] for item in history_b] == ["Customer B separate timeline."]
    assert all(item["content"] != "Customer A confidential requirement." for item in history_b)
    history_a = deal_agent.timeline(customer_a["id"])
    assert [item["content"] for item in history_a] == [
        "Customer A confidential requirement.",
        "Customer A updated requirement.",
    ]
    assert len({item["id"] for item in history_a}) == 2
    assert all(item["created_at"] for item in history_a)
    assert (isolated_data_dir / "interactions.json").exists()


def test_customer_answer_uses_only_requested_customer_context(isolated_data_dir, monkeypatch):
    customer_a = deal_agent.create_deal("Customer A")
    customer_b = deal_agent.create_deal("Customer B")

    async def fake_retain(deal_id, deal_name, content, document_id=None):
        return None

    monkeypatch.setattr(hindsight_service, "retain", fake_retain)
    asyncio.run(deal_agent.add_interaction(customer_a["id"], "A_SECRET: Customer A pricing ceiling."))
    asyncio.run(deal_agent.add_interaction(customer_b["id"], "Customer B requires a regional deployment."))

    calls = []
    prompts = []

    async def scoped_recall(query, deal_id):
        calls.append(deal_id)
        assert deal_id == customer_b["id"]
        return []

    async def fake_complete(messages, **kwargs):
        prompts.extend(messages)
        return "Customer B requires a regional deployment."

    monkeypatch.setattr(hindsight_service, "recall", scoped_recall)
    monkeypatch.setattr(deal_agent.llm, "complete", fake_complete)

    response = asyncio.run(deal_agent.ask(customer_b["id"], "What is the requirement?", brief=False))

    assert response["answer"] == "Customer B requires a regional deployment."
    assert response["memory_source"] == "customer_history"
    assert calls and set(calls) == {customer_b["id"]}
    assert not {"live_intelligence", "live_status", "live_message", "deal_intelligence"} & response.keys()
    assert "A_SECRET" not in json.dumps(prompts)


def test_ask_without_memory_uses_only_question_and_groq(isolated_data_dir, monkeypatch):
    prompts = []

    async def fake_complete(messages, **kwargs):
        prompts.extend(messages)
        return "A general answer without customer history."

    monkeypatch.setattr(deal_agent.llm, "complete", fake_complete)

    response = asyncio.run(
        deal_agent.ask_without_memory("acme-corp", "What should I prepare?")
    )

    assert response["answer"] == "A general answer without customer history."
    assert prompts[-1]["content"] == "What should I prepare?"
    assert not {"live_intelligence", "live_status", "live_message", "deal_intelligence"} & response.keys()


def test_hindsight_recall_and_retain_use_strict_customer_tags(monkeypatch):
    class FakeClient:
        async def arecall(self, bank_id, query, tags=None, tags_match=None, budget=None):
            self.recall_tags = tags
            self.tags_match = tags_match
            return {"results": [
                {
                    "id": "memory-a",
                    "text": "Customer A must be discarded.",
                    "tags": ["dealpilot_deal_customer-a-id"],
                },
                {"id": "legacy", "text": "Untagged legacy memory must be discarded."},
                {
                    "id": "memory-b",
                    "text": "Only tagged customer context.",
                    "tags": ["dealpilot_deal_customer-b-id"],
                },
            ]}

        async def aretain(self, bank_id, content, tags=None, **kwargs):
            self.retain_tags = tags

    client = FakeClient()
    monkeypatch.setattr(hindsight_service, "is_configured", lambda: True)
    monkeypatch.setattr(hindsight_service, "_get_client", lambda: client)

    async def no_bank_setup(_client):
        return None

    monkeypatch.setattr(hindsight_service, "_ensure_bank", no_bank_setup)

    recalled = asyncio.run(hindsight_service.recall("requirements", "customer-b-id"))
    asyncio.run(hindsight_service.retain(
        "customer-b-id", "Customer B", "A new requirement.", document_id="customer-b-id-1"
    ))

    expected_tag = "dealpilot_deal_customer-b-id"
    assert client.recall_tags == [expected_tag]
    assert client.tags_match == "exact"
    assert client.retain_tags == [expected_tag]
    assert [item["id"] for item in recalled] == ["memory-b"]
    assert recalled[0]["source"] == "hindsight"


def test_hindsight_refuses_unfiltered_recall_when_client_lacks_tag_filters(monkeypatch):
    class UnsupportedClient:
        called = False

        async def arecall(self, bank_id, query, budget=None):
            self.called = True
            return {"results": []}

    client = UnsupportedClient()
    monkeypatch.setattr(hindsight_service, "is_configured", lambda: True)
    monkeypatch.setattr(hindsight_service, "_get_client", lambda: client)

    with pytest.raises(AppError) as error:
        asyncio.run(hindsight_service.recall("customer question", "customer-b-id"))

    assert error.value.code == "hindsight_scoping_unsupported"
    assert not client.called


def test_offline_llm_fallback_recognizes_customer_scoped_context(monkeypatch):
    monkeypatch.setattr(llm_service, "is_configured", lambda: False)

    answer = asyncio.run(llm_service.complete([
        {"role": "system", "content": "Answer only using supplied customer context."},
        {
            "role": "user",
            "content": "Customer-specific context:\nCustomer B requires regional hosting.\n\nRequest: What do they need?",
        },
    ]))

    assert answer.startswith("Offline demo mode:")
    assert "recalled memory" in answer
