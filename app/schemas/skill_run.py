import uuid
from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict

SkillRunStatusT = Literal["pending", "running", "completed", "failed"]


class SkillRunOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    node_id: uuid.UUID
    skill: str
    status: SkillRunStatusT
    error: str | None
    duration_ms: int | None
    created_at: datetime
    completed_at: datetime | None


class SkillRunStarted(BaseModel):
    skill_run_id: uuid.UUID
    skill: str
    status: SkillRunStatusT
