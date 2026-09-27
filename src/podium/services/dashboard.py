"""Numbers, attention items and judging progress for the organizer console."""

from dataclasses import dataclass, field
from datetime import datetime, timedelta

from sqlalchemy import func, select
from sqlalchemy.orm import Session as DbSession

from podium.models import (
    Assignment,
    Certificate,
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
from podium.services.events import Stage, publish_blocker, shifted, stage_of
from podium.services.text import plural, utc_text


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
        if prog.below_target and event.judging_closed_at is None:
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
        elif prog.below_target:
            add(
                AttentionItem(
                    "warning",
                    f"{plural(len(prog.below_target), 'project')} were scored on fewer than "
                    f"{event.reviews_per_project} reviews",
                    "They are marked thin in Results: their rank is less certain.",
                    f"{base}/results#ranking",
                    "See results",
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
    if "duplicate" in title:
        return not published
    if "tracks" in title or "deadline" in title:
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
    at: datetime | None = None  # a date the label ends with, rendered in local time
    link_label: str = ""  # the button text for href steps when it isn't the label


CLOSE_JUDGING = ("close_judging", "Close judging", "Judges can no longer edit or submit reviews.")
PUBLISH_CONSEQUENCE = "Rankings, scores and vote counts become public. This is logged."


def certificate_counts(db: DbSession, event: Event) -> tuple[int, int]:
    """(issued and still valid, revoked) certificates for the event."""
    rows = dict(
        db.execute(
            select(Certificate.revoked_at.is_(None), func.count())
            .where(Certificate.event_id == event.id)
            .group_by(Certificate.revoked_at.is_(None))
        ).all()
    )
    return rows.get(True, 0), rows.get(False, 0)


def _duplicates_ranked(db: DbSession, event: Event) -> int:
    return db.execute(
        select(func.count())
        .select_from(Project)
        .where(
            Project.event_id == event.id,
            Project.duplicate_of_id.isnot(None),
            Project.status == ProjectStatus.submitted,
        )
    ).scalar_one()


def _below_target(db: DbSession, event: Event) -> int:
    """Submitted projects with fewer submitted reviews than the event's target."""
    reviewed = (
        select(Review.project_id, func.count(Review.id).label("n"))
        .where(Review.event_id == event.id, Review.status == ReviewStatus.submitted)
        .group_by(Review.project_id)
        .subquery()
    )
    return db.execute(
        select(func.count())
        .select_from(Project)
        .outerjoin(reviewed, reviewed.c.project_id == Project.id)
        .where(
            Project.event_id == event.id,
            Project.status == ProjectStatus.submitted,
            func.coalesce(reviewed.c.n, 0) < event.reviews_per_project,
        )
    ).scalar_one()


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
        return NextStep(
            "Submissions open until" if event.submissions_close_at else "Submissions open",
            "After the deadline, open judging from here — it does not open by itself.",
            href=f"{base}/progress",
            enabled=False,
            at=event.submissions_close_at,
            link_label="View judging progress",
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
        below = _below_target(db, event)
        thin = (
            f" {plural(below, 'project')} have fewer than {event.reviews_per_project} "
            "submitted reviews and will be marked thin in Results."
            if below
            else ""
        )
        if pending:
            return NextStep(
                f"Judging in progress · {plural(pending, 'review')} pending",
                "Close judging once the reviews you need are in.",
                href=f"{base}/progress",
                enabled=False,
                note=f"{done} of {total} assigned reviews are in." + thin,
                secondary=pending_close,
                link_label="View judging progress",
            )
        return NextStep(
            "Close judging",
            CLOSE_JUDGING[2],
            action="close_judging",
            note="Every assigned review is in." + thin,
        )
    if stage == Stage.voting:
        step = NextStep(
            "Voting open until" if event.voting_close_at else "Voting open until you close it",
            "Results can be published once it closes.",
            href=f"{base}/voting",
            enabled=False,
            at=event.voting_close_at,
            link_label="Manage voting",
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
        duplicates = _duplicates_ranked(db, event)
        if duplicates:
            note += (
                f" {plural(duplicates, 'possible duplicate')} still ranked; withdraw it from "
                "Attention first if it shouldn't be."
            )
        if event.voting_open_at is not None and event.voting_open_at > utcnow():
            return NextStep(
                "Voting opens",
                "Publish after voting closes so vote counts are final.",
                href=f"{base}/voting",
                enabled=False,
                note=note,
                at=event.voting_open_at,
                link_label="Manage voting",
                secondary=(
                    "publish_results",
                    "Publish results now",
                    "Rankings go public before the community vote; vote counts stay hidden "
                    "until it closes.",
                ),
            )
        return NextStep("Publish results", PUBLISH_CONSEQUENCE, action="publish_results", note=note)
    issued, _ = certificate_counts(db, event)
    if issued:
        return NextStep(
            f"Event complete — {plural(issued, 'certificate')} issued",
            "Results are public and every certificate verifies at /verify.",
            href=f"/e/{event.slug}/results",
            link_label="View public results",
            secondary=("archive", "Archive event", "The event becomes read-only for everyone."),
        )
    return NextStep(
        "Issue certificates",
        "Participation certificates, judge records and winner certificates.",
        href=f"{base}/certificates",
    )


@dataclass
class PhaseAction:
    label: str
    action: str | None = None  # POST /organizer/actions/{action}
    date_field: str | None = None  # POST /organizer/dates with preset
    preset: str | None = None  # now | +15m | +60m
    href: str | None = None
    consequence: str = ""
    enabled: bool = True
    reason: str = ""
    kind: str = "secondary"  # primary | secondary | danger


@dataclass
class Phase:
    key: str
    label: str
    state: str  # scheduled | open | closed | done | off
    summary: str
    starts: datetime | None = None
    ends: datetime | None = None
    actions: list[PhaseAction] = field(default_factory=list)


def timeline(db: DbSession, event: Event, now: datetime | None = None) -> list[Phase]:
    """The run of show as phases with the actions the server would accept right now. This is
    the one place an organizer changes a date in a hurry, and it never offers an undo as a
    next step."""
    from podium.services.voting import voting_has_closed, voting_is_open

    now = now or utcnow()
    base = f"/e/{event.slug}/organizer"
    phases: list[Phase] = []
    archived = event.archived_at is not None
    # -- submissions
    sub_open = (
        event.is_public
        and (event.submissions_open_at is None or event.submissions_open_at <= now)
        and (event.submissions_close_at is None or now < event.submissions_close_at)
    )
    if not event.is_public:
        sub = Phase(
            "submissions", "Submissions", "off", "Not public yet — publish the event to open"
        )
    elif event.submissions_open_at and event.submissions_open_at > now:
        sub = Phase("submissions", "Submissions", "scheduled", "Opens")
    elif sub_open:
        sub = Phase("submissions", "Submissions", "open", "Open")
    else:
        sub = Phase("submissions", "Submissions", "closed", "Closed")
    sub.starts, sub.ends = event.submissions_open_at, event.submissions_close_at
    if not archived and event.is_public:
        if sub_open:
            sub.actions += [
                PhaseAction(
                    "Close now",
                    date_field="submissions_close_at",
                    preset="now",
                    consequence=(
                        "Submissions close this minute; teams can no longer submit or edit."
                    ),
                    kind="danger",
                ),
                PhaseAction(
                    "Extend 15 min",
                    date_field="submissions_close_at",
                    preset="+15m",
                    consequence="The deadline moves 15 minutes later, to "
                    f"{utc_text(shifted(event, 'submissions_close_at', '+15m', now))}.",
                ),
                PhaseAction(
                    "Extend 1 h",
                    date_field="submissions_close_at",
                    preset="+60m",
                    consequence="The deadline moves one hour later, to "
                    f"{utc_text(shifted(event, 'submissions_close_at', '+60m', now))}.",
                ),
            ]
        elif event.submissions_close_at and event.judging_opened_at is None:
            sub.actions.append(
                PhaseAction(
                    "Reopen for 1 h",
                    date_field="submissions_close_at",
                    preset="+60m",
                    consequence="Submissions reopen until "
                    f"{utc_text(shifted(event, 'submissions_close_at', '+60m', now))}; teams can "
                    "submit and edit again.",
                )
            )
        elif event.submissions_close_at:
            sub.actions.append(
                PhaseAction(
                    "Reopen for 1 h",
                    enabled=False,
                    reason="results are published"
                    if event.results_published_at
                    else "judging has started",
                )
            )
    phases.append(sub)
    # -- judging
    total, pending = _assignment_counts(db, event)
    if event.judging_opened_at is None:
        jud = Phase("judging", "Judging", "scheduled", "Not opened yet")
    elif event.judging_closed_at is None:
        jud = Phase(
            "judging",
            "Judging",
            "open",
            f"Open · {total - pending} of {total} reviews in"
            if total
            else "Open · nothing assigned",
        )
    else:
        jud = Phase("judging", "Judging", "closed", "Closed")
    jud.starts, jud.ends = event.judging_opened_at, event.judging_closed_at
    if not archived:
        if event.judging_opened_at is None:
            jud.actions.append(
                PhaseAction(
                    "Open judging",
                    action="open_judging",
                    consequence="Judges can start scoring their assigned projects."
                    + (
                        " Submissions are still open, so teams can still change what judges score."
                        if sub_open
                        else ""
                    ),
                    kind="primary" if not sub_open else "secondary",
                    enabled=event.is_public,
                    reason="" if event.is_public else "publish the event first",
                )
            )
        elif event.judging_closed_at is None:
            jud.actions.append(
                PhaseAction(
                    "Close judging",
                    action="close_judging",
                    consequence=(
                        "Judges can no longer edit or submit reviews; "
                        f"{plural(pending, 'pending review')} stay unsubmitted."
                        if pending
                        else "Judges can no longer edit or submit reviews."
                    ),
                    kind="primary" if not pending else "secondary",
                )
            )
        elif event.results_published_at is None:
            jud.actions.append(
                PhaseAction(
                    "Reopen judging",
                    action="open_judging",
                    consequence="Judges can edit and submit reviews again.",
                )
            )
    phases.append(jud)
    # -- voting
    if event.voting_open_at is None:
        vot = Phase("voting", "Community voting", "off", "Not scheduled")
        if not archived:
            vot.actions.append(PhaseAction("Set a window", href=f"{base}/voting"))
    elif voting_is_open(event):
        vot = Phase("voting", "Community voting", "open", "Open")
    elif voting_has_closed(event):
        vot = Phase("voting", "Community voting", "closed", "Closed")
    else:
        vot = Phase("voting", "Community voting", "scheduled", "Opens")
    vot.starts, vot.ends = event.voting_open_at, event.voting_close_at
    if not archived and event.voting_open_at is not None:
        if vot.state == "scheduled":
            vot.actions.append(
                PhaseAction(
                    "Open now",
                    date_field="voting_open_at",
                    preset="now",
                    consequence="Voting opens this minute.",
                )
            )
        elif vot.state == "open":
            vot.actions += [
                PhaseAction(
                    "Close now",
                    date_field="voting_close_at",
                    preset="now",
                    consequence="Voting closes this minute; nobody else can vote.",
                    kind="danger",
                ),
                PhaseAction(
                    "Extend 15 min",
                    date_field="voting_close_at",
                    preset="+15m",
                    consequence="Voting closes 15 minutes later, at "
                    f"{utc_text(shifted(event, 'voting_close_at', '+15m', now))}.",
                ),
                PhaseAction(
                    "Extend 1 h",
                    date_field="voting_close_at",
                    preset="+60m",
                    consequence="Voting closes one hour later, at "
                    f"{utc_text(shifted(event, 'voting_close_at', '+60m', now))}.",
                ),
            ]
    phases.append(vot)
    # -- results
    if event.results_published_at is not None:
        res = Phase("results", "Results", "done", "Published")
        res.starts = event.results_published_at
        if not archived:
            res.actions.append(
                PhaseAction(
                    "Unpublish results",
                    action="unpublish_results",
                    consequence=(
                        "Results are hidden again and issued winner certificates are revoked. "
                        "This is logged."
                    ),
                    kind="danger",
                )
            )
    else:
        res = Phase("results", "Results", "scheduled", "Not published")
        if not archived:
            blocker = publish_blocker(event, now)
            duplicates = _duplicates_ranked(db, event) if blocker is None else 0
            res.actions.append(
                PhaseAction(
                    "Publish results",
                    action="publish_results",
                    consequence=PUBLISH_CONSEQUENCE
                    + (
                        f" {plural(duplicates, 'possible duplicate')} still ranked."
                        if duplicates
                        else ""
                    ),
                    enabled=blocker is None,
                    reason=blocker[0] if blocker else "",
                    # the obvious next move only once nothing else is still running
                    kind="primary"
                    if blocker is None
                    and (event.voting_open_at is None or voting_has_closed(event))
                    else "secondary",
                )
            )
    phases.append(res)
    # -- certificates
    issued, revoked = certificate_counts(db, event)
    cert = Phase(
        "certificates",
        "Certificates",
        "done" if event.results_published_at else "scheduled",
        f"{issued} issued · {revoked} revoked"
        if issued or revoked
        else "Issue after publishing"
        if not event.results_published_at
        else "Ready to issue",
    )
    if not archived:
        cert.actions.append(PhaseAction("Certificates", href=f"{base}/certificates"))
    phases.append(cert)
    return phases


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
