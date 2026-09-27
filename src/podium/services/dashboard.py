"""Numbers, attention items and judging progress for the organizer console."""

from dataclasses import dataclass, field
from datetime import datetime, timedelta

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
    RubricCriterion,
    Team,
    Track,
    User,
)
from podium.models.base import utcnow
from podium.services.events import Stage, stage_of
from podium.services.text import plural


@dataclass
class AttentionItem:
    kind: str  # info | warning | serious
    title: str
    detail: str
    href: str
    action: str
    form_action: str | None = None  # a POST the organizer can take right here
    form_label: str = ""
    form_confirm: str = ""


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
    invites_pending: int = 0


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
                form_action=f"/e/{event.slug}/projects/{dup.public_id}/withdraw",
                form_label="Withdraw duplicate",
                form_confirm=f"Withdraw “{dup.title}” ({dup.public_id})? It leaves the gallery and "
                "the judging pool; the team or you can restore it later.",
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
                    f"{plural(len(prog.below_target), 'project')} have fewer than "
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
    data.invites_pending = pending
    if pending:
        add(
            AttentionItem(
                "info",
                f"{plural(pending, 'judge invitation')} not accepted yet",
                "Judges can't be assigned projects until they accept.",
                f"{base}/judges#invites",
                "See invites",
            )
        )
    stale = db.execute(
        select(func.count())
        .select_from(JudgeInvite)
        .where(
            JudgeInvite.event_id == event.id,
            JudgeInvite.accepted_at.is_(None),
            JudgeInvite.created_at < utcnow() - timedelta(days=7),
        )
    ).scalar_one()
    if stale:
        add(
            AttentionItem(
                "info",
                f"{plural(stale, 'invitation')} older than a week",
                "Chase the judge, regenerate the link, or revoke it.",
                f"{base}/judges#invites",
                "Review invites",
            )
        )
    if data.stage in (Stage.draft, Stage.upcoming, Stage.open, Stage.closed):
        criteria = db.execute(
            select(func.count())
            .select_from(RubricCriterion)
            .where(RubricCriterion.event_id == event.id, RubricCriterion.archived_at.is_(None))
        ).scalar_one()
        if not criteria:
            add(
                AttentionItem(
                    "warning",
                    "No rubric yet",
                    "Judges can't score until the rubric has at least one criterion.",
                    f"{base}/rubric",
                    "Add criteria",
                )
            )
        if data.judges == 0 and data.stage != Stage.draft:
            add(
                AttentionItem(
                    "warning",
                    "No judges have accepted yet",
                    "Invite judges and send them their links; assignments need accepted judges.",
                    f"{base}/judges",
                    "Invite judges",
                )
            )
    data.attention = [item for item in data.attention if _still_relevant(item, event)]
    return data


def _still_relevant(item: AttentionItem, event: Event) -> bool:
    """Attention items expire with the phase they belong to: nobody needs "8 projects below
    target" on a published event or "invitations pending" once judging has closed."""
    title = item.title.lower()
    if event.archived_at is not None:
        return "not public" in title
    judging_closed = event.judging_closed_at is not None
    published = event.results_published_at is not None
    if "duplicate" in title or "tracks" in title or "deadline" in title:
        return not judging_closed
    if "invitation" in title:
        return not judging_closed
    if "identically" in title or "fewer than" in title:
        return not published
    return True


STEPS = [
    (Stage.draft, "Set up", "Dates, tracks, prizes, rubric"),
    (Stage.open, "Submissions", "Teams form and submit"),
    (Stage.closed, "Closed", "Deadline passed"),
    (Stage.judging, "Judging", "Judges score assigned projects"),
    (Stage.voting, "Voting", "Community votes"),
    (Stage.judged, "Results review", "Scores are final: check normalization, award prizes"),
    (Stage.published, "Published", "Results are public"),
    (Stage.archived, "Archived", "Read-only"),
]


def steps_for(event: Event) -> list[tuple[Stage, str, str]]:
    """The run of show for this event: Voting only appears when a window is scheduled."""
    return [s for s in STEPS if s[0] != Stage.voting or event.voting_open_at is not None]


def step_index(event: Event, now: datetime | None = None) -> int:
    """The furthest milestone reached. Milestones come from set-once timestamps, so the index
    never walks backwards as time passes; only an explicit reopen or unpublish lowers it."""
    now = now or utcnow()
    order = {stage: i for i, (stage, _, _) in enumerate(steps_for(event))}
    reached = Stage.draft
    if event.is_public:
        reached = Stage.open  # covers "upcoming" too
    if event.is_public and event.submissions_close_at and now >= event.submissions_close_at:
        reached = Stage.closed
    if event.judging_opened_at is not None:
        reached = Stage.judging
    if Stage.voting in order and event.voting_open_at <= now:
        reached = Stage.voting  # open or already closed
    voting_done = event.voting_open_at is None or (
        event.voting_close_at is not None and now >= event.voting_close_at
    )
    if event.judging_closed_at is not None and voting_done:
        reached = Stage.judged
    if event.results_published_at is not None:
        reached = Stage.published
    if event.archived_at is not None:
        reached = Stage.archived
    return order[reached]


@dataclass
class NextStep:
    label: str
    consequence: str = ""
    action: str | None = None  # POST /organizer/actions/{action}
    href: str | None = None  # or a page to go to
    enabled: bool = True
    reason: str = ""
    note: str = ""
    secondary: tuple[str, str, str] | None = None  # (action, button label, consequence)


CLOSE_JUDGING = ("close_judging", "Close judging", "Judges can no longer edit or submit reviews.")
PUBLISH_CONSEQUENCE = "Rankings, scores and vote counts become public. This is logged."


def _when(value: datetime | None) -> str:
    return value.strftime("%d %b %Y, %H:%M UTC") if value else ""


def _assignment_counts(db: DbSession, event: Event) -> tuple[int, int]:
    """(total, pending) assignments for the event."""
    from podium.models import Assignment, AssignmentStatus

    total = db.execute(
        select(func.count()).select_from(Assignment).where(Assignment.event_id == event.id)
    ).scalar_one()
    pending = db.execute(
        select(func.count())
        .select_from(Assignment)
        .where(Assignment.event_id == event.id, Assignment.status != AssignmentStatus.done)
    ).scalar_one()
    return total, pending


def next_step(db: DbSession, event: Event) -> NextStep:
    """Exactly one recommended next move for the organizer, plus at most one secondary action.
    The rules follow the stage; nothing here can suggest undoing what was just done."""
    from podium.models import RubricCriterion

    stage = stage_of(event)
    base = f"/e/{event.slug}/organizer"
    if stage == Stage.archived:
        return NextStep("Archived — read-only", enabled=False)
    if not event.is_public:
        return NextStep(
            "Publish event",
            "The event and its gallery become visible to everyone.",
            action="publish_event",
        )
    if stage in (Stage.upcoming, Stage.open):
        criteria = db.execute(
            select(func.count())
            .select_from(RubricCriterion)
            .where(RubricCriterion.event_id == event.id, RubricCriterion.archived_at.is_(None))
        ).scalar_one()
        judges = db.execute(
            select(func.count())
            .select_from(EventRole)
            .where(EventRole.event_id == event.id, EventRole.role == Role.judge)
        ).scalar_one()
        if criteria == 0:
            return NextStep(
                "Add rubric criteria",
                "Judges need a rubric before judging can open.",
                href=f"{base}/rubric",
            )
        if judges == 0:
            return NextStep(
                "Invite judges", "Invitations are links you send yourself.", href=f"{base}/judges"
            )
        when = _when(event.submissions_close_at) or "no deadline set"
        return NextStep(
            f"Submissions open until {when}",
            "Judging opens once the deadline passes.",
            href=f"{base}/progress",
            enabled=False,
        )
    if stage == Stage.closed:
        return NextStep(
            "Open judging",
            "Judges can start scoring their assigned projects.",
            action="open_judging",
        )
    total, pending = _assignment_counts(db, event)
    done = total - pending
    pending_close = (
        CLOSE_JUDGING[0],
        CLOSE_JUDGING[1],
        f"Judges can no longer edit or submit reviews; {plural(pending, 'pending review')} "
        "stay unsubmitted.",
    )
    if stage == Stage.judging:
        if total == 0:
            return NextStep(
                "Assign projects to judges",
                "Judges have nothing to score until projects are assigned.",
                href=f"{base}/assignments",
                secondary=CLOSE_JUDGING,
            )
        if pending:
            return NextStep(
                f"Judging in progress · {plural(pending, 'review')} pending",
                "Close judging once the reviews you need are in.",
                href=f"{base}/progress",
                enabled=False,
                note=f"{done} of {total} assigned reviews are in.",
                secondary=pending_close,
            )
        return NextStep(
            "Close judging",
            CLOSE_JUDGING[2],
            action="close_judging",
            note="Every assigned review is in.",
        )
    if stage == Stage.voting:
        step = NextStep(
            f"Voting open until {_when(event.voting_close_at) or 'you close it'}",
            "Results can be published once it closes.",
            href=f"{base}/voting",
            enabled=False,
        )
        if event.judging_opened_at is not None and event.judging_closed_at is None:
            step.note = (
                f"{pending} of {total} assigned reviews still pending."
                if pending
                else "Every assigned review is in."
            )
            step.secondary = pending_close if pending else CLOSE_JUDGING
        elif event.judging_opened_at is None:
            step.secondary = (
                "open_judging",
                "Open judging",
                "Judges can start scoring their assigned projects.",
            )
        else:
            step.note = "Judging is closed. Publish after voting closes so vote counts are final."
        return step
    if stage == Stage.judged:
        note = (
            f"{plural(pending, 'assigned review')} were never submitted."
            if pending
            else "Every assigned review is in."
        )
        if event.voting_open_at is not None and event.voting_open_at > utcnow():
            return NextStep(
                f"Voting opens {_when(event.voting_open_at)}",
                "Publish after voting closes so vote counts are final.",
                href=f"{base}/voting",
                enabled=False,
                note=note,
                secondary=(
                    "publish_results",
                    "Publish results now",
                    "Rankings go public before the community vote; vote counts stay hidden "
                    "until it closes.",
                ),
            )
        return NextStep("Publish results", PUBLISH_CONSEQUENCE, action="publish_results", note=note)
    return NextStep(
        "Issue certificates",
        "Participation certificates, judge records and winner certificates.",
        href=f"{base}/certificates",
    )


def more_actions(event: Event) -> list[tuple[str, str, str]]:
    """Reversible-but-disruptive actions, kept out of the primary path."""
    out: list[tuple[str, str, str]] = []
    if event.archived_at is not None:
        return out
    if event.is_public:
        out.append(
            ("unpublish_event", "Unpublish event", "Hides the event from everyone but organizers.")
        )
    if event.judging_closed_at is not None and event.results_published_at is None:
        out.append(("open_judging", "Reopen judging", "Judges can edit and submit reviews again."))
    if event.results_published_at is not None:
        out.append(
            (
                "unpublish_results",
                "Unpublish results",
                "Results are hidden again and issued winner certificates are revoked. "
                "This is logged.",
            )
        )
    out.append(("archive", "Archive event", "The event becomes read-only for everyone."))
    return out


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
    projects_total: int = 0
    projects_reviewed: int = 0  # submitted projects at or above reviews_per_project

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
        projects_total=len(projects),
        projects_reviewed=len(projects) - len(below_target),
        flat_judges=flat,
        total_assignments=total,
        total_done=done,
    )
