from fastapi import APIRouter

from app.models import AskIn, MeetingSummaryIn
from app.services import deal_agent

router = APIRouter(prefix="/api")


@router.post("/deals/{deal_id}/ask")
async def ask(deal_id: str, body: AskIn):
    """Recall from Hindsight, then answer with Groq using only the recalled memories."""
    return await deal_agent.ask(deal_id, body.question, body.brief)


@router.post("/deals/{deal_id}/ask-without-memory")
async def ask_without_memory(deal_id: str, body: AskIn):
    """Same question, no Hindsight context (used by the 'Why Memory Matters' demo)."""
    return await deal_agent.ask_without_memory(deal_id, body.question)


@router.post("/deals/{deal_id}/meeting-summary")
async def meeting_summary(deal_id: str, body: MeetingSummaryIn):
    """Create a structured draft from an unsaved meeting transcript."""
    return await deal_agent.summarize_meeting(deal_id, body.transcript)
