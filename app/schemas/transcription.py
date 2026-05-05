import uuid

from pydantic import BaseModel, Field

from app.schemas.skill_run import SkillRunStatusT


class TranscribeYoutubeIn(BaseModel):
    url: str = Field(min_length=1)


class YoutubeMetaOut(BaseModel):
    title: str | None
    duration_seconds: int | None
    channel: str | None
    video_id: str | None


class TranscriptionStarted(BaseModel):
    skill_run_id: uuid.UUID
    skill: str
    status: SkillRunStatusT
