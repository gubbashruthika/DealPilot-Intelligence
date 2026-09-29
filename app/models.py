from typing import Optional

from pydantic import BaseModel


class InteractionIn(BaseModel):
    content: str = ""


class AskIn(BaseModel):
    question: str = ""
    # None = auto-detect meeting-prep questions; True/False forces brief/plain text.
    brief: Optional[bool] = None
