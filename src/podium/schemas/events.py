from datetime import datetime

from pydantic import BaseModel, Field


class EventCreate(BaseModel):
    name: str = Field(..., max_length=160, examples=["Sample Hack 2026"])
    description: str = ""
    is_public: bool = False
    max_team_size: int = Field(4, ge=1, le=20)
    submissions_open_at: datetime | None = None
    submissions_close_at: datetime | None = None
    voting_open_at: datetime | None = None
    voting_close_at: datetime | None = None


class EventUpdate(EventCreate):
    pass


class TrackCreate(BaseModel):
    name: str = Field(..., max_length=120)
    description: str = ""


class PrizeCreate(BaseModel):
    name: str = Field(..., max_length=120)
    amount: str = Field("", max_length=60)
    description: str = ""
    track: str = Field("", description="Track public id, optional")


class PrizeAward(BaseModel):
    project: str | None = Field(default=None, description="Project public id, or null to clear")


class TeamCreate(BaseModel):
    name: str = Field(..., max_length=120)


def event_out(event, stage: str) -> dict:
    return {
        "id": event.public_id,
        "slug": event.slug,
        "name": event.name,
        "description": event.description,
        "is_public": event.is_public,
        "stage": stage,
        "max_team_size": event.max_team_size,
        "submissions_open_at": event.submissions_open_at,
        "submissions_close_at": event.submissions_close_at,
        "judging_opened_at": event.judging_opened_at,
        "judging_closed_at": event.judging_closed_at,
        "voting_open_at": event.voting_open_at,
        "voting_close_at": event.voting_close_at,
        "results_published_at": event.results_published_at,
        "archived_at": event.archived_at,
        "tracks": [
            {"id": t.public_id, "name": t.name, "description": t.description} for t in event.tracks
        ],
        "prizes": [
            {
                "id": p.public_id,
                "name": p.name,
                "amount": p.amount_text,
                "track": p.track.public_id if p.track else None,
                "project": p.project.public_id if p.project else None,
            }
            for p in event.prizes
        ],
    }
