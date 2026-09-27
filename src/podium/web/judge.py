from fastapi import APIRouter, Depends, Request
from fastapi.responses import RedirectResponse
from sqlalchemy.orm import Session as DbSession

from podium.db import get_db
from podium.errors import PodiumError, ValidationFailed
from podium.models import Event, Role, User
from podium.security.csrf import verify_csrf
from podium.security.deps import EventContext, current_user, require_event_role, require_user
from podium.security.ratelimit import ip_hash
from podium.services import judges as judges_service
from podium.services import reviews as reviews_service
from podium.services import rubric as rubric_service
from podium.services import scoring
from podium.services.events import STAGE_LABELS, stage_of
from podium.web.rendering import is_htmx, render, wants_partial

router = APIRouter(include_in_schema=False)

NAV = [
    ("queue", "Queue", ""),
    ("compare", "Compare", "/compare"),
    ("records", "Your records", "/records"),
]


def _console(ctx: EventContext, active: str, **extra):
    stage = stage_of(ctx.event)
    base = f"/e/{ctx.event.slug}/judge"
    return {
        "event": ctx.event,
        "user": ctx.user,
        "ctx": ctx,
        "stage": stage.value,
        "stage_label": STAGE_LABELS[stage],
        "console": "judge",
        "nav_items": [(key, label, base + path) for key, label, path in NAV],
        "active": active,
        "judging_open": reviews_service.judging_is_open(ctx.event),
        **extra,
    }


# --- invitations --------------------------------------------------------------------------------


@router.get("/judge-invite/{token}")
def invite_page(
    request: Request,
    token: str,
    db: DbSession = Depends(get_db),
    user: User | None = Depends(current_user),
):
    invite = judges_service.invite_by_token(db, token)
    event = db.get(Event, invite.event_id)
    tracks = [t for t in event.tracks if t.public_id in invite.track_public_ids]
    return render(
        request,
        "judge/invite.html",
        title=f"Judge {event.name}",
        event=event,
        user=user,
        invite=invite,
        tracks=tracks,
        token=token,
        next=f"/judge-invite/{token}",
        accepted=invite.accepted_at is not None
        and invite.accepted_user_id == (user.id if user else None),
        refusal=judges_service.invite_refusal(db, invite, user) if user else None,
    )


@router.post("/judge-invite/{token}", dependencies=[Depends(verify_csrf)])
def invite_accept(
    request: Request,
    token: str,
    db: DbSession = Depends(get_db),
    user: User = Depends(require_user),
):
    invite = judges_service.invite_by_token(db, token)
    event = judges_service.accept_invite(db, invite, user)
    return RedirectResponse(f"/e/{event.slug}/judge?saved=joined", status_code=303)


# --- queue and scoring ----------------------------------------------------------------------------


@router.get("/e/{slug}/judge")
def queue_page(
    request: Request,
    ctx: EventContext = Depends(require_event_role(Role.judge)),
    db: DbSession = Depends(get_db),
):
    items = reviews_service.queue(db, ctx.event, ctx.user)
    groups = {"todo": [], "in_progress": [], "done": []}
    for item in items:
        groups[item.state].append(item)
    return render(
        request,
        "judge/queue.html",
        title="Judge queue",
        **_console(ctx, "queue", items=items, groups=groups),
    )


def _review_ctx(ctx, db, assignment, review, values=None, errors=None, error=""):
    criteria = rubric_service.criteria(db, ctx.event)
    scores = reviews_service.scores_of(review)
    by_public = {c.public_id: scores.get(c.id) for c in criteria}
    if values:
        by_public.update(
            {k: v for k, v in values.items() if k in by_public and v not in (None, "")}
        )
    total = scoring.raw_score(
        {
            c.id: int(v)
            for c in criteria
            for k, v in by_public.items()
            if k == c.public_id and v not in (None, "")
        },
        criteria,
    )
    items = reviews_service.queue(db, ctx.event, ctx.user)
    idx = next((i for i, it in enumerate(items) if it.assignment.id == assignment.id), 0)
    next_todo = _next_todo(db, ctx.event, ctx.user, assignment.project.public_id)
    prev_item = items[idx - 1] if idx > 0 else None
    next_item = items[idx + 1] if idx + 1 < len(items) else None
    return _console(
        ctx,
        "queue",
        assignment=assignment,
        project=assignment.project,
        review=review,
        criteria=criteria,
        values=by_public,
        total=total,
        errors=errors or {},
        error=error,
        position=idx + 1,
        count=len(items),
        prev_item=prev_item,
        next_item=next_item,
        next_todo=next_todo,
        done_count=sum(1 for item in items if item.state == "done"),
        submitted=review is not None and review.status.value == "submitted",
        comment=(values or {}).get("comment", review.comment if review else ""),
    )


@router.get("/e/{slug}/judge/review/{pid}")
def review_page(
    request: Request,
    pid: str,
    ctx: EventContext = Depends(require_event_role(Role.judge)),
    db: DbSession = Depends(get_db),
):
    assignment = reviews_service.assignment_for(db, ctx.event, ctx.user, pid)
    review = reviews_service.review_for(db, assignment)
    context = _review_ctx(ctx, db, assignment, review)
    prev = request.query_params.get("prev")
    if prev:  # "Submit & next" landed here: name the review that was just submitted
        items = reviews_service.queue(db, ctx.event, ctx.user)
        context["prev_title"] = next(
            (i.project.title for i in items if i.project.public_id == prev), None
        )
    return render(
        request,
        "judge/review.html",
        title=f"Review · {assignment.project.title}",
        **context,
    )


@router.post("/e/{slug}/judge/review/{pid}", dependencies=[Depends(verify_csrf)])
async def review_save(
    request: Request,
    pid: str,
    ctx: EventContext = Depends(require_event_role(Role.judge)),
    db: DbSession = Depends(get_db),
):
    form = await request.form()
    action = str(form.get("action", "draft"))
    assignment = reviews_service.assignment_for(db, ctx.event, ctx.user, pid)
    raw = {k: str(v) for k, v in form.items() if k.startswith("crt_")}
    comment = str(form.get("comment", ""))
    try:
        if action == "reopen":
            reviews_service.reopen(db, ctx.event, assignment, ctx.user)
        else:
            reviews_service.save(
                db,
                ctx.event,
                assignment,
                ctx.user,
                raw,
                comment,
                submit=action in ("submit", "submit_next"),
                ip_hash=ip_hash(request),
            )
    except ValidationFailed as exc:
        review = reviews_service.review_for(db, assignment)
        if is_htmx(request):
            return render(
                request,
                "partials/save_status.html",
                status_code=422,
                state="error",
                message=next(iter(exc.errors.values())),
            )
        return render(
            request,
            "judge/review.html",
            status_code=422,
            title=f"Error: Review · {assignment.project.title}",
            **_review_ctx(
                ctx, db, assignment, review, values={**raw, "comment": comment}, errors=exc.errors
            ),
        )
    except PodiumError as exc:
        review = reviews_service.review_for(db, assignment)
        if is_htmx(request):
            return render(
                request,
                "partials/save_status.html",
                status_code=exc.status_code,
                state="error",
                message=exc.message,
            )
        return render(
            request,
            "judge/review.html",
            status_code=exc.status_code,
            title="Review",
            **_review_ctx(
                ctx, db, assignment, review, values={**raw, "comment": comment}, error=exc.message
            ),
        )
    if is_htmx(request):
        return render(request, "partials/save_status.html", state="saved", message="Draft saved")
    if action == "submit_next":
        following = _next_todo(db, ctx.event, ctx.user, pid)
        if following is not None:
            return RedirectResponse(
                f"/e/{ctx.event.slug}/judge/review/{following.project.public_id}"
                f"?saved=submitted&prev={pid}",
                status_code=303,
            )
        return RedirectResponse(f"/e/{ctx.event.slug}/judge?done=1", status_code=303)
    saved = {"reopen": "reopened", "submit": "submitted"}.get(action, "draft")
    return RedirectResponse(
        f"/e/{ctx.event.slug}/judge/review/{pid}?saved={saved}", status_code=303
    )


def _next_todo(db, event, judge, current_pid: str):
    """The next queue item that still needs a review, starting after the current one and
    wrapping around; None when everything is submitted."""
    items = reviews_service.queue(db, event, judge)
    todo = [i for i in items if i.state != "done" and i.project.public_id != current_pid]
    if not todo:
        return None
    after = [
        i
        for i in todo
        if i.assignment.id
        > next((j.assignment.id for j in items if j.project.public_id == current_pid), 0)
    ]
    return (after or todo)[0]


# --- pairwise compare mode --------------------------------------------------------------------


@router.get("/e/{slug}/judge/compare")
def compare_page(
    request: Request,
    ctx: EventContext = Depends(require_event_role(Role.judge)),
    db: DbSession = Depends(get_db),
):
    from podium.services import pairwise

    state = pairwise.next_pair(db, ctx.event, ctx.user)
    template = "partials/compare_card.html" if wants_partial(request) else "judge/compare.html"
    return render(
        request,
        template,
        title="Compare projects",
        **_console(ctx, "compare", state=state, error=""),
    )


@router.post("/e/{slug}/judge/compare", dependencies=[Depends(verify_csrf)])
async def compare_submit(
    request: Request,
    ctx: EventContext = Depends(require_event_role(Role.judge)),
    db: DbSession = Depends(get_db),
):
    from podium.services import pairwise

    form = await request.form()
    action = str(form.get("action", ""))
    error = ""
    try:
        if action == "undo":
            pairwise.undo_last(db, ctx.event, ctx.user)
        elif action == "skip":
            pairwise.record(
                db,
                ctx.event,
                ctx.user,
                str(form.get("a", "")),
                str(form.get("b", "")),
                None,
                str(form.get("skip_reason", "cannot_decide")),
                ip_hash=ip_hash(request),
            )
        else:
            pairwise.record(
                db,
                ctx.event,
                ctx.user,
                str(form.get("a", "")),
                str(form.get("b", "")),
                str(form.get("winner", "")),
                ip_hash=ip_hash(request),
            )
    except PodiumError as exc:
        error = exc.message
    state = pairwise.next_pair(db, ctx.event, ctx.user)
    if is_htmx(request):
        return render(
            request,
            "partials/compare_card.html",
            status_code=200,
            **_console(ctx, "compare", state=state, error=error),
        )
    return RedirectResponse(f"/e/{ctx.event.slug}/judge/compare", status_code=303)
