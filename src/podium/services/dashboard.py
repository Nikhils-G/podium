"""Numbers, attention items and judging progress for the organizer console."""

from dataclasses import dataclass, field

from sqlalchemy import func, select
from sqlalchemy.orm import Session as DbSession

from podium.models import (
    Assignment,
    Event,
    EventRole,
    JudgeInvite,
    JudgeTrack,
    Project,
    ProjectStatus,
    Review,
    ReviewStatus,
    Role,
    Team,
    Track,
    User,
)
from podium.services.events import Stage, stage_of


@dataclass
class AttentionItem:
    kind: str  # info | warning | serious
    title: str
    detail: str
    href: str
    action: str


@dataclass
class Overview:
    stage: Stage
    projects_submitted: int
    projects_draft: int
    projects_withdrawn: int
    teams: int
    tracks: int
    judges: int
    participants: int
    attention: list[AttentionItem] = field(default_factory=list)


def overview(db: DbSession, event: Event) -> Overview:
    def count_projects(status: ProjectStatus) -> int:
        return db.execute(
            select(func.count())
            .select_from(Project)
            .where(Project.event_id == event.id, Project.status == status)
        ).scalar_one()

    def count_role(role: Role) -> int:
        return db.execute(
            select(func.count())
            .select_from(EventRole)
            .where(EventRole.event_id == event.id, EventRole.role == role)
        ).scalar_one()

    data = Overview(
        stage=stage_of(event),
        projects_submitted=count_projects(ProjectStatus.submitted),
        projects_draft=count_projects(ProjectStatus.draft),
        projects_withdrawn=count_projects(ProjectStatus.withdrawn),
        teams=db.execute(
            select(func.count()).select_from(Team).where(Team.event_id == event.id)
        ).scalar_one(),
        tracks=db.execute(
            select(func.count()).select_from(Track).where(Track.event_id == event.id)
        ).scalar_one(),
        judges=count_role(Role.judge),
        participants=count_role(Role.participant),
    )
    base = f"/e/{event.slug}/organizer"
    add = data.attention.append
    duplicates = (
        db.execute(
            select(Project).where(
                Project.event_id == event.id,
                Project.duplicate_of_id.isnot(None),
                Project.status != ProjectStatus.withdrawn,
            )
        )
        .scalars()
        .all()
    )
    for dup in duplicates:
        add(
            AttentionItem(
                "warning",
                "Possible duplicate submission",
                f"“{dup.title}” ({dup.public_id}) looks like a re-submission of "
                f"{dup.duplicate_of.public_id} by the same team.",
                f"/e/{event.slug}/projects/{dup.public_id}",
                "Review",
            )
        )
    if data.tracks == 0:
        add(
            AttentionItem(
                "info",
                "No tracks yet",
                "Add at least one track so teams can categorise their projects.",
                f"{base}/settings#tracks",
                "Add tracks",
            )
        )
    if not event.submissions_close_at:
        add(
            AttentionItem(
                "serious",
                "No submission deadline",
                "Without a close date the deadline can't be enforced.",
                f"{base}/settings",
                "Set dates",
            )
        )
    if not event.is_public:
        add(
            AttentionItem(
                "info",
                "Event is not public",
                "Only organizers can see it. Publish when you're ready.",
                f"{base}",
                "Publish event",
            )
        )
    prog = progress(db, event)
    if prog.total_assignments:
        for judge in prog.flat_judges:
            add(
                AttentionItem(
                    "warning",
                    "Judge scores every project identically",
                    f"{judge.name} gave the same total to all of their projects. Their reviews "
                    "carry no ranking information and are neutralised by normalization.",
                    f"{base}/results",
                    "View calibration",
                )
            )
        if prog.below_target:
            add(
                AttentionItem(
                    "warning",
                    f"{len(prog.below_target)} project(s) have fewer than "
                    f"{event.reviews_per_project} submitted reviews",
                    "Assign more judges or nudge the ones who haven't finished.",
                    f"{base}/progress",
                    "See which",
                )
            )
    pending = db.execute(
        select(func.count())
        .select_from(JudgeInvite)
        .where(JudgeInvite.event_id == event.id, JudgeInvite.accepted_at.is_(None))
    ).scalar_one()
    if pending:
        add(
            AttentionItem(
                "info",
                f"{pending} judge invitation(s) not accepted yet",
                "Judges can't be assigned projects until they accept.",
                f"{base}/judges",
                "See invites",
            )
        )
    return data


STEPS = [
    (Stage.draft, "Set up", "Dates, tracks, prizes, rubric"),
    (Stage.open, "Submissions", "Teams form and submit"),
    (Stage.closed, "Closed", "Deadline passed"),
    (Stage.judging, "Judging", "Judges score assigned projects"),
    (Stage.voting, "Voting", "Community votes"),
    (Stage.published, "Published", "Results are public"),
    (Stage.archived, "Archived", "Read-only"),
]

ORDER = {stage: i for i, (stage, _, _) in enumerate(STEPS)}


def step_index(stage: Stage) -> int:
    if stage == Stage.upcoming:
        return ORDER[Stage.open]
    return ORDER.get(stage, 0)


def next_actions(event: Event, stage: Stage) -> list[tuple[str, str, str]]:
    """(action key, button label, consequence) the organizer can take right now."""
    actions: list[tuple[str, str, str]] = []
    if event.archived_at is not None:
        return actions
    if not event.is_public:
        actions.append(
            (
                "publish_event",
                "Publish event",
                "The event and its gallery become visible to everyone.",
            )
        )
    else:
        actions.append(
            ("unpublish_event", "Unpublish event", "Hides the event from everyone but organizers.")
        )
    if event.judging_opened_at is None or event.judging_closed_at is not None:
        actions.append(
            ("open_judging", "Open judging", "Judges can start scoring their assigned projects.")
        )
    else:
        actions.append(
            ("close_judging", "Close judging", "Judges can no longer edit or submit reviews.")
        )
    if event.results_published_at is None:
        actions.append(
            (
                "publish_results",
                "Publish results",
                "Rankings, scores and vote counts become public. This is logged.",
            )
        )
    else:
        actions.append(
            ("unpublish_results", "Unpublish results", "Results are hidden again. This is logged.")
        )
    actions.append(
        (
            "archive",
            "Archive event",
            "The event becomes read-only for everyone. This cannot be undone here.",
        )
    )
    return actions


# --- judging progress ----------------------------------------------------------------------------


@dataclass
class JudgeProgress:
    judge: User
    tracks: list[str]
    assigned: int
    done: int
    in_progress: int

    @property
    def pending(self) -> int:
        return self.assigned - self.done - self.in_progress

    @property
    def pct(self) -> int:
        return round(100 * self.done / self.assigned) if self.assigned else 0


@dataclass
class TrackProgress:
    track: Track | None
    projects: int
    reviews: int
    below_target: int

    @property
    def avg(self) -> float:
        return self.reviews / self.projects if self.projects else 0.0


@dataclass
class Progress:
    judges: list[JudgeProgress]
    tracks: list[TrackProgress]
    below_target: list[tuple[Project, int]]  # project, submitted review count
    flat_judges: list[User]
    total_assignments: int
    total_done: int

    @property
    def pct(self) -> int:
        return (
            round(100 * self.total_done / self.total_assignments) if self.total_assignments else 0
        )


def progress(db: DbSession, event: Event) -> Progress:
    from podium.services import scoring

    judges = (
        db.execute(
            select(User)
            .join(EventRole, EventRole.user_id == User.id)
            .where(EventRole.event_id == event.id, EventRole.role == Role.judge)
            .order_by(User.name)
        )
        .scalars()
        .all()
    )
    track_names: dict[int, list[str]] = {}
    for user_id, name in db.execute(
        select(JudgeTrack.user_id, Track.name)
        .join(Track, Track.id == JudgeTrack.track_id)
        .where(JudgeTrack.event_id == event.id)
        .order_by(Track.position)
    ).all():
        track_names.setdefault(user_id, []).append(name)
    counts: dict[int, dict[str, int]] = {}
    for judge_id, status, n in db.execute(
        select(Assignment.judge_id, Assignment.status, func.count(Assignment.id))
        .where(Assignment.event_id == event.id)
        .group_by(Assignment.judge_id, Assignment.status)
    ).all():
        counts.setdefault(judge_id, {})[status.value] = n
    judge_rows = []
    for j in judges:
        c = counts.get(j.id, {})
        judge_rows.append(
            JudgeProgress(
                judge=j,
                tracks=track_names.get(j.id, []),
                assigned=sum(c.values()),
                done=c.get("done", 0),
                in_progress=c.get("in_progress", 0),
            )
        )
    submitted = {
        project_id: n
        for project_id, n in db.execute(
            select(Review.project_id, func.count(Review.id))
            .where(Review.event_id == event.id, Review.status == ReviewStatus.submitted)
            .group_by(Review.project_id)
        ).all()
    }
    projects = (
        db.execute(
            select(Project)
            .where(Project.event_id == event.id, Project.status == ProjectStatus.submitted)
            .order_by(Project.id)
        )
        .scalars()
        .all()
    )
    tracks = list(
        db.execute(
            select(Track).where(Track.event_id == event.id).order_by(Track.position)
        ).scalars()
    )
    track_rows = []
    for track in [*tracks, None]:
        members = [p for p in projects if p.track_id == (track.id if track else None)]
        if not members and track is None:
            continue
        reviews = sum(submitted.get(p.id, 0) for p in members)
        below = sum(1 for p in members if submitted.get(p.id, 0) < event.reviews_per_project)
        track_rows.append(
            TrackProgress(track=track, projects=len(members), reviews=reviews, below_target=below)
        )
    below_target = [
        (p, submitted.get(p.id, 0))
        for p in projects
        if submitted.get(p.id, 0) < event.reviews_per_project
    ]
    below_target.sort(key=lambda pair: (pair[1], pair[0].title.lower()))
    results = scoring.compute(db, event)
    flat = [j.judge for j in results.judges if j.flat]
    total = sum(r.assigned for r in judge_rows)
    done = sum(r.done for r in judge_rows)
    return Progress(
        judges=judge_rows,
        tracks=track_rows,
        below_target=below_target,
        flat_judges=flat,
        total_assignments=total,
        total_done=done,
    )
