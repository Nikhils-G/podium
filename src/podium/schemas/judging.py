from pydantic import BaseModel, Field


class InviteCreate(BaseModel):
    email: str = Field(..., max_length=254)
    tracks: list[str] = Field(default_factory=list, description="Track public ids")


class CriterionCreate(BaseModel):
    name: str = Field(..., max_length=120)
    description: str = ""
    weight: float = Field(1.0, gt=0, le=100)
    min_score: int = Field(1, ge=0)
    max_score: int = Field(5, le=100)


class CriterionUpdate(BaseModel):
    name: str | None = None
    description: str | None = None
    weight: float | None = None
    min_score: int | None = None
    max_score: int | None = None


class ManualAssign(BaseModel):
    judge: str = Field(..., description="Judge public id")
    projects: list[str] = Field(..., min_length=1, description="Project public ids")


class AutoAssign(BaseModel):
    reviews_per_project: int = Field(3, ge=1, le=20)
    seed: int = Field(1, ge=0)


class ReviewWrite(BaseModel):
    scores: dict[str, int] = Field(default_factory=dict, description="criterion public id → value")
    comment: str = ""
    submit: bool = False


def criterion_out(c) -> dict:
    return {
        "id": c.public_id,
        "key": c.key,
        "name": c.name,
        "description": c.description,
        "weight": c.weight,
        "min": c.min_score,
        "max": c.max_score,
        "archived": c.archived_at is not None,
    }


def review_out(review, criteria) -> dict:
    by_id = {c.id: c for c in criteria}
    return {
        "id": review.public_id,
        "project": review.project.public_id,
        "judge": review.judge.public_id,
        "status": review.status.value,
        "submitted_at": review.submitted_at,
        "comment": review.comment,
        "scores": {
            by_id[i.criterion_id].public_id: i.value
            for i in review.items
            if i.criterion_id in by_id
        },
    }
