"""Judging: roster, rubric, assignments, reviews, results, exports, audit.

Role isolation is enforced here and in services.authz — a judge can read only their own
reviews; organizers can read everyone's; participants reach none of it."""

from fastapi import APIRouter, Depends, Query, Request
from fastapi.responses import StreamingResponse
from sqlalchemy.orm import Session as DbSession

from podium.config import Settings, get_settings
from podium.db import get_db
from podium.errors import NotFound
from podium.models import Role, User
from podium.schemas.judging import (
    AutoAssign,
    CriterionCreate,
    CriterionUpdate,
    InviteCreate,
    ManualAssign,
    ReviewWrite,
    criterion_out,
    review_out,
)
from podium.schemas.responses import ResultsOut
from podium.security.deps import (
    EventContext,
    load_event,
    require_event_role,
    require_organizer,
    require_user,
)
from podium.security.ratelimit import ip_hash
from podium.services import assignments as assignments_service
from podium.services import audit as audit_service
from podium.services import exports, scoring
from podium.services import judges as judges_service
from podium.services import reviews as reviews_service
from podium.services import rubric as rubric_service
from podium.services.authz import assert_can_view_judge_reviews

router = APIRouter(tags=["judging"])


# --- reviews: the isolation surface --------------------------------------------------------------


@router.get("/events/{slug}/judges/me/reviews")
def my_reviews(
    ctx: EventContext = Depends(require_event_role(Role.judge)), db: DbSession = Depends(get_db)
):
    """The caller's own reviews in this event. Judges only (organizers see an empty list)."""
    criteria = rubric_service.criteria(db, ctx.event, include_archived=True)
    rows = reviews_service.reviews_of_judge(db, ctx.event, ctx.user)
    return {"judge": ctx.user.public_id, "reviews": [review_out(r, criteria) for r in rows]}


@router.get("/events/{slug}/judges/{judge_id}/reviews")
def judge_reviews(
    judge_id: str,
    ctx: EventContext = Depends(load_event),
    user: User = Depends(require_user),
    db: DbSession = Depends(get_db),
):
    """A specific judge's reviews. 403 unless you are that judge or an organizer of the event."""
    judge = judges_service.judge_by_public_id(db, ctx.event, judge_id)
    assert_can_view_judge_reviews(db, ctx.event, user, judge)
    criteria = rubric_service.criteria(db, ctx.event, include_archived=True)
    rows = reviews_service.reviews_of_judge(db, ctx.event, judge)
    return {"judge": judge.public_id, "reviews": [review_out(r, criteria) for r in rows]}


@router.get("/events/{slug}/judges/me/queue")
def my_queue(
    ctx: EventContext = Depends(require_event_role(Role.judge)), db: DbSession = Depends(get_db)
):
    """Projects assigned to the caller, each with its review state: todo, in progress, done."""
    items = reviews_service.queue(db, ctx.event, ctx.user)
    return {
        "judging_open": reviews_service.judging_is_open(ctx.event),
        "queue": [
            {
                "project": i.project.public_id,
                "title": i.project.title,
                "state": i.state,
                "review": i.review.public_id if i.review else None,
            }
            for i in items
        ],
    }


@router.get("/events/{slug}/reviews/{project_id}")
def get_my_review(
    project_id: str,
    ctx: EventContext = Depends(require_event_role(Role.judge)),
    db: DbSession = Depends(get_db),
):
    """The caller's own review of one assigned project, draft or submitted."""
    assignment = reviews_service.assignment_for(db, ctx.event, ctx.user, project_id)
    review = reviews_service.review_for(db, assignment)
    if review is None:
        raise NotFound("No review yet for this project.")
    return {
        "review": review_out(review, rubric_service.criteria(db, ctx.event, include_archived=True))
    }


@router.put("/events/{slug}/reviews/{project_id}")
def write_my_review(
    request: Request,
    project_id: str,
    body: ReviewWrite,
    ctx: EventContext = Depends(require_event_role(Role.judge)),
    db: DbSession = Depends(get_db),
):
    """Save a draft (submit=false) or submit (submit=true, all criteria required)."""
    assignment = reviews_service.assignment_for(db, ctx.event, ctx.user, project_id)
    review = reviews_service.save(
        db,
        ctx.event,
        assignment,
        ctx.user,
        body.scores,
        body.comment,
        submit=body.submit,
        ip_hash=ip_hash(request),
    )
    return {
        "review": review_out(review, rubric_service.criteria(db, ctx.event, include_archived=True))
    }


@router.post("/events/{slug}/reviews/{project_id}/reopen")
def reopen_my_review(
    project_id: str,
    ctx: EventContext = Depends(require_event_role(Role.judge)),
    db: DbSession = Depends(get_db),
):
    """Turn a submitted review back into a draft while judging is open."""
    assignment = reviews_service.assignment_for(db, ctx.event, ctx.user, project_id)
    review = reviews_service.reopen(db, ctx.event, assignment, ctx.user)
    return {
        "review": review_out(review, rubric_service.criteria(db, ctx.event, include_archived=True))
    }


# --- roster --------------------------------------------------------------------------------------


@router.get("/events/{slug}/judges")
def list_judges(ctx: EventContext = Depends(require_organizer), db: DbSession = Depends(get_db)):
    """Judges of the event with their tracks and assignment counts."""
    rows = judges_service.list_judges(db, ctx.event)
    return {
        "judges": [
            {
                "id": r.user.public_id,
                "name": r.user.name,
                "email": r.user.email,
                "tracks": [t.public_id for t in r.tracks],
                "assigned": r.assigned,
                "done": r.done,
            }
            for r in rows
        ]
    }


@router.post("/events/{slug}/judges/invites", status_code=201)
def invite_judge(
    body: InviteCreate,
    ctx: EventContext = Depends(require_organizer),
    db: DbSession = Depends(get_db),
    settings: Settings = Depends(get_settings),
):
    """Create an invite link. Send it yourself — Podium never needs an email service."""
    invite, token = judges_service.invite_judge(db, ctx.event, ctx.user, body.email, body.tracks)
    return {
        "invite": {
            "id": invite.id,
            "email": invite.email,
            "expires_at": invite.expires_at,
            "link": f"{settings.base_url}/judge-invite/{token}",
        }
    }


@router.post("/events/{slug}/judges/invites/{invite_id}/regenerate")
def regenerate_invite(
    invite_id: int,
    ctx: EventContext = Depends(require_organizer),
    db: DbSession = Depends(get_db),
    settings: Settings = Depends(get_settings),
):
    """A fresh link for a pending invite; the previous link stops working immediately."""
    invite, token = judges_service.regenerate_invite(db, ctx.event, ctx.user, invite_id)
    return {
        "invite": {
            "id": invite.id,
            "email": invite.email,
            "expires_at": invite.expires_at,
            "link": f"{settings.base_url}/judge-invite/{token}",
        }
    }


@router.delete("/events/{slug}/judges/invites/{invite_id}", status_code=204)
def revoke_invite(
    invite_id: int,
    ctx: EventContext = Depends(require_organizer),
    db: DbSession = Depends(get_db),
):
    """Cancel a pending judge invitation; its link stops working at once."""
    judges_service.revoke_invite(db, ctx.event, ctx.user, invite_id)


@router.delete("/events/{slug}/judges/{judge_id}", status_code=204)
def remove_judge(
    judge_id: str, ctx: EventContext = Depends(require_organizer), db: DbSession = Depends(get_db)
):
    """Remove a judge from the event."""
    judges_service.remove_judge(db, ctx.event, ctx.user, judge_id)


# --- rubric ---------------------------------------------------------------------------------------


@router.get("/events/{slug}/rubric")
def get_rubric(
    ctx: EventContext = Depends(require_event_role(Role.judge)), db: DbSession = Depends(get_db)
):
    """Scoring criteria with weights and scales."""
    return {
        "locked": rubric_service.is_locked(ctx.event),
        "criteria": [criterion_out(c) for c in rubric_service.criteria(db, ctx.event)],
    }


@router.post("/events/{slug}/rubric", status_code=201)
def add_criterion(
    body: CriterionCreate,
    ctx: EventContext = Depends(require_organizer),
    db: DbSession = Depends(get_db),
):
    """Add a criterion; refused once judging has opened."""
    crit = rubric_service.add_criterion(
        db,
        ctx.event,
        ctx.user,
        body.name,
        body.description,
        body.weight,
        body.min_score,
        body.max_score,
    )
    return {"criterion": criterion_out(crit)}


@router.patch("/events/{slug}/rubric/{criterion_id}")
def update_criterion(
    criterion_id: str,
    body: CriterionUpdate,
    ctx: EventContext = Depends(require_organizer),
    db: DbSession = Depends(get_db),
):
    """Rename or reweight a criterion; the scale is locked once it has scores."""
    crit = rubric_service.get_criterion(db, ctx.event, criterion_id)
    crit = rubric_service.update_criterion(
        db, ctx.event, ctx.user, crit, **body.model_dump(exclude_unset=True)
    )
    return {"criterion": criterion_out(crit)}


@router.delete("/events/{slug}/rubric/{criterion_id}", status_code=204)
def archive_criterion(
    criterion_id: str,
    ctx: EventContext = Depends(require_organizer),
    db: DbSession = Depends(get_db),
):
    """Archive a criterion that has scores, or delete one that has none."""
    crit = rubric_service.get_criterion(db, ctx.event, criterion_id)
    rubric_service.archive_criterion(db, ctx.event, ctx.user, crit)


# --- assignments ------------------------------------------------------------------------------


@router.get("/events/{slug}/assignments")
def list_assignments(
    ctx: EventContext = Depends(require_organizer), db: DbSession = Depends(get_db)
):
    """Every judge-to-project assignment with its status."""
    loads = assignments_service.by_judge(db, ctx.event)
    return {
        "judges": [
            {
                "id": load.judge.public_id,
                "name": load.judge.name,
                "assignments": [
                    {
                        "id": a.id,
                        "project": a.project.public_id,
                        "status": a.status.value,
                        "method": a.method.value,
                    }
                    for a in load.assignments
                ],
            }
            for load in loads
        ]
    }


@router.post("/events/{slug}/assignments", status_code=201)
def assign_manually(
    body: ManualAssign,
    ctx: EventContext = Depends(require_organizer),
    db: DbSession = Depends(get_db),
):
    """Assign one judge to one project; idempotent and never a judge's own team."""
    judge = judges_service.judge_by_public_id(db, ctx.event, body.judge)
    created = assignments_service.assign_manually(db, ctx.event, ctx.user, judge, body.projects)
    return {"created": created}


def _plan_out(plan) -> dict:
    return {
        "reviews_per_project": plan.reviews_per_project,
        "seed": plan.seed,
        "judges": plan.judges,
        "projects": plan.projects,
        "new_assignments": len(plan.entries),
        "entries": [
            {"judge": e.judge.public_id, "project": e.project.public_id, "fallback": e.fallback}
            for e in plan.entries
        ],
        "loads_after": {str(k): v for k, v in plan.loads_after.items()},
        "shortfalls": [{"project": p.public_id, "missing": n} for p, n in plan.shortfalls],
    }


@router.post("/events/{slug}/assignments/auto/preview")
def auto_preview(
    body: AutoAssign,
    ctx: EventContext = Depends(require_organizer),
    db: DbSession = Depends(get_db),
):
    """Deterministic for (reviews_per_project, seed): what apply would create, with shortfalls."""
    return {
        "plan": _plan_out(
            assignments_service.build_plan(
                db, ctx.event, reviews_per_project=body.reviews_per_project, seed=body.seed
            )
        )
    }


@router.post("/events/{slug}/assignments/auto/apply")
def auto_apply(
    body: AutoAssign,
    ctx: EventContext = Depends(require_organizer),
    db: DbSession = Depends(get_db),
):
    """Apply the deterministic, balanced assignment plan the preview showed."""
    plan = assignments_service.build_plan(
        db, ctx.event, reviews_per_project=body.reviews_per_project, seed=body.seed
    )
    created = assignments_service.apply_plan(db, ctx.event, ctx.user, plan)
    return {"created": created, "plan": _plan_out(plan)}


@router.delete("/events/{slug}/assignments/{assignment_id}", status_code=204)
def remove_assignment(
    assignment_id: int,
    ctx: EventContext = Depends(require_organizer),
    db: DbSession = Depends(get_db),
):
    """Remove an assignment that has no submitted review."""
    assignments_service.remove_assignment(db, ctx.event, ctx.user, assignment_id)


# --- results, exports, audit --------------------------------------------------------------------


@router.get("/events/{slug}/results", response_model=ResultsOut)
def results(ctx: EventContext = Depends(load_event), db: DbSession = Depends(get_db)):
    """Rankings. Public once results are published; organizers can always see them."""
    if ctx.event.results_published_at is None and not ctx.is_organizer:
        raise NotFound("Results haven't been published yet.")
    r = scoring.compute(db, ctx.event)
    return {
        "method": r.method.value,
        "basis": r.basis.value,
        "global_mean": r.global_mean,
        "global_std": r.global_std,
        "projects": [
            {
                "rank": p.rank_norm if r.basis.value == "normalized" else p.rank_raw,
                "tied": p.tied_norm,
                "project": p.project.public_id,
                "title": p.project.title,
                "track": p.track.name if p.track else None,
                "reviews": p.n,
                "raw_mean": p.raw_mean,
                "normalized": p.normalized,
                "rank_raw": p.rank_raw,
                "rank_normalized": p.rank_norm,
                "disagreement": p.disagreement,
            }
            for p in r.projects
        ],
        "judges": [
            {
                "judge": j.judge.public_id,
                "reviews": j.n,
                "mean": j.mean,
                "std": j.std,
                "shrunk_mean": j.shrunk_mean,
                "shrunk_std": j.shrunk_std,
                "flat": j.flat,
            }
            for j in r.judges
        ]
        if ctx.is_organizer
        else [],
    }


@router.get("/events/{slug}/exports/{name}.csv")
def export_csv(
    name: str, ctx: EventContext = Depends(require_organizer), db: DbSession = Depends(get_db)
):
    """CSV exports: projects, teams, assignments, reviews (raw per criterion), scores, audit."""
    generator = exports.GENERATORS.get(name)
    if generator is None:
        raise NotFound(f"No export called {name}. Try one of: {', '.join(exports.NAMES)}.")
    filename = f"{ctx.event.slug}-{name}.csv"
    return StreamingResponse(
        generator(db, ctx.event),
        media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@router.get("/events/{slug}/audit")
def audit_log(
    ctx: EventContext = Depends(require_organizer),
    db: DbSession = Depends(get_db),
    action: str = Query("", max_length=64),
    page: int = Query(1, ge=1),
):
    """Hash-chained audit entries, newest first; filter by action or actor."""
    data = audit_service.list_entries(db, ctx.event.id, action=action, page=page)
    return {
        "total": data.total,
        "page": data.page,
        "pages": data.pages,
        "entries": [
            {
                "id": a.id,
                "at": a.created_at,
                "actor_id": a.actor_id,
                "action": a.action,
                "entity_type": a.entity_type,
                "entity_id": a.entity_id,
                "meta": a.meta,
                "row_hash": a.row_hash,
                "prev_hash": a.prev_hash,
            }
            for a in data.rows
        ],
    }


@router.get("/events/{slug}/audit/verify")
def audit_verify(ctx: EventContext = Depends(require_organizer), db: DbSession = Depends(get_db)):
    """Recompute the hash chain over the whole log; any edited or deleted row breaks it."""
    report = audit_service.verify_chain(db)
    return {
        "entries": report.entries,
        "ok": report.ok,
        "first_bad_id": report.first_bad_id,
        "head_hash": report.head_hash,
    }


@router.get("/events/{slug}/audit/anchor")
def audit_anchor(
    ctx: EventContext = Depends(require_organizer),
    db: DbSession = Depends(get_db),
    settings: Settings = Depends(get_settings),
):
    """The chain head, signed with the instance key. Publish it externally so that nobody with
    database access can quietly rewrite history; verify with the well-known signing key."""
    return audit_service.anchor(db, settings)


# --- pairwise (Bradley-Terry) ---------------------------------------------------------------------


@router.get("/events/{slug}/judges/me/compare")
def next_comparison(
    ctx: EventContext = Depends(require_event_role(Role.judge)), db: DbSession = Depends(get_db)
):
    """The next pair of assigned projects to compare, plus progress."""
    from podium.services import pairwise

    state = pairwise.next_pair(db, ctx.event, ctx.user)
    return {
        "done": state.done,
        "total": state.total,
        "pair": [{"project": p.public_id, "title": p.title} for p in state.pair]
        if state.pair
        else None,
    }


@router.post("/events/{slug}/comparisons", status_code=201)
def record_comparison(
    request: Request,
    body: dict,
    ctx: EventContext = Depends(require_event_role(Role.judge)),
    db: DbSession = Depends(get_db),
):
    """Record a comparison: {a, b, winner} or {a, b, skip_reason}."""
    from podium.services import pairwise

    row = pairwise.record(
        db,
        ctx.event,
        ctx.user,
        str(body.get("a", "")),
        str(body.get("b", "")),
        body.get("winner"),
        body.get("skip_reason"),
        ip_hash=ip_hash(request),
    )
    return {
        "comparison": {"id": row.id, "winner": body.get("winner"), "skip_reason": row.skip_reason}
    }


@router.get("/events/{slug}/results/pairwise")
def pairwise_results(ctx: EventContext = Depends(load_event), db: DbSession = Depends(get_db)):
    """Bradley-Terry ranking from pairwise comparisons, with Spearman ρ against the
    normalized ranking."""
    from podium.services import pairwise

    if ctx.event.results_published_at is None and not ctx.is_organizer:
        raise NotFound("Results haven't been published yet.")
    r = pairwise.results(db, ctx.event)
    return {
        "comparisons": r.comparisons,
        "judge_comparisons": r.judge_comparisons,
        "derived_comparisons": r.derived_comparisons,
        "skipped": r.skipped,
        "judges": r.judges,
        "rho": r.rho,
        "projects": [
            {
                "rank": row.rank,
                "project": row.project.public_id,
                "title": row.project.title,
                "log_strength": row.log_strength,
                "comparisons": row.comparisons,
                "wins": row.wins,
                "rank_normalized": row.rank_normalized,
            }
            for row in r.rows
        ],
    }
