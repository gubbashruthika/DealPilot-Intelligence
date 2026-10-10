from typing import Optional

from pydantic import BaseModel, Field


class InteractionIn(BaseModel):
    content: str = ""


class AskIn(BaseModel):
    question: str = ""
    # None = auto-detect meeting-prep questions; True/False forces brief/plain text.
    brief: Optional[bool] = None


class MeetingSummaryIn(BaseModel):
    transcript: str = Field(min_length=1, max_length=10_000)


class DealIn(BaseModel):
    name: str = Field(min_length=1, max_length=120)
