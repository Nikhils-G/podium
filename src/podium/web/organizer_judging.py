from fastapi import APIRouter, Depends, Form, Query, Request
from fastapi.responses import RedirectResponse
from sqlalchemy.orm import Session as DbSession

from podium.config import Settings, get_settings
from podium.db import get_db
from podium.errors import PodiumError
from podium.models import NormalizationMethod, RankingBasis
from podium.security.csrf import verify_csrf
from podium.security.deps import EventContext, require_organizer
from podium.services import assignments as assignments_service
from podium.services import audit as audit_service
from podium.services import dashboard, exports, scoring
from podium.services import judges as judges_service
from podium.services import rubric as rubric_service
from podium.web.organizer import _console
from podium.web.rendering import is_htmx, render

router = APIRouter(include_in_schema=False)


# --- rubric -------------------------------------------------------------------------------------


def _rubric_ctx(ctx, db, **extra):
    defaults = {
        "criteria": rubric_service.criteria(db, ctx.event, include_archived=True),
        "locked": rubric_service.is_locked(ctx.event),
        "errors": {},
    }
    defaults.update(extra)
    return _console(ctx, "rubric", **defaults)


@router.get("/e/{slug}/organizer/rubric")
def rubric_page(
    request: Request,
    ctx: EventContext = Depends(require_organizer),
    db: DbSession = Depends(get_db),
):
    return render(request, "organizer/rubric.html", title="Rubric", **_rubric_ctx(ctx, db))


@router.post("/e/{slug}/organizer/rubric", dependencies=[Depends(verify_csrf)])
def rubric_add(
    request: Request,
    name: str = Form(""),
    description: str = Form(""),
    weight: str = Form("1"),
    min_score: str = Form("1"),
    max_score: str = Form("5"),
    ctx: EventContext = Depends(require_organizer),
    db: DbSession = Depends(get_db),
):
    try:
        rubric_service.add_criterion(
            db, ctx.event, ctx.user, name, description, weight, min_score, max_score
        )
    except PodiumError as exc:
        c = _rubric_ctx(ctx, db)
        c["errors"] = getattr(exc, "errors", None) or {"name": exc.message}
        return render(
            request, "organizer/rubric.html", status_code=exc.status_code, title="Rubric", **c
        )
    return RedirectResponse(f"/e/{ctx.event.slug}/organizer/rubric", status_code=303)


@router.post("/e/{slug}/organizer/rubric/{criterion_id}", dependencies=[Depends(verify_csrf)])
def rubric_update(
    request: Request,
    criterion_id: str,
    name: str = Form(None),
    description: str = Form(None),
    weight: str = Form(None),
    ctx: EventContext = Depends(require_organizer),
    db: DbSession = Depends(get_db),
):
    crit = rubric_service.get_criterion(db, ctx.event, criterion_id)
    try:
        rubric_service.update_criterion(
            db, ctx.event, ctx.user, crit, name=name, description=description, weight=weight
        )
    except PodiumError as exc:
        c = _rubric_ctx(ctx, db)
        c["errors"] = getattr(exc, "errors", None) or {"row_" + criterion_id: exc.message}
        return render(
            request, "organizer/rubric.html", status_code=exc.status_code, title="Rubric", **c
        )
    return RedirectResponse(f"/e/{ctx.event.slug}/organizer/rubric", status_code=303)


@router.post(
    "/e/{slug}/organizer/rubric/{criterion_id}/archive", dependencies=[Depends(verify_csrf)]
)
def rubric_archive(
    request: Request,
    criterion_id: str,
    ctx: EventContext = Depends(require_organizer),
    db: DbSession = Depends(get_db),
):
    crit = rubric_service.get_criterion(db, ctx.event, criterion_id)
    try:
        rubric_service.archive_criterion(db, ctx.event, ctx.user, crit)
    except PodiumError as exc:
        c = _rubric_ctx(ctx, db)
        c["errors"] = {"row_" + criterion_id: exc.message}
        return render(
            request, "organizer/rubric.html", status_code=exc.status_code, title="Rubric", **c
        )
    return RedirectResponse(f"/e/{ctx.event.slug}/organizer/rubric", status_code=303)


# --- judges ---------------------------------------------------------------------------------------


def _judges_ctx(ctx, db, **extra):
    defaults = {
        "judges": judges_service.list_judges(db, ctx.event),
        "invites": judges_service.pending_invites(db, ctx.event),
        "errors": {},
        "link": None,
    }
    defaults.update(extra)
    return _console(ctx, "judges", **defaults)


@router.get("/e/{slug}/organizer/judges")
def judges_page(
    request: Request,
    ctx: EventContext = Depends(require_organizer),
    db: DbSession = Depends(get_db),
):
    return render(request, "organizer/judges.html", title="Judges", **_judges_ctx(ctx, db))


@router.post("/e/{slug}/organizer/judges/invite", dependencies=[Depends(verify_csrf)])
async def judges_invite(
    request: Request,
    ctx: EventContext = Depends(require_organizer),
    db: DbSession = Depends(get_db),
    settings: Settings = Depends(get_settings),
):
    form = await request.form()
    email = str(form.get("email", ""))
    tracks = [str(v) for v in form.getlist("tracks")]
    try:
        invite, token = judges_service.invite_judge(db, ctx.event, ctx.user, email, tracks)
    except PodiumError as exc:
        c = _judges_ctx(ctx, db)
        c["errors"] = getattr(exc, "errors", None) or {"email": exc.message}
        return render(
            request, "organizer/judges.html", status_code=exc.status_code, title="Judges", **c
        )
    link = f"{settings.base_url}/judge-invite/{token}"
    return render(
        request,
        "organizer/judges.html",
        title="Judges",
        **_judges_ctx(ctx, db, link=link, invited=invite.email),
    )


@router.post("/e/{slug}/organizer/judges/{judge_id}/remove", dependencies=[Depends(verify_csrf)])
def judges_remove(
    request: Request,
    judge_id: str,
    ctx: EventContext = Depends(require_organizer),
    db: DbSession = Depends(get_db),
):
    try:
        judges_service.remove_judge(db, ctx.event, ctx.user, judge_id)
    except PodiumError as exc:
        c = _judges_ctx(ctx, db)
        c["errors"] = {"judge_" + judge_id: exc.message}
        return render(
            request, "organizer/judges.html", status_code=exc.status_code, title="Judges", **c
        )
    return RedirectResponse(f"/e/{ctx.event.slug}/organizer/judges", status_code=303)


@router.post("/e/{slug}/organizer/judges/{judge_id}/tracks", dependencies=[Depends(verify_csrf)])
async def judges_tracks(
    request: Request,
    judge_id: str,
    ctx: EventContext = Depends(require_organizer),
    db: DbSession = Depends(get_db),
):
    form = await request.form()
    judge = judges_service.judge_by_public_id(db, ctx.event, judge_id)
    judges_service.set_judge_tracks(db, ctx.event, judge, [str(v) for v in form.getlist("tracks")])
    return RedirectResponse(f"/e/{ctx.event.slug}/organizer/judges", status_code=303)


# --- assignments ----------------------------------------------------------------------------------


def _assign_ctx(ctx, db, **extra):
    defaults = {
        "loads": assignments_service.by_judge(db, ctx.event),
        "projects": assignments_service.judgeable_projects(db, ctx.event),
        "plan": None,
        "errors": {},
        "reviews_per_project": ctx.event.reviews_per_project,
        "seed": 1,
    }
    defaults.update(extra)
    return _console(ctx, "assignments", **defaults)


@router.get("/e/{slug}/organizer/assignments")
def assignments_page(
    request: Request,
    ctx: EventContext = Depends(require_organizer),
    db: DbSession = Depends(get_db),
):
    return render(
        request, "organizer/assignments.html", title="Assignments", **_assign_ctx(ctx, db)
    )


@router.post("/e/{slug}/organizer/assignments/manual", dependencies=[Depends(verify_csrf)])
async def assignments_manual(
    request: Request,
    ctx: EventContext = Depends(require_organizer),
    db: DbSession = Depends(get_db),
):
    form = await request.form()
    judge_id = str(form.get("judge", ""))
    projects = [str(v) for v in form.getlist("projects")]
    try:
        judge = judges_service.judge_by_public_id(db, ctx.event, judge_id)
        assignments_service.assign_manually(db, ctx.event, ctx.user, judge, projects)
    except PodiumError as exc:
        c = _assign_ctx(ctx, db)
        c["errors"] = {"manual": exc.message}
        return render(
            request,
            "organizer/assignments.html",
            status_code=exc.status_code,
            title="Assignments",
            **c,
        )
    return RedirectResponse(f"/e/{ctx.event.slug}/organizer/assignments", status_code=303)


@router.post("/e/{slug}/organizer/assignments/auto/preview", dependencies=[Depends(verify_csrf)])
def assignments_preview(
    request: Request,
    reviews_per_project: int = Form(3),
    seed: int = Form(1),
    ctx: EventContext = Depends(require_organizer),
    db: DbSession = Depends(get_db),
):
    plan = assignments_service.build_plan(
        db, ctx.event, reviews_per_project=max(1, min(20, reviews_per_project)), seed=seed
    )
    template = "partials/assign_preview.html" if is_htmx(request) else "organizer/assignments.html"
    return render(
        request,
        template,
        title="Assignments",
        **_assign_ctx(ctx, db, plan=plan, reviews_per_project=plan.reviews_per_project, seed=seed),
    )


@router.post("/e/{slug}/organizer/assignments/auto/apply", dependencies=[Depends(verify_csrf)])
def assignments_apply(
    reviews_per_project: int = Form(3),
    seed: int = Form(1),
    ctx: EventContext = Depends(require_organizer),
    db: DbSession = Depends(get_db),
):
    plan = assignments_service.build_plan(
        db, ctx.event, reviews_per_project=max(1, min(20, reviews_per_project)), seed=seed
    )
    assignments_service.apply_plan(db, ctx.event, ctx.user, plan)
    return RedirectResponse(
        f"/e/{ctx.event.slug}/organizer/assignments?applied={len(plan.entries)}", status_code=303
    )


@router.post(
    "/e/{slug}/organizer/assignments/{assignment_id}/remove", dependencies=[Depends(verify_csrf)]
)
def assignments_remove(
    request: Request,
    assignment_id: int,
    ctx: EventContext = Depends(require_organizer),
    db: DbSession = Depends(get_db),
):
    try:
        assignments_service.remove_assignment(db, ctx.event, ctx.user, assignment_id)
    except PodiumError as exc:
        c = _assign_ctx(ctx, db)
        c["errors"] = {"manual": exc.message}
        return render(
            request,
            "organizer/assignments.html",
            status_code=exc.status_code,
            title="Assignments",
            **c,
        )
    return RedirectResponse(f"/e/{ctx.event.slug}/organizer/assignments", status_code=303)


# --- progress -------------------------------------------------------------------------------------


@router.get("/e/{slug}/organizer/progress")
def progress_page(
    request: Request,
    ctx: EventContext = Depends(require_organizer),
    db: DbSession = Depends(get_db),
):
    data = dashboard.progress(db, ctx.event)
    template = "partials/progress_body.html" if is_htmx(request) else "organizer/progress.html"
    return render(
        request, template, title="Judging progress", **_console(ctx, "progress", progress=data)
    )


# --- results --------------------------------------------------------------------------------------


@router.get("/e/{slug}/organizer/results")
def results_page(
    request: Request,
    ctx: EventContext = Depends(require_organizer),
    db: DbSession = Depends(get_db),
):
    from podium.services import pairwise

    results = scoring.compute(db, ctx.event, include_withdrawn=False)
    return render(
        request,
        "organizer/results.html",
        title="Results",
        **_console(ctx, "results", results=results, pairwise=pairwise.results(db, ctx.event)),
    )


@router.post("/e/{slug}/organizer/results/settings", dependencies=[Depends(verify_csrf)])
def results_settings(
    method: str = Form("zscore"),
    basis: str = Form("normalized"),
    ctx: EventContext = Depends(require_organizer),
    db: DbSession = Depends(get_db),
):
    ctx.event.normalization_method = (
        NormalizationMethod(method)
        if method in NormalizationMethod.__members__
        else NormalizationMethod.zscore
    )
    ctx.event.published_ranking = (
        RankingBasis(basis) if basis in RankingBasis.__members__ else RankingBasis.normalized
    )
    audit_service.record(
        db,
        "results.settings",
        "event",
        ctx.event.public_id,
        event_id=ctx.event.id,
        actor_id=ctx.user.id,
        meta={
            "method": ctx.event.normalization_method.value,
            "basis": ctx.event.published_ranking.value,
        },
    )
    db.commit()
    return RedirectResponse(f"/e/{ctx.event.slug}/organizer/results", status_code=303)


# --- audit ----------------------------------------------------------------------------------------


@router.get("/e/{slug}/organizer/audit")
def audit_page(
    request: Request,
    ctx: EventContext = Depends(require_organizer),
    db: DbSession = Depends(get_db),
    action: str = Query("", max_length=64),
    page: int = Query(1, ge=1),
):
    data = audit_service.list_entries(db, ctx.event.id, action=action, page=page)
    return render(
        request,
        "organizer/audit.html",
        title="Audit log",
        **_console(ctx, "audit", audit=data, action=action, export_names=exports.NAMES),
    )


@router.get("/e/{slug}/organizer/audit/verify")
def audit_verify(
    request: Request,
    ctx: EventContext = Depends(require_organizer),
    db: DbSession = Depends(get_db),
):
    report = audit_service.verify_chain(db)
    return render(request, "partials/audit_verify.html", **_console(ctx, "audit", report=report))
