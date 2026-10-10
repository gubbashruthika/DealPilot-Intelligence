from fastapi import APIRouter

from app.config import settings
from app.models import DealIn, InteractionIn
from app.services import deal_agent, hindsight_service, llm_service

router = APIRouter(prefix="/api")


@router.get("/status")
async def status():
    """Configuration flags only. Never returns key values."""
    return {
        "hindsight_configured": hindsight_service.is_configured(),
        "groq_configured": llm_service.is_configured(),
        "bank_id": settings.hindsight_bank_id,
        "model": settings.groq_model,
    }


@router.get("/deals")
async def deals():
    return {"deals": deal_agent.list_deals()}


@router.post("/deals", status_code=201)
async def create_deal(body: DealIn):
    return {"deal": deal_agent.create_deal(body.name)}


@router.get("/deals/{deal_id}")
async def deal(deal_id: str):
    return await deal_agent.deal_detail(deal_id)


@router.post("/deals/{deal_id}/interactions", status_code=201)
async def add_interaction(deal_id: str, body: InteractionIn):
    record = await deal_agent.add_interaction(deal_id, body.content)
    return {"interaction": record, "interaction_count": len(deal_agent.timeline(deal_id))}


@router.post("/deals/{deal_id}/load-demo")
async def load_demo(deal_id: str):
    result = await deal_agent.load_demo(deal_id)
    return {**result, "interaction_count": len(deal_agent.timeline(deal_id))}


@router.get("/deals/{deal_id}/memories")
async def memories(deal_id: str):
    return await deal_agent.list_memories(deal_id)
