from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import RedirectResponse
from sqlalchemy.orm import Session as DbSession

from podium.db import get_db
from podium.errors import Closed, Conflict, PodiumError, ValidationFailed
from podium.models import Event, ProjectStatus, Role, User
from podium.security.csrf import verify_csrf
from podium.security.deps import (
    EventContext,
    current_user,
    load_event,
    require_user,
    submissions_are_open,
)
from podium.security.ratelimit import ip_hash
from podium.services import projects, teams
from podium.services.events import STAGE_LABELS, stage_of
from podium.services.voting import Voter
from podium.web.community import comments_context, current_voter, vote_context
from podium.web.rendering import render

router = APIRouter(include_in_schema=False)


def _base(ctx: EventContext, **extra):
    stage = stage_of(ctx.event)
    return {
        "event": ctx.event,
        "user": ctx.user,
        "ctx": ctx,
        "stage": stage.value,
        "stage_label": STAGE_LABELS[stage],
        "open": submissions_are_open(ctx.event),
        **extra,
    }


# --- teams -------------------------------------------------------------------------------------


@router.get("/e/{slug}/team")
def team_page(
    request: Request,
    ctx: EventContext = Depends(load_event),
    db: DbSession = Depends(get_db),
    saved: str = "",
):
    if ctx.user is None:
        return RedirectResponse(f"/login?next=/e/{ctx.event.slug}/team", status_code=303)
    team = teams.team_for(db, ctx.event, ctx.user)
    view = teams.team_view(db, team, ctx.user) if team else None
    return render(
        request,
        "participant/team.html",
        title="Your team",
        nav="team",
        **_base(ctx, view=view, errors={}, values={}, error="", saved=saved),
    )


@router.post("/e/{slug}/team/invite/regenerate", dependencies=[Depends(verify_csrf)])
def team_invite_regenerate(
    request: Request,
    ctx: EventContext = Depends(load_event),
    user: User = Depends(require_user),
    db: DbSession = Depends(get_db),
):
    teams.regenerate_invite(db, ctx.event, user, ip_hash=ip_hash(request))
    return RedirectResponse(f"/e/{ctx.event.slug}/team?saved=invite", status_code=303)


@router.post("/e/{slug}/team", dependencies=[Depends(verify_csrf)])
def team_create(
    request: Request,
    name: str = Form(""),
    ctx: EventContext = Depends(load_event),
    user: User = Depends(require_user),
    db: DbSession = Depends(get_db),
):
    try:
        teams.create_team(db, ctx.event, user, name, ip_hash=ip_hash(request))
    except ValidationFailed as exc:
        return render(
            request,
            "participant/team.html",
            status_code=422,
            title="Your team",
            nav="team",
            **_base(ctx, view=None, errors=exc.errors, values={"name": name}, error=""),
        )
    except PodiumError as exc:
        return render(
            request,
            "participant/team.html",
            status_code=exc.status_code,
            title="Your team",
            nav="team",
            **_base(ctx, view=None, errors={}, values={"name": name}, error=exc.message),
        )
    return RedirectResponse(f"/e/{ctx.event.slug}/team", status_code=303)


@router.post("/e/{slug}/team/join", dependencies=[Depends(verify_csrf)])
def team_join_by_code(
    request: Request,
    code: str = Form(""),
    ctx: EventContext = Depends(load_event),
    user: User = Depends(require_user),
    db: DbSession = Depends(get_db),
):
    code = code.strip().rsplit("/", 1)[-1]
    try:
        team = teams.team_by_invite(db, code)
        if team.event_id != ctx.event.id:
            raise Conflict("That invite belongs to a different event.")
        teams.join_team(db, team, user, ip_hash=ip_hash(request))
    except PodiumError as exc:
        return render(
            request,
            "participant/team.html",
            status_code=exc.status_code,
            title="Your team",
            nav="team",
            **_base(ctx, view=None, errors={}, values={"code": code}, error=exc.message),
        )
    return RedirectResponse(f"/e/{ctx.event.slug}/team", status_code=303)


@router.post("/e/{slug}/team/leave", dependencies=[Depends(verify_csrf)])
def team_leave(
    request: Request,
    ctx: EventContext = Depends(load_event),
    user: User = Depends(require_user),
    db: DbSession = Depends(get_db),
):
    team = teams.team_for(db, ctx.event, user)
    if team is not None:
        teams.leave_team(db, team, user, ip_hash=ip_hash(request))
    return RedirectResponse(f"/e/{ctx.event.slug}/team", status_code=303)


@router.get("/join/{code}")
def join_page(
    request: Request,
    code: str,
    db: DbSession = Depends(get_db),
    user: User | None = Depends(current_user),
):
    team = teams.team_by_invite(db, code)
    event = db.get(Event, team.event_id)
    view = teams.team_view(db, team, user)
    already = user is not None and any(u.id == user.id for u, _ in view.members)
    return render(
        request,
        "participant/join.html",
        title=f"Join {team.name}",
        event=event,
        user=user,
        team=team,
        view=view,
        already=already,
        code=code,
        next=f"/join/{code}",
    )


@router.post("/join/{code}", dependencies=[Depends(verify_csrf)])
def join_submit(
    request: Request, code: str, db: DbSession = Depends(get_db), user: User = Depends(require_user)
):
    team = teams.team_by_invite(db, code)
    event = db.get(Event, team.event_id)
    teams.join_team(db, team, user, ip_hash=ip_hash(request))
    return RedirectResponse(f"/e/{event.slug}/team", status_code=303)


# --- projects: submit / edit / withdraw ----------------------------------------------------------


def _form_values(project) -> dict:
    if project is None:
        return {
            k: ""
            for k in (
                "title",
                "summary",
                "description",
                "repo_url",
                "demo_url",
                "video_url",
                "track",
            )
        }
    return {
        "title": project.title,
        "summary": project.summary,
        "description": project.description,
        "repo_url": project.repo_url,
        "demo_url": project.demo_url,
        "video_url": project.video_url,
        "track": project.track.public_id if project.track else "",
    }


def _submit_title(project) -> str:
    if project is None:
        return "Submit a project"
    return "Your draft" if project.status == ProjectStatus.draft else "Your submission"


def _submit_form(request, ctx, view, project, data, *, errors, error, editable, status_code):
    return render(
        request,
        "participant/submit.html",
        status_code=status_code,
        title=_submit_title(project),
        nav="submit",
        **_base(
            ctx,
            view=view,
            project=project,
            values=data,
            errors=errors,
            error=error,
            editable=editable,
        ),
    )


SAVED_MESSAGES = {
    "draft": (
        "info",
        "Draft saved. Only your team and the organizers can see it until you submit.",
    ),
    "submitted": ("success", "Project submitted. It's in the gallery and the judging pool now."),
    "updated": ("success", "Changes saved."),
    "unsubmitted": ("warning", "Returned to draft. It left the gallery until you submit it again."),
    "withdrawn": (
        "warning",
        "Project withdrawn. It's out of the gallery and judging; you can restore it.",
    ),
    "restored": ("success", "Project restored. It's back in the gallery."),
}


@router.get("/e/{slug}/submit")
def submit_page(
    request: Request, ctx: EventContext = Depends(load_event), db: DbSession = Depends(get_db)
):
    if ctx.user is None:
        return RedirectResponse(f"/login?next=/e/{ctx.event.slug}/submit", status_code=303)
    team = teams.team_for(db, ctx.event, ctx.user)
    view = teams.team_view(db, team, ctx.user) if team else None
    project = view.project if view else None
    editable = (
        project is None
        and submissions_are_open(ctx.event)
        or (project is not None and projects.editable_now(ctx.event, project))
    )
    return _submit_form(
        request,
        ctx,
        view,
        project,
        _form_values(project),
        errors={},
        error="",
        editable=editable,
        status_code=200,
    )


@router.post("/e/{slug}/submit", dependencies=[Depends(verify_csrf)])
async def submit_save(
    request: Request,
    ctx: EventContext = Depends(load_event),
    user: User = Depends(require_user),
    db: DbSession = Depends(get_db),
):
    form = await request.form()
    data = {
        k: str(form.get(k, ""))
        for k in ("title", "summary", "description", "repo_url", "demo_url", "video_url", "track")
    }
    action = str(form.get("action", "save"))
    team = teams.team_for(db, ctx.event, user)
    view = teams.team_view(db, team, user) if team else None
    project = view.project if view else None
    try:
        if project is None:
            if team is None:
                raise Conflict("Create or join a team before submitting a project.")
            if not submissions_are_open(ctx.event):
                raise Closed("Submissions are closed for this event.")
            project = projects.create_project(
                db, ctx.event, user, data, submit=(action == "submit"), ip_hash=ip_hash(request)
            )
        else:
            submit = True if action == "submit" else (False if action == "unsubmit" else None)
            projects.update_project(
                db, ctx.event, project, user, data, submit=submit, ip_hash=ip_hash(request)
            )
    except ValidationFailed as exc:
        return _submit_form(
            request,
            ctx,
            view,
            project,
            data,
            errors=exc.errors,
            error="",
            editable=True,
            status_code=422,
        )
    except PodiumError as exc:
        return _submit_form(
            request,
            ctx,
            view,
            project,
            data,
            errors={},
            error=exc.message,
            editable=False,
            status_code=exc.status_code,
        )
    if action == "submit":
        saved = "submitted"
    elif action == "unsubmit":
        saved = "unsubmitted"
    else:
        saved = "updated" if project.status == ProjectStatus.submitted else "draft"
    return RedirectResponse(
        f"/e/{ctx.event.slug}/projects/{project.public_id}?saved={saved}", status_code=303
    )


@router.post("/e/{slug}/projects/{pid}/submit", dependencies=[Depends(verify_csrf)])
def submit_draft(
    request: Request,
    pid: str,
    ctx: EventContext = Depends(load_event),
    user: User = Depends(require_user),
    db: DbSession = Depends(get_db),
):
    """The Submit button on a draft's own page: submit it as it is, no field changes."""
    project = projects.get_project(db, ctx.event, pid, user, organizer=ctx.is_organizer)
    view = teams.team_view(db, project.team, user)
    data = _form_values(project)
    try:
        projects.update_project(
            db, ctx.event, project, user, data, submit=True, ip_hash=ip_hash(request)
        )
    except ValidationFailed as exc:
        return _submit_form(
            request,
            ctx,
            view,
            project,
            data,
            errors=exc.errors,
            error="Fix these before submitting.",
            editable=True,
            status_code=422,
        )
    except PodiumError as exc:
        return _submit_form(
            request,
            ctx,
            view,
            project,
            data,
            errors={},
            error=exc.message,
            editable=False,
            status_code=exc.status_code,
        )
    return RedirectResponse(f"/e/{ctx.event.slug}/projects/{pid}?saved=submitted", status_code=303)


@router.post("/e/{slug}/projects/{pid}/withdraw", dependencies=[Depends(verify_csrf)])
def withdraw(
    request: Request,
    pid: str,
    ctx: EventContext = Depends(load_event),
    user: User = Depends(require_user),
    db: DbSession = Depends(get_db),
):
    project = projects.get_project(db, ctx.event, pid, user, organizer=ctx.is_organizer)
    projects.withdraw_project(
        db, ctx.event, project, user, organizer=ctx.is_organizer, ip_hash=ip_hash(request)
    )
    return RedirectResponse(f"/e/{ctx.event.slug}/projects/{pid}?saved=withdrawn", status_code=303)


@router.post("/e/{slug}/projects/{pid}/restore", dependencies=[Depends(verify_csrf)])
def restore(
    request: Request,
    pid: str,
    ctx: EventContext = Depends(load_event),
    user: User = Depends(require_user),
    db: DbSession = Depends(get_db),
):
    project = projects.get_project(db, ctx.event, pid, user, organizer=ctx.is_organizer)
    projects.withdraw_project(
        db,
        ctx.event,
        project,
        user,
        restore=True,
        organizer=ctx.is_organizer,
        ip_hash=ip_hash(request),
    )
    return RedirectResponse(f"/e/{ctx.event.slug}/projects/{pid}?saved=restored", status_code=303)


# --- public project page --------------------------------------------------------------------------


@router.get("/e/{slug}/projects/{pid}")
def project_page(
    request: Request,
    pid: str,
    ctx: EventContext = Depends(load_event),
    db: DbSession = Depends(get_db),
    voter: Voter | None = Depends(current_voter),
    saved: str = "",
):
    project = projects.get_project(db, ctx.event, pid, ctx.user, organizer=ctx.is_organizer)
    member = projects.is_member(db, project, ctx.user)
    view = teams.team_view(db, project.team, ctx.user)
    return render(
        request,
        "public/project.html",
        title=project.title,
        nav="gallery",
        **_base(
            ctx,
            project=project,
            member=member,
            team_view=view,
            editable=member and projects.editable_now(ctx.event, project),
            is_draft=project.status == ProjectStatus.draft,
            is_withdrawn=project.status == ProjectStatus.withdrawn,
            saved_alert=SAVED_MESSAGES.get(saved) if member or ctx.is_organizer else None,
            meta_description=(project.summary or f"{project.title} by {project.team.name}")[:160],
            role=ctx.role,
            Role=Role,
            vote=vote_context(db, ctx, project, voter),
            comments_ctx=comments_context(db, ctx, project),
        ),
    )
