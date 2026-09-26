from datetime import datetime

from pydantic import BaseModel, Field


class ProjectCreate(BaseModel):
    title: str = Field(..., max_length=140, examples=["Quiet Hours"])
    summary: str = Field("", max_length=280)
    description: str = Field("", max_length=20_000)
    repo_url: str = Field("", max_length=500)
    demo_url: str = Field("", max_length=500)
    video_url: str = Field("", max_length=500)
    track: str = Field("", description="Track public id, e.g. trk_01")
    submit: bool = Field(True, description="False saves a draft only the team can see")


class ProjectOut(BaseModel):
    id: str
    title: str
    summary: str
    team: str
    track: str | None
    repo_url: str
    demo_url: str
    video_url: str
    status: str
    submitted_at: datetime | None


class ProjectUpdate(BaseModel):
    title: str | None = Field(None, max_length=140)
    summary: str | None = Field(None, max_length=280)
    description: str | None = Field(None, max_length=20_000)
    repo_url: str | None = Field(None, max_length=500)
    demo_url: str | None = Field(None, max_length=500)
    video_url: str | None = Field(None, max_length=500)
    track: str | None = None
    submit: bool | None = Field(None, description="True submits a draft, False returns it to draft")
