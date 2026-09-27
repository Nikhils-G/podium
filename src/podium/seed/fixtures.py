"""Import a hackathon in the DOGFOOD fixtures shape. Idempotent: rows are matched by their
public ids (fixture ids are kept as public ids) and updated in place, so re-running is safe."""

import json
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.orm import Session as DbSession

from podium.models import (
    Assignment,
    AssignmentMethod,
    AssignmentStatus,
    Event,
    EventRole,
    JudgeTrack,
    MemberRole,
    Project,
    ProjectStatus,
    Review,
    ReviewStatus,
    Role,
    RubricCriterion,
    ScoreItem,
    Team,
    TeamMember,
    Track,
    User,
)
from podium.models.base import new_public_id
from podium.security.passwords import hash_password


@dataclass
class ImportReport:
    event_slug: str = ""
    counts: dict[str, int] = field(default_factory=dict)
    duplicates: list[tuple[str, str]] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)


def parse_ts(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(UTC)


def slugify(name: str) -> str:
    out = "".join(ch.lower() if ch.isalnum() else "-" for ch in name).strip("-")
    while "--" in out:
        out = out.replace("--", "-")
    return out[:80] or "event"


def _user_by_email(db: DbSession, email: str) -> User | None:
    return db.execute(select(User).where(User.email == email.lower())).scalar_one_or_none()


def _ensure_user(
    db: DbSession, email: str, name: str, password_hash: str, report: ImportReport
) -> User:
    user = _user_by_email(db, email)
    if user is None:
        user = User(email=email.lower(), name=name, password_hash=password_hash)
        db.add(user)
        db.flush()
        report.counts["accounts"] = report.counts.get("accounts", 0) + 1
    return user


def _ensure_role(db: DbSession, event: Event, user: User, role: Role, report: ImportReport) -> None:
    row = db.execute(
        select(EventRole).where(EventRole.event_id == event.id, EventRole.user_id == user.id)
    ).scalar_one_or_none()
    if row is None:
        db.add(EventRole(event_id=event.id, user_id=user.id, role=role))
    elif row.role != role:
        report.warnings.append(
            f"{user.email} is already {row.role} in this event; not changed to {role}"
        )


def foreign_ids(db: DbSession, data: dict, event: Event | None) -> list[str]:
    """Ids in the file that already belong to another event (any match, for a new event).
    Rows are matched by instance-wide public ids, so without this check a file could rewrite
    another event's tracks, teams, projects or prizes."""
    ext = data.get("podium") if isinstance(data.get("podium"), dict) else {}
    sections = [data.get(k) for k in ("tracks", "teams", "projects")] + [ext.get("prizes")]
    ids = {
        row["id"]
        for rows in sections
        if isinstance(rows, list)
        for row in rows
        if isinstance(row, dict) and isinstance(row.get("id"), str)
    }
    if not ids:
        return []
    from podium.models import Prize

    found: set[str] = set()
    for model in (Track, Team, Project, Prize):
        query = select(model.public_id).where(model.public_id.in_(ids))
        if event is not None:
            query = query.where(model.event_id != event.id)
        found.update(db.execute(query).scalars())
    return sorted(found)


def import_fixtures_file(db: DbSession, path: Path, *, default_password: str) -> ImportReport:
    with open(path, encoding="utf-8") as f:
        data = json.load(f)
    return import_fixtures(db, data, default_password=default_password)


def import_fixtures(
    db: DbSession, data: dict, *, default_password: str, commit: bool = True
) -> ImportReport:
    report = ImportReport()
    counts = report.counts
    password_hash = hash_password(default_password)  # one hash shared by all demo users

    # --- event -----------------------------------------------------------
    ev = data["event"]
    event = db.execute(select(Event).where(Event.public_id == ev["id"])).scalar_one_or_none()
    closes = parse_ts(ev["submissions_close"])
    if event is None:
        event = Event(public_id=ev["id"], slug=slugify(ev["name"]), name=ev["name"])
        db.add(event)
        counts["events"] = 1
    event.name = ev["name"]
    if ev.get("description"):
        event.description = ev["description"]
    elif not event.description:
        event.description = (
            f"{len(data.get('projects', []))} projects across "
            f"{len(data.get('tracks', []))} tracks, reviewed by "
            f"{len(data.get('judges', []))} judges."
        )
    event.is_public = True
    event.submissions_close_at = closes
    event.submissions_open_at = event.submissions_open_at or closes - timedelta(days=30)
    if data.get("scores") and event.judging_opened_at is None:
        # the file carries reviews, so judging evidently happened: open it so the console has data
        event.judging_opened_at = closes + timedelta(hours=1)
    db.flush()
    report.event_slug = event.slug

    # --- tracks ----------------------------------------------------------
    tracks: dict[str, Track] = {}
    for position, t in enumerate(data.get("tracks", [])):
        track = db.execute(select(Track).where(Track.public_id == t["id"])).scalar_one_or_none()
        if track is None:
            track = Track(public_id=t["id"], event_id=event.id, name=t["name"])
            db.add(track)
            counts["tracks"] = counts.get("tracks", 0) + 1
        track.name = t["name"]
        track.position = position
        tracks[t["id"]] = track
    db.flush()

    # --- judges ----------------------------------------------------------
    judges: dict[str, User] = {}
    for j in data.get("judges", []):
        user = _user_by_email(db, j["email"])
        if user is None:
            user = User(
                public_id=j["id"],
                email=j["email"].lower(),
                name=j["name"],
                password_hash=password_hash,
            )
            db.add(user)
            db.flush()
            counts["judges"] = counts.get("judges", 0) + 1
            counts["accounts"] = counts.get("accounts", 0) + 1
        judges[j["id"]] = user
        _ensure_role(db, event, user, Role.judge, report)
        for track_id in j.get("tracks", []):
            if track_id not in tracks:
                report.warnings.append(f"judge {j['id']} lists unknown track {track_id}")
                continue
            exists = db.execute(
                select(JudgeTrack.id).where(
                    JudgeTrack.event_id == event.id,
                    JudgeTrack.user_id == user.id,
                    JudgeTrack.track_id == tracks[track_id].id,
                )
            ).scalar()
            if exists is None:
                db.add(JudgeTrack(event_id=event.id, user_id=user.id, track_id=tracks[track_id].id))
    db.flush()

    # --- teams and members -------------------------------------------------
    teams: dict[str, Team] = {}
    for t in data.get("teams", []):
        team = db.execute(select(Team).where(Team.public_id == t["id"])).scalar_one_or_none()
        if team is None:
            team = Team(
                public_id=t["id"],
                event_id=event.id,
                name=t["name"],
                invite_code=new_public_id("join", 10),
            )
            db.add(team)
            db.flush()
            counts["teams"] = counts.get("teams", 0) + 1
        team.name = t["name"]
        teams[t["id"]] = team
        for index, email in enumerate(t.get("members", [])):
            name = email.split("@")[0].replace(".", " ").replace("_", " ").title()
            user = _ensure_user(db, email, name, password_hash, report)
            _ensure_role(db, event, user, Role.participant, report)
            member = db.execute(
                select(TeamMember).where(
                    TeamMember.team_id == team.id, TeamMember.user_id == user.id
                )
            ).scalar_one_or_none()
            if member is None:
                db.add(
                    TeamMember(
                        team_id=team.id,
                        user_id=user.id,
                        role=MemberRole.lead if index == 0 else MemberRole.member,
                    )
                )
                counts["members"] = counts.get("members", 0) + 1
    db.flush()

    # --- projects (with duplicate detection) -------------------------------
    projects: dict[str, Project] = {}
    seen_repo: dict[tuple[int, str], Project] = {}
    for p in sorted(data.get("projects", []), key=lambda x: x.get("submitted_at", "")):
        project = db.execute(
            select(Project).where(Project.public_id == p["id"])
        ).scalar_one_or_none()
        team = teams.get(p["team"])
        if team is None:
            report.warnings.append(f"project {p['id']} references unknown team {p['team']}")
            continue
        if project is None:
            project = Project(
                public_id=p["id"], event_id=event.id, team_id=team.id, title=p["title"]
            )
            db.add(project)
            counts["projects"] = counts.get("projects", 0) + 1
        project.team_id = team.id
        project.track_id = tracks[p["track"]].id if p.get("track") in tracks else None
        project.title = p["title"]
        project.summary = p.get("summary", "") or ""
        project.repo_url = p.get("repo_url", "") or ""
        project.status = ProjectStatus.submitted
        project.submitted_at = parse_ts(p["submitted_at"]) if p.get("submitted_at") else closes
        # the explicit value also wins over onupdate on a re-seed
        project.created_at = project.updated_at = project.submitted_at
        key = (team.id, project.repo_url or project.title.lower())
        if key in seen_repo and seen_repo[key] is not project:
            project.duplicate_of_id = seen_repo[key].id
            report.duplicates.append((p["id"], seen_repo[key].public_id))
        else:
            seen_repo[key] = project
        projects[p["id"]] = project
        db.flush()

    # --- rubric: criteria are the union of keys seen in scores -------------
    keys: list[str] = []
    for s in data.get("scores", []):
        for k in s.get("criteria", {}):
            if k not in keys:
                keys.append(k)
    criteria: dict[str, RubricCriterion] = {}
    for position, key in enumerate(keys):
        crit = db.execute(
            select(RubricCriterion).where(
                RubricCriterion.event_id == event.id, RubricCriterion.key == key
            )
        ).scalar_one_or_none()
        if crit is None:
            crit = RubricCriterion(
                event_id=event.id,
                key=key,
                name=key.capitalize(),
                position=position,
                min_score=1,
                max_score=5,
                weight=1.0,
            )
            db.add(crit)
            counts["criteria"] = counts.get("criteria", 0) + 1
        criteria[key] = crit
    db.flush()

    # --- scores → assignments + reviews + score items ----------------------
    for index, s in enumerate(data.get("scores", [])):
        judge = judges.get(s["judge"])
        project = projects.get(s["project"])
        if judge is None or project is None:
            report.warnings.append(f"score #{index} references unknown judge/project")
            continue
        assignment = db.execute(
            select(Assignment).where(
                Assignment.judge_id == judge.id, Assignment.project_id == project.id
            )
        ).scalar_one_or_none()
        if assignment is None:
            assignment = Assignment(
                event_id=event.id,
                judge_id=judge.id,
                project_id=project.id,
                method=AssignmentMethod.imported,
            )
            db.add(assignment)
            db.flush()
        assignment.status = AssignmentStatus.done
        review = db.execute(
            select(Review).where(Review.assignment_id == assignment.id)
        ).scalar_one_or_none()
        if review is None:
            review = Review(
                assignment_id=assignment.id,
                event_id=event.id,
                judge_id=judge.id,
                project_id=project.id,
            )
            db.add(review)
            db.flush()
            counts["reviews"] = counts.get("reviews", 0) + 1
        review.comment = s.get("comment", "") or ""
        review.status = ReviewStatus.submitted
        review.submitted_at = event.judging_opened_at + timedelta(minutes=index)
        for key, value in s.get("criteria", {}).items():
            item = db.execute(
                select(ScoreItem).where(
                    ScoreItem.review_id == review.id, ScoreItem.criterion_id == criteria[key].id
                )
            ).scalar_one_or_none()
            if item is None:
                db.add(
                    ScoreItem(review_id=review.id, criterion_id=criteria[key].id, value=int(value))
                )
            else:
                item.value = int(value)
    _apply_extension(
        db, event, data.get("podium") or {}, tracks, projects, criteria, judges, report
    )
    db.flush()
    if commit:
        db.commit()
    return report


def _apply_extension(db, event, ext: dict, tracks, projects, criteria, judges, report) -> None:
    """The `podium` block of an export: everything the base fixtures shape can't carry."""
    if not ext:
        return
    from podium.models import (
        Assignment,
        AssignmentMethod,
        AssignmentStatus,
        NormalizationMethod,
        Prize,
        ProjectStatus,
        RankingBasis,
        VotingMode,
    )

    ev = ext.get("event") or {}
    if ev.get("description") is not None:
        event.description = ev["description"]
    for name in (
        "submissions_open_at",
        "judging_opened_at",
        "judging_closed_at",
        "voting_open_at",
        "voting_close_at",
        "results_published_at",
    ):
        if ev.get(name):
            setattr(event, name, parse_ts(ev[name]))
    for name in (
        "quadratic_enabled",
        "voting_credits",
        "reviews_per_project",
        "max_team_size",
        "is_public",
    ):
        if ev.get(name) is not None:
            setattr(event, name, ev[name])
    if ev.get("voting_mode") in VotingMode.__members__:
        event.voting_mode = VotingMode(ev["voting_mode"])
    if ev.get("normalization_method") in NormalizationMethod.__members__:
        event.normalization_method = NormalizationMethod(ev["normalization_method"])
    if ev.get("published_ranking") in RankingBasis.__members__:
        event.published_ranking = RankingBasis(ev["published_ranking"])
    for t in ext.get("tracks") or []:
        if t.get("id") in tracks and t.get("description") is not None:
            tracks[t["id"]].description = t["description"]
    for position, pz in enumerate(ext.get("prizes") or []):
        prize = db.execute(
            select(Prize).where(Prize.public_id == pz.get("id", ""))
        ).scalar_one_or_none()
        if prize is None:
            prize = Prize(
                public_id=pz.get("id") or new_public_id("prz"),
                event_id=event.id,
                name=pz.get("name", "Prize"),
            )
            db.add(prize)
            report.counts["prizes"] = report.counts.get("prizes", 0) + 1
        prize.name = pz.get("name", prize.name)
        prize.amount_text = pz.get("amount", "") or ""
        prize.description = pz.get("description", "") or ""
        prize.position = position
        prize.track_id = tracks[pz["track"]].id if pz.get("track") in tracks else None
        winner = pz.get("project")
        prize.project_id = projects[winner].id if winner in projects else None
    for position, c in enumerate(ext.get("rubric") or []):
        crit = criteria.get(c.get("key"))
        if crit is None:
            crit = RubricCriterion(
                event_id=event.id, key=c["key"], name=c.get("name", c["key"]), position=position
            )
            db.add(crit)
            criteria[c["key"]] = crit
            report.counts["criteria"] = report.counts.get("criteria", 0) + 1
        crit.name = c.get("name", crit.name)
        crit.description = c.get("description", "") or ""
        crit.weight = float(c.get("weight", crit.weight))
        crit.min_score = int(c.get("min", crit.min_score))
        crit.max_score = int(c.get("max", crit.max_score))
        crit.position = position
    for px in ext.get("projects") or []:
        project = projects.get(px.get("id"))
        if project is None:
            continue
        for name in ("description", "demo_url", "video_url"):
            if px.get(name) is not None:
                setattr(project, name, px[name])
        if px.get("status") in ProjectStatus.__members__:
            project.status = ProjectStatus(px["status"])
    for ax in ext.get("assignments") or []:
        judge, project = judges.get(ax.get("judge")), projects.get(ax.get("project"))
        if judge is None or project is None:
            continue
        exists = db.execute(
            select(Assignment).where(
                Assignment.judge_id == judge.id, Assignment.project_id == project.id
            )
        ).scalar_one_or_none()
        if exists is None:
            db.add(
                Assignment(
                    event_id=event.id,
                    judge_id=judge.id,
                    project_id=project.id,
                    method=AssignmentMethod(ax.get("method", "import"))
                    if ax.get("method") in AssignmentMethod._value2member_map_
                    else AssignmentMethod.imported,
                    status=AssignmentStatus(ax.get("status", "pending"))
                    if ax.get("status") in AssignmentStatus._value2member_map_
                    else AssignmentStatus.pending,
                )
            )
            report.counts["assignments"] = report.counts.get("assignments", 0) + 1
    db.flush()
