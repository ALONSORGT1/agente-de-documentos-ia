from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, StringConstraints

Question = Annotated[str, StringConstraints(strip_whitespace=True, min_length=3, max_length=3000)]


class HistoryMessage(BaseModel):
    model_config = ConfigDict(extra="forbid")
    role: Literal["user", "assistant"]
    content: str = Field(min_length=1, max_length=3000)


class AskRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    question: Question
    use_search: bool = True
    history: list[HistoryMessage] = Field(default_factory=list, max_length=6)


class Source(BaseModel):
    name: str
    url: str | None = None
    drive_id: str | None = None
    file_id: str | None = None
    snippet: str | None = None
    citation_indices: list[int] = Field(default_factory=list)
    evidence_kind: Literal["file_citation", "verified_excerpt"] = "file_citation"
    reference_number: int | None = Field(default=None, ge=1)


class AskResponse(BaseModel):
    success: bool
    answer: str
    sources: list[Source]
    grounded: bool = False
    mode: Literal["documental", "experimental"] = "documental"
    warning: str | None = None
    usage: dict[str, int] | None = None
