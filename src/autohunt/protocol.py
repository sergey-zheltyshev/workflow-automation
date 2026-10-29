"""Strict Codex/operator exchange. Free text cannot become an action command."""

from datetime import UTC, datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from .audit import append_audit_event


class WorkItem(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str = Field(min_length=1)
    kind: Literal["vacancy_review", "chat_review", "external_item_review"]
    vacancy_id: str | None = None
    chat_id: str | None = None
    evidence_refs: list[str] = Field(min_length=1)
    created_at: datetime


class Decision(BaseModel):
    model_config = ConfigDict(extra="forbid")

    work_item_id: str = Field(min_length=1)
    decision: Literal["accept", "reject", "hold", "needs_user"]
    reason: str = Field(min_length=1)
    evidence_refs: list[str] = Field(min_length=1)
    decided_at: datetime


def record_decision(audit_path, decision: Decision) -> None:
    append_audit_event(audit_path, "decision_recorded", decision.model_dump(mode="json"))


def new_work_item(item_id: str, kind: str, evidence_refs: list[str], **ids: str | None) -> WorkItem:
    return WorkItem(
        id=item_id,
        kind=kind,
        evidence_refs=evidence_refs,
        created_at=datetime.now(UTC),
        **ids,
    )
