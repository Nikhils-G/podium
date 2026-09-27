"""Community voting. Three ways to identify a voter (per event): a signed anonymous cookie
(link mode), a redeemed voter code (email/code mode) or a signed-in account. Every vote is
unique per (event, project, voter); quadratic mode charges n² credits for n votes on one project.
Tallies stay hidden until the window has closed AND the organizer publishes."""

import hashlib
import hmac
import random
import re
import secrets
from dataclasses import dataclass, field

from sqlalchemy import func, select, update
from sqlalchemy.orm import Session as DbSession

from podium.errors import Closed, Conflict, Forbidden, NotFound, ValidationFailed
from podium.models import (
    Event,
    Project,
    ProjectStatus,
    User,
    Vote,
    VoterCode,
    VoterLedger,
    VotingMode,
    utcnow,
)
from podium.services import audit, webhooks

BURST_THRESHOLD = 12  # votes from one ip_hash inside the window before we flag them


# --- window and identity -------------------------------------------------------------------------


def voting_is_open(event: Event) -> bool:
    now = utcnow()
    return (
        event.archived_at is None
        and event.voting_open_at is not None
        and event.voting_open_at <= now
        and (event.voting_close_at is None or now < event.voting_close_at)
    )


def voting_has_closed(event: Event) -> bool:
    return event.voting_close_at is not None and utcnow() >= event.voting_close_at


def results_visible(event: Event, *, organizer: bool) -> bool:
    return organizer or event.results_published_at is not None


def tallies_visible(event: Event, *, organizer: bool) -> bool:
    """Vote counts: organizers always; everyone else only after close and publish."""
    if organizer:
        return True
    if event.voting_open_at is None:
        return event.results_published_at is not None
    return voting_has_closed(event) and event.results_published_at is not None


def sign_voter_id(secret: str, voter_id: str) -> str:
    return hmac.new(secret.encode(), voter_id.encode(), "sha256").hexdigest()[:24]


def parse_voter_cookie(secret: str, value: str | None) -> str | None:
    if not value or "." not in value:
        return None
    voter_id, sig = value.rsplit(".", 1)
    if hmac.compare_digest(sign_voter_id(secret, voter_id), sig):
        return voter_id
    return None


def new_voter_cookie(secret: str) -> tuple[str, str]:
    voter_id = secrets.token_urlsafe(12)
    return voter_id, f"{voter_id}.{sign_voter_id(secret, voter_id)}"


@dataclass
class Voter:
    key: str  # unique per event: usr:<id> | anon:<id> | code:<hash>
    mode: VotingMode
    user: User | None = None
    verified: bool = True


def resolve_voter(
    event: Event, user: User | None, anon_id: str | None, code_key: str | None
) -> Voter | None:
    """Who is voting, according to the event's mode. None → the UI must ask them to identify."""
    mode = event.voting_mode
    if mode == VotingMode.account:
        return Voter(key=f"usr:{user.public_id}", mode=mode, user=user) if user else None
    if mode == VotingMode.link:
        if user is not None:
            return Voter(key=f"usr:{user.public_id}", mode=mode, user=user)
        return Voter(key=f"anon:{anon_id}", mode=mode) if anon_id else None
    if mode == VotingMode.email:
        return Voter(key=f"code:{code_key}", mode=mode, user=user) if code_key else None
    return None


# --- ballots ---------------------------------------------------------------------------------


def ballot_order(projects: list[Project], voter_key: str, event_id: int) -> list[Project]:
    """Stable per voter, different across voters: position bias is spread, reloads don't shuffle."""
    seed = int(hashlib.sha256(f"{event_id}:{voter_key}".encode()).hexdigest()[:12], 16)
    rng = random.Random(seed)
    ordered = projects[:]
    rng.shuffle(ordered)
    return ordered


@dataclass
class VoterStatus:
    votes: dict[int, int] = field(default_factory=dict)  # project_id -> credits (votes) cast
    credits_spent: int = 0
    credits_total: int = 0

    @property
    def credits_left(self) -> int:
        return max(0, self.credits_total - self.credits_spent)


def _ledger(db: DbSession, event: Event, voter: Voter) -> VoterLedger:
    row = db.execute(
        select(VoterLedger).where(
            VoterLedger.event_id == event.id, VoterLedger.voter_key == voter.key
        )
    ).scalar_one_or_none()
    if row is None:
        row = VoterLedger(
            event_id=event.id,
            voter_key=voter.key,
            mode=voter.mode.value,
            verified_at=utcnow() if voter.verified else None,
        )
        db.add(row)
        db.flush()
    return row


def voter_status(db: DbSession, event: Event, voter: Voter | None) -> VoterStatus:
    status = VoterStatus(credits_total=event.voting_credits if event.quadratic_enabled else 0)
    if voter is None:
        return status
    for project_id, credits in db.execute(
        select(Vote.project_id, Vote.credits).where(
            Vote.event_id == event.id, Vote.voter_key == voter.key, Vote.voided_at.is_(None)
        )
    ).all():
        status.votes[project_id] = credits
    status.credits_spent = (
        sum(c * c for c in status.votes.values()) if event.quadratic_enabled else 0
    )
    return status


def cost_of_next_vote(current: int) -> int:
    """Quadratic: the n-th vote on one project costs n² − (n−1)² = 2n − 1 credits."""
    return 2 * (current + 1) - 1


def cast(
    db: DbSession,
    event: Event,
    project: Project,
    voter: Voter,
    *,
    ip_hash: str | None,
    ua_hash: str | None = None,
    honeypot: str = "",
) -> Vote:
    if not voting_is_open(event):
        raise Closed("Voting isn't open right now.")
    if project.event_id != event.id or project.status != ProjectStatus.submitted:
        raise NotFound("That project can't be voted for.")
    if honeypot:
        # A bot filled the invisible field: log it, pretend it worked, store nothing.
        audit.record(
            db,
            "vote.rejected",
            "project",
            project.public_id,
            event_id=event.id,
            meta={"reason": "honeypot"},
            ip_hash=ip_hash,
        )
        db.commit()
        raise Forbidden("Your vote could not be counted.")
    if voter.user is not None:
        from podium.models import TeamMember

        own = db.execute(
            select(TeamMember.id).where(
                TeamMember.team_id == project.team_id, TeamMember.user_id == voter.user.id
            )
        ).scalar()
        if own is not None:
            audit.record(
                db,
                "vote.rejected",
                "project",
                project.public_id,
                event_id=event.id,
                actor_id=voter.user.id,
                meta={"reason": "own team"},
                ip_hash=ip_hash,
            )
            db.commit()
            raise Forbidden("You can't vote for your own team's project.")
    status = voter_status(db, event, voter)
    existing = db.execute(
        select(Vote).where(
            Vote.event_id == event.id, Vote.project_id == project.id, Vote.voter_key == voter.key
        )
    ).scalar_one_or_none()
    if existing is not None and existing.voided_at is not None:
        raise Forbidden("A vote from you on this project was voided by the organizers.")
    ledger = _ledger(db, event, voter)
    if event.quadratic_enabled:
        current = status.votes.get(project.id, 0)
        cost = cost_of_next_vote(current)
        if status.credits_spent + cost > event.voting_credits:
            raise Conflict(
                f"Not enough credits: another vote here costs {cost}, "
                f"you have {status.credits_left} left."
            )
        if existing is None:
            existing = Vote(
                event_id=event.id,
                project_id=project.id,
                voter_key=voter.key,
                voter_user_id=voter.user.id if voter.user else None,
                credits=1,
                ip_hash=ip_hash,
                ua_hash=ua_hash,
            )
            db.add(existing)
        else:
            existing.credits += 1
        ledger.credits_spent = status.credits_spent + cost
    else:
        if existing is not None:
            raise Conflict("You've already voted for this project.")
        existing = Vote(
            event_id=event.id,
            project_id=project.id,
            voter_key=voter.key,
            voter_user_id=voter.user.id if voter.user else None,
            credits=1,
            ip_hash=ip_hash,
            ua_hash=ua_hash,
        )
        db.add(existing)
    db.flush()
    if ip_hash:
        burst = db.execute(
            select(func.count())
            .select_from(Vote)
            .where(Vote.event_id == event.id, Vote.ip_hash == ip_hash, Vote.voided_at.is_(None))
        ).scalar_one()
        if burst > BURST_THRESHOLD:
            existing.flagged = True
            existing.flag_reason = f"{burst} votes from one network address"
    audit.record(
        db,
        "vote.cast",
        "project",
        project.public_id,
        event_id=event.id,
        actor_id=voter.user.id if voter.user else None,
        meta={"voter": voter.key[:24], "credits": existing.credits, "flagged": existing.flagged},
        ip_hash=ip_hash,
    )
    total = db.execute(
        select(func.coalesce(func.sum(Vote.credits), 0)).where(
            Vote.event_id == event.id, Vote.project_id == project.id, Vote.voided_at.is_(None)
        )
    ).scalar_one()
    webhooks.emit(
        db,
        event,
        "vote.cast",
        {"project": project.public_id, "votes": int(total)},
        coalesce_on="project",
    )
    db.commit()
    return existing


def retract(
    db: DbSession, event: Event, project: Project, voter: Voter, *, ip_hash: str | None
) -> None:
    if not voting_is_open(event):
        raise Closed("Voting isn't open, so votes can't be changed.")
    vote = db.execute(
        select(Vote).where(
            Vote.event_id == event.id,
            Vote.project_id == project.id,
            Vote.voter_key == voter.key,
            Vote.voided_at.is_(None),
        )
    ).scalar_one_or_none()
    if vote is None:
        raise NotFound("You haven't voted for this project.")
    ledger = _ledger(db, event, voter)
    if event.quadratic_enabled and vote.credits > 1:
        ledger.credits_spent -= vote.credits * vote.credits - (vote.credits - 1) ** 2
        vote.credits -= 1
    else:
        if event.quadratic_enabled:
            ledger.credits_spent = max(0, ledger.credits_spent - 1)
        db.delete(vote)
    audit.record(
        db,
        "vote.retracted",
        "project",
        project.public_id,
        event_id=event.id,
        actor_id=voter.user.id if voter.user else None,
        meta={"voter": voter.key[:24]},
        ip_hash=ip_hash,
    )
    db.commit()


# --- codes (email / code-gated mode) --------------------------------------------------------------


def normalize_code(code: str) -> str:
    """`abcd-efgh`, `ABCDEFGH` and `abcd efgh` are the same code."""
    return re.sub(r"[^A-Z0-9]", "", (code or "").upper())


def _code_hash(code: str) -> str:
    return hashlib.sha256(normalize_code(code).encode()).hexdigest()


def generate_codes(
    db: DbSession, event: Event, organizer: User, count: int, emails: list[str] | None = None
) -> list[tuple[str, str | None]]:
    """Create voter codes; the raw codes are returned once (organizer distributes them)."""
    if not 1 <= count <= 5000:
        raise ValidationFailed(errors={"count": "Generate between 1 and 5,000 codes at a time."})
    alphabet = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"
    out: list[tuple[str, str | None]] = []
    emails = emails or []
    for i in range(count):
        code = "-".join("".join(secrets.choice(alphabet) for _ in range(4)) for _ in range(2))
        email = emails[i].strip().lower() if i < len(emails) and emails[i].strip() else None
        db.add(VoterCode(event_id=event.id, code_hash=_code_hash(code), email=email))
        out.append((code, email))
    audit.record(
        db,
        "voting.codes_generated",
        "event",
        event.public_id,
        event_id=event.id,
        actor_id=organizer.id,
        meta={"count": count},
    )
    db.commit()
    return out


def redeem_code(db: DbSession, event: Event, code: str) -> str:
    """Return the voter key for a valid code and record its first use. The key is derived from
    the code, so redeeming it again (a second device, a cleared browser) yields the same voter
    and the same single ballot — locking it out would only punish the person who holds it."""
    row = db.execute(
        select(VoterCode).where(
            VoterCode.event_id == event.id, VoterCode.code_hash == _code_hash(code)
        )
    ).scalar_one_or_none()
    if row is None:
        raise NotFound("That code isn't valid for this event.")
    if row.expires_at is not None and row.expires_at <= utcnow():
        raise Forbidden("That code has expired.")
    if row.used_at is None:
        db.execute(
            update(VoterCode)
            .where(VoterCode.id == row.id, VoterCode.used_at.is_(None))
            .values(used_at=utcnow())
        )
        db.commit()
    return row.code_hash[:16]


def code_stats(db: DbSession, event: Event) -> tuple[int, int]:
    total = db.execute(
        select(func.count()).select_from(VoterCode).where(VoterCode.event_id == event.id)
    ).scalar_one()
    used = db.execute(
        select(func.count())
        .select_from(VoterCode)
        .where(VoterCode.event_id == event.id, VoterCode.used_at.isnot(None))
    ).scalar_one()
    return total, used


# --- tallies and abuse review ---------------------------------------------------------------------


@dataclass
class ProjectTally:
    project: Project
    votes: int  # sum of credits (votes)
    voters: int
    flagged: int


@dataclass
class Tally:
    projects: list[ProjectTally]
    total_votes: int
    total_voters: int
    voided: int
    flagged: int


def tally(db: DbSession, event: Event) -> Tally:
    rows = db.execute(
        select(
            Project,
            func.coalesce(func.sum(Vote.credits), 0),
            func.count(Vote.id),
            func.coalesce(func.sum(func.iif(Vote.flagged, 1, 0)), 0),
        )
        .outerjoin(Vote, (Vote.project_id == Project.id) & Vote.voided_at.is_(None))
        .where(Project.event_id == event.id, Project.status == ProjectStatus.submitted)
        .group_by(Project.id)
        .order_by(func.coalesce(func.sum(Vote.credits), 0).desc(), Project.title)
    ).all()
    projects = [
        ProjectTally(project=p, votes=int(v), voters=int(n), flagged=int(f)) for p, v, n, f in rows
    ]
    total_voters = db.execute(
        select(func.count(func.distinct(Vote.voter_key))).where(
            Vote.event_id == event.id, Vote.voided_at.is_(None)
        )
    ).scalar_one()
    voided = db.execute(
        select(func.count())
        .select_from(Vote)
        .where(Vote.event_id == event.id, Vote.voided_at.isnot(None))
    ).scalar_one()
    return Tally(
        projects=projects,
        total_votes=sum(p.votes for p in projects),
        total_voters=int(total_voters),
        voided=int(voided),
        flagged=sum(p.flagged for p in projects),
    )


@dataclass
class BallotRow:
    project: Project
    team_name: str
    track_name: str | None
    track_position: int | None
    mine: int
    cost: int
    own_team: bool


def ballot(db: DbSession, event: Event, voter: Voter | None, order_key: str) -> list[BallotRow]:
    """Every submitted project in this voter's fixed random order, with what they have already
    cast on each and what the next vote costs."""
    from podium.models import Team, TeamMember, Track

    rows = db.execute(
        select(Project, Team.name, Track.name, Track.position)
        .join(Team, Team.id == Project.team_id)
        .outerjoin(Track, Track.id == Project.track_id)
        .where(Project.event_id == event.id, Project.status == ProjectStatus.submitted)
        .order_by(Project.id)
    ).all()
    rows = ballot_order(rows, order_key, event.id)
    status = voter_status(db, event, voter)
    my_teams: set[int] = set()
    if voter is not None and voter.user is not None:
        my_teams = set(
            db.execute(
                select(TeamMember.team_id)
                .join(Team, Team.id == TeamMember.team_id)
                .where(Team.event_id == event.id, TeamMember.user_id == voter.user.id)
            ).scalars()
        )
    out = []
    for project, team_name, track_name, track_position in rows:
        mine = status.votes.get(project.id, 0)
        out.append(
            BallotRow(
                project=project,
                team_name=team_name,
                track_name=track_name,
                track_position=track_position,
                mine=mine,
                cost=cost_of_next_vote(mine) if event.quadratic_enabled else 1,
                own_team=project.team_id in my_teams,
            )
        )
    return out


def recent(db: DbSession, event: Event, limit: int = 50) -> list[Vote]:
    """The latest votes, voided ones included, so an organizer can act on any of them."""
    return list(
        db.execute(
            select(Vote).where(Vote.event_id == event.id).order_by(Vote.id.desc()).limit(limit)
        ).scalars()
    )


def suspicious(db: DbSession, event: Event) -> list[Vote]:
    return list(
        db.execute(
            select(Vote)
            .where(Vote.event_id == event.id, Vote.flagged.is_(True), Vote.voided_at.is_(None))
            .order_by(Vote.created_at.desc())
        ).scalars()
    )


def void(db: DbSession, event: Event, organizer: User, vote_id: int, reason: str) -> Vote:
    vote = db.get(Vote, vote_id)
    if vote is None or vote.event_id != event.id:
        raise NotFound("No such vote.")
    reason = reason.strip()
    if not reason:
        raise ValidationFailed(
            errors={"reason": "Say why this vote is being voided; it goes in the audit log."}
        )
    vote.voided_at = utcnow()
    vote.void_reason = reason[:200]
    audit.record(
        db,
        "vote.voided",
        "vote",
        vote.id,
        event_id=event.id,
        actor_id=organizer.id,
        meta={
            "project": vote.project.public_id,
            "reason": vote.void_reason,
            "voter": vote.voter_key[:24],
        },
    )
    db.commit()
    return vote
