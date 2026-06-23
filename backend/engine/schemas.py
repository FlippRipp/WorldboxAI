"""Structured output schemas for LLM pipeline hardening."""
from pydantic import BaseModel, Field


class MemorySummary(BaseModel):
    """Structured summary produced by the Librarian for long-term memory storage."""
    summary: str = Field(description="One-paragraph factual summary of the most important events in this chunk of history")
    entities: list[str] = Field(default_factory=list, description="Named entities mentioned (characters, places, items, factions)")
    topics: list[str] = Field(default_factory=list, description="Key topics or themes (e.g. combat, diplomacy, exploration, mystery)")
    turn_range: str = Field(default="", description="Range of turns covered, e.g. 'turns 12-15'")


class MemoryImportance(BaseModel):
    """Structured importance score produced by the Librarian."""
    importance: int = Field(ge=1, le=10, description="Importance score 1 (trivial) to 10 (campaign-defining)")
    reason: str = Field(description="Single-sentence justification for the importance score")
    permanent: bool = Field(default=False, description="True if this memory should never be purged by decay")
