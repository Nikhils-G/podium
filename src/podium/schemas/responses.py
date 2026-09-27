"""Response models for the core read endpoints. Each allows extra keys, so documenting a shape
never silently drops a field the handler returns."""

from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field

from podium.schemas.projects import ProjectOut


class Open(BaseModel):
    model_config = ConfigDict(extra="allow")


class TrackOut(Open):
    id: str
    name: str
    description: str = ""


class PrizeOut(Open):
    id: str
    name: str
    amount: str = ""
    track: str | None = None
    project: str | None = Field(None, description="Awarded project public id, once awarded")


class EventOut(Open):
    id: str
    slug: str
    name: str
    description: str = ""
    is_public: bool
    stage: str = Field(
        description="draft, upcoming, open, closed, judging, judged, voting, published or archived"
    )
    max_team_size: int
    submissions_open_at: datetime | None = None
    submissions_close_at: datetime | None = None
    voting_open_at: datetime | None = None
    voting_close_at: datetime | None = None
    results_published_at: datetime | None = None
    archived_at: datetime | None = None
    tracks: list[TrackOut] = []
    prizes: list[PrizeOut] = []


class EventEnvelope(Open):
    event: EventOut


class EventsOut(Open):
    events: list[EventOut]


class GalleryOut(Open):
    event: str
    total: int
    page: int
    pages: int
    projects: list[ProjectOut]


class ResultRow(Open):
    rank: int | None
    tied: bool = False
    project: str
    title: str
    track: str | None
    reviews: int
    raw_mean: float | None
    normalized: float | None
    rank_raw: int | None
    rank_normalized: int | None
    disagreement: float | None


class JudgeStatsOut(Open):
    judge: str
    reviews: int
    mean: float | None
    std: float | None
    shrunk_mean: float | None
    shrunk_std: float | None
    flat: bool


class ResultsOut(Open):
    method: str
    basis: str
    global_mean: float | None
    global_std: float | None
    projects: list[ResultRow]
    judges: list[JudgeStatsOut] = Field(default=[], description="Organizers only; empty otherwise")


class TallyRow(Open):
    project: str
    title: str
    votes: int
    voters: int


class TallyOut(Open):
    total_votes: int
    total_voters: int
    voided: int
    projects: list[TallyRow]


class UserOut(Open):
    id: str
    name: str
    email: str
    is_admin: bool


class MeOut(Open):
    user: UserOut


class TokenOut(Open):
    id: int
    name: str
    prefix: str
    scope: str
    expires_at: datetime | None
    created_at: datetime
    last_used_at: datetime | None


class TokensOut(Open):
    tokens: list[TokenOut]


class CertificateOut(Open):
    serial: str
    kind: str
    payload: dict
    signature: str
    issued_at: datetime
    revoked_at: datetime | None


class VerifyOut(Open):
    serial: str
    status: str = Field(description="valid | revoked | invalid | not_found")
    public_key_hex: str
    certificate: CertificateOut | None
