from pydantic import BaseModel, Field


class CommentCreate(BaseModel):
    body: str = Field(..., max_length=2000)


class CodesCreate(BaseModel):
    count: int = Field(10, ge=1, le=5000)
    emails: list[str] = Field(default_factory=list)


class VoidVote(BaseModel):
    reason: str = Field(..., max_length=200)


class VotingSettings(BaseModel):
    voting_mode: str | None = Field(None, description="link | email | account")
    quadratic_enabled: bool | None = None
    voting_credits: int | None = Field(None, ge=1, le=1000)
    comments_enabled: bool | None = None


class CodeRedeem(BaseModel):
    code: str = Field(min_length=4, max_length=40)
