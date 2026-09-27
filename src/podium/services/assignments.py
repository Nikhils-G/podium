"""Who judges what. Manual assignment plus a balanced, track-aware, deterministic auto-assigner
that previews before it applies and reports what it could not place."""

import random
from dataclasses import dataclass, field

from sqlalchemy import select
from sqlalchemy.orm import Session as DbSession

from podium.errors import Closed, Conflict, NotFound
from podium.models import (
    Assignment,
    AssignmentMethod,
    AssignmentStatus,
    Event,
    EventRole,
    JudgeTrack,
    Project,
    ProjectStatus,
    Role,
    TeamMember,
    User,
)
from podium.services import audit, webhooks


@dataclass
class PlanEntry:
    judge: User
    project: Project
    fallback: bool  # judge was outside the project's track (no eligible judge in it)


@dataclass
class Plan:
    reviews_per_project: int
    seed: int
    entries: list[PlanEntry] = field(default_factory=list)
    loads_after: dict[int, int] = field(default_factory=dict)  # judge_id -> total assignments
    shortfalls: list[tuple[Project, int]] = field(default_factory=list)  # project, still missing
    judges: int = 0
    projects: int = 0


def judge_pool(db: DbSession, event: Event) -> tuple[list[User], dict[int, set[int]]]:
    judges = (
        db.execute(
            select(User)
            .join(EventRole, EventRole.user_id == User.id)
            .where(EventRole.event_id == event.id, EventRole.role == Role.judge)
            .order_by(User.id)
        )
        .scalars()
        .all()
    )
    tracks: dict[int, set[int]] = {}
    for user_id, track_id in db.execute(
        select(JudgeTrack.user_id, JudgeTrack.track_id).where(JudgeTrack.event_id == event.id)
    ).all():
        tracks.setdefault(user_id, set()).add(track_id)
    return judges, tracks


def judgeable_projects(db: DbSession, event: Event) -> list[Project]:
    return list(
        db.execute(
            select(Project)
            .where(Project.event_id == event.id, Project.status == ProjectStatus.submitted)
            .order_by(Project.id)
        ).scalars()
    )


def _conflicts(db: DbSession, event: Event) -> set[tuple[int, int]]:
    """(judge_id, project_id) pairs where the judge is on the project's team. Structurally
    impossible under the one-role-per-event rule, but checked anyway."""
    pairs = set()
    for user_id, project_id in db.execute(
        select(TeamMember.user_id, Project.id)
        .join(Project, Project.team_id == TeamMember.team_id)
        .where(Project.event_id == event.id)
    ).all():
        pairs.add((user_id, project_id))
    return pairs


def build_plan(db: DbSession, event: Event, *, reviews_per_project: int, seed: int) -> Plan:
    judges, judge_tracks = judge_pool(db, event)
    projects = judgeable_projects(db, event)
    plan = Plan(
        reviews_per_project=reviews_per_project,
        seed=seed,
        judges=len(judges),
        projects=len(projects),
    )
    if not judges or not projects:
        return plan
    existing: dict[int, set[int]] = {}  # project_id -> judge_ids already assigned
    loads: dict[int, int] = {j.id: 0 for j in judges}
    for judge_id, project_id in db.execute(
        select(Assignment.judge_id, Assignment.project_id).where(Assignment.event_id == event.id)
    ).all():
        existing.setdefault(project_id, set()).add(judge_id)
        if judge_id in loads:
            loads[judge_id] += 1
    conflicts = _conflicts(db, event)
    rng = random.Random(seed)
    order = projects[:]
    rng.shuffle(order)
    # projects with the fewest existing reviews first, so shortfalls are filled before extras
    order.sort(key=lambda p: len(existing.get(p.id, ())))
    for project in order:
        need = reviews_per_project - len(existing.get(project.id, ()))
        if need <= 0:
            continue
        in_track = [j for j in judges if project.track_id in judge_tracks.get(j.id, set())]
        any_track = [j for j in judges if not judge_tracks.get(j.id)]
        pool, fallback = in_track + any_track, False
        if not pool:
            pool, fallback = judges, True
        candidates = [
            j
            for j in pool
            if j.id not in existing.get(project.id, set()) and (j.id, project.id) not in conflicts
        ]
        rng.shuffle(candidates)
        candidates.sort(key=lambda j: loads[j.id])
        chosen = candidates[:need]
        for judge in chosen:
            plan.entries.append(PlanEntry(judge=judge, project=project, fallback=fallback))
            existing.setdefault(project.id, set()).add(judge.id)
            loads[judge.id] += 1
        if len(chosen) < need:
            plan.shortfalls.append((project, need - len(chosen)))
    plan.loads_after = loads
    return plan


def _assignments_open(event: Event) -> None:
    """Once judging has closed the reviews are final, so who judges what can't change."""
    if event.judging_closed_at is not None:
        raise Closed("Judging is closed, so assignments can't change. Reopen judging first.")


def apply_plan(db: DbSession, event: Event, organizer: User, plan: Plan) -> int:
    _assignments_open(event)
    created = 0
    for entry in plan.entries:
        exists = db.execute(
            select(Assignment.id).where(
                Assignment.judge_id == entry.judge.id, Assignment.project_id == entry.project.id
            )
        ).scalar()
        if exists is None:
            db.add(
                Assignment(
                    event_id=event.id,
                    judge_id=entry.judge.id,
                    project_id=entry.project.id,
                    method=AssignmentMethod.auto,
                    assigned_by=organizer.id,
                )
            )
            created += 1
    if created:
        webhooks.emit(
            db,
            event,
            "assignment.created",
            {
                "created": created,
                "method": "auto",
                "assignments": [
                    {"judge": e.judge.public_id, "project": e.project.public_id}
                    for e in plan.entries
                ],
            },
        )
    audit.record(
        db,
        "assignments.auto_applied",
        "event",
        event.public_id,
        event_id=event.id,
        actor_id=organizer.id,
        meta={
            "created": created,
            "seed": plan.seed,
            "reviews_per_project": plan.reviews_per_project,
        },
    )
    db.commit()
    return created


def assign_manually(
    db: DbSession, event: Event, organizer: User, judge: User, project_public_ids: list[str]
) -> int:
    _assignments_open(event)
    projects = (
        db.execute(
            select(Project).where(
                Project.event_id == event.id, Project.public_id.in_(project_public_ids)
            )
        )
        .scalars()
        .all()
    )
    if not projects:
        raise NotFound("Choose at least one project.")
    conflicts = _conflicts(db, event)
    created = 0
    for project in projects:
        if (judge.id, project.id) in conflicts:
            raise Conflict(f"{judge.name} is on the team behind “{project.title}”.")
        exists = db.execute(
            select(Assignment.id).where(
                Assignment.judge_id == judge.id, Assignment.project_id == project.id
            )
        ).scalar()
        if exists is None:
            db.add(
                Assignment(
                    event_id=event.id,
                    judge_id=judge.id,
                    project_id=project.id,
                    method=AssignmentMethod.manual,
                    assigned_by=organizer.id,
                )
            )
            created += 1
    audit.record(
        db,
        "assignments.manual",
        "user",
        judge.public_id,
        event_id=event.id,
        actor_id=organizer.id,
        meta={"projects": [p.public_id for p in projects], "created": created},
    )
    db.commit()
    return created


def remove_assignment(db: DbSession, event: Event, organizer: User, assignment_id: int) -> None:
    _assignments_open(event)
    assignment = db.get(Assignment, assignment_id)
    if assignment is None or assignment.event_id != event.id:
        raise NotFound("No such assignment.")
    if assignment.status == AssignmentStatus.done:
        raise Conflict("This assignment has a submitted review. It can't be removed.")
    audit.record(
        db,
        "assignments.removed",
        "assignment",
        assignment.id,
        event_id=event.id,
        actor_id=organizer.id,
        meta={
            "judge": assignment.judge.public_id,
            "project": assignment.project.public_id,
            "draft_discarded": assignment.review is not None,
        },
    )
    # reviews.assignment_id is NOT NULL and has no ORM cascade: drop the draft first (its
    # score items cascade from the review)
    if assignment.review is not None:
        db.delete(assignment.review)
    db.delete(assignment)
    db.commit()


@dataclass
class JudgeLoad:
    judge: User
    tracks: list
    assignments: list[Assignment]


def by_judge(db: DbSession, event: Event) -> list[JudgeLoad]:
    from podium.services.judges import list_judges

    rows = list_judges(db, event)
    assignments = (
        db.execute(
            select(Assignment).where(Assignment.event_id == event.id).order_by(Assignment.id)
        )
        .scalars()
        .all()
    )
    grouped: dict[int, list[Assignment]] = {}
    for a in assignments:
        grouped.setdefault(a.judge_id, []).append(a)
    return [
        JudgeLoad(judge=r.user, tracks=r.tracks, assignments=grouped.get(r.user.id, []))
        for r in rows
    ]
