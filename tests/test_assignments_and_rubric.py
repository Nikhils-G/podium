import pytest
from sqlalchemy import select

from podium.errors import Conflict
from podium.models import Assignment, Event, Project, TeamMember, User
from podium.services import assignments as svc
from podium.services import rubric
from tests.conftest import SLUG


def _event(db):
    return db.execute(select(Event).where(Event.slug == SLUG)).scalar_one()


def test_plan_is_track_aware_conflict_free_and_deterministic(db):
    event = _event(db)
    plan_a = svc.build_plan(db, event, reviews_per_project=4, seed=7)
    plan_b = svc.build_plan(db, event, reviews_per_project=4, seed=7)
    assert [(e.judge.id, e.project.id) for e in plan_a.entries] == [
        (e.judge.id, e.project.id) for e in plan_b.entries
    ]
    assert plan_a.entries, "fixtures have projects with fewer than 4 reviews, so there is work"
    pairs = {(e.judge.id, e.project.id) for e in plan_a.entries}
    assert len(pairs) == len(plan_a.entries), "no judge is planned twice for one project"
    existing = {(a.judge_id, a.project_id) for a in db.execute(select(Assignment)).scalars()}
    assert not (pairs & existing)
    judges, tracks = svc.judge_pool(db, event)
    for e in plan_a.entries:
        if not e.fallback:
            assert e.project.track_id in tracks.get(e.judge.id, set())
    members = {
        (m.user_id, p.id)
        for m, p in db.execute(
            select(TeamMember, Project).join(Project, Project.team_id == TeamMember.team_id)
        ).all()
    }
    assert not (pairs & members)


def test_plan_balances_load_from_a_clean_slate(db):
    """Six judges who can judge anything, twelve projects, three reviews each → 36 assignments,
    six per judge, and the same plan for the same seed."""
    from podium.models import EventRole, MemberRole, ProjectStatus, Role, Team, TeamMember, utcnow
    from podium.models.base import new_public_id

    event = Event(
        slug="balance-" + new_public_id("x", 4),
        name="Balance",
        is_public=True,
        submissions_close_at=utcnow(),
    )
    db.add(event)
    db.flush()
    judges = []
    for i in range(6):
        u = User(email=f"bal-judge-{i}-{event.slug}@x.test", name=f"Judge {i}", password_hash="x")
        db.add(u)
        db.flush()
        db.add(EventRole(event_id=event.id, user_id=u.id, role=Role.judge))
        judges.append(u)
    for i in range(12):
        u = User(email=f"bal-team-{i}-{event.slug}@x.test", name=f"Maker {i}", password_hash="x")
        db.add(u)
        db.flush()
        team = Team(event_id=event.id, name=f"Team {i}", invite_code=new_public_id("join", 10))
        db.add(team)
        db.flush()
        db.add(TeamMember(team_id=team.id, user_id=u.id, role=MemberRole.lead))
        db.add(
            Project(
                event_id=event.id,
                team_id=team.id,
                title=f"Project {i}",
                status=ProjectStatus.submitted,
                submitted_at=utcnow(),
            )
        )
    db.commit()
    plan = svc.build_plan(db, event, reviews_per_project=3, seed=3)
    assert len(plan.entries) == 36 and not plan.shortfalls
    loads = sorted(plan.loads_after.values())
    assert loads == [6] * 6, loads
    again = svc.build_plan(db, event, reviews_per_project=3, seed=3)
    assert [(e.judge.id, e.project.id) for e in again.entries] == [
        (e.judge.id, e.project.id) for e in plan.entries
    ]
    different = svc.build_plan(db, event, reviews_per_project=3, seed=4)
    assert [(e.judge.id, e.project.id) for e in different.entries] != [
        (e.judge.id, e.project.id) for e in plan.entries
    ]
    organizer = db.execute(select(User).where(User.email == "organizer@podium.local")).scalar_one()
    assert svc.apply_plan(db, event, organizer, plan) == 36
    assert svc.build_plan(db, event, reviews_per_project=3, seed=3).entries == []


def test_plan_reports_shortfalls_when_no_judges(db):
    event = _event(db)
    huge = svc.build_plan(db, event, reviews_per_project=20, seed=1)
    assert huge.shortfalls, (
        "cannot reach 20 reviews per project with 30 judges spread across tracks"
    )


def test_rubric_locks_when_judging_open_but_weights_stay_editable(db):
    event = _event(db)  # fixture event has judging opened
    organizer = db.execute(select(User).where(User.email == "organizer@podium.local")).scalar_one()
    assert rubric.is_locked(event)
    with pytest.raises(Conflict):
        rubric.add_criterion(db, event, organizer, "Late criterion")
    crit = rubric.criteria(db, event)[0]
    updated = rubric.update_criterion(db, event, organizer, crit, weight=2.5)
    assert updated.weight == 2.5
    with pytest.raises(Conflict):
        rubric.update_criterion(db, event, organizer, crit, min_score=0, max_score=10)
    rubric.update_criterion(db, event, organizer, crit, weight=1.0)  # restore


def test_judge_can_reopen_and_resubmit_but_never_score_unassigned(client, auth):
    S = f"/api/v1/events/{SLUG}"
    mine = client.get(f"{S}/judges/me/queue", headers=auth("judge_a")).json()
    assert mine["judging_open"] and len(mine["queue"]) == 11
    pid = mine["queue"][0]["project"]
    r = client.get(f"{S}/reviews/{pid}", headers=auth("judge_a"))
    assert r.status_code == 200 and r.json()["review"]["status"] == "submitted"
    original = r.json()["review"]["scores"]
    assert client.post(f"{S}/reviews/{pid}/reopen", headers=auth("judge_a")).status_code == 200
    partial = {next(iter(original)): 5}
    r = client.put(
        f"{S}/reviews/{pid}", headers=auth("judge_a"), json={"scores": partial, "submit": True}
    )
    assert r.status_code == 422, "submitting requires every criterion"
    r = client.put(
        f"{S}/reviews/{pid}", headers=auth("judge_a"), json={"scores": partial, "submit": False}
    )
    assert r.status_code == 200 and r.json()["review"]["status"] == "draft"
    r = client.put(
        f"{S}/reviews/{pid}",
        headers=auth("judge_a"),
        json={"scores": original, "comment": "restored", "submit": True},
    )
    assert r.status_code == 200 and r.json()["review"]["status"] == "submitted"
    other = client.get(f"{S}/judges/jdg_26/reviews", headers=auth("organizer")).json()["reviews"][
        0
    ]["project"]
    assert other not in {q["project"] for q in mine["queue"]}
    r = client.put(
        f"{S}/reviews/{other}", headers=auth("judge_a"), json={"scores": original, "submit": True}
    )
    assert r.status_code == 403
    assert (
        client.put(
            f"{S}/reviews/{pid}", headers=auth("participant"), json={"scores": {}}
        ).status_code
        == 403
    )


def test_manual_assignment_and_removal_rules(client, auth):
    S = f"/api/v1/events/{SLUG}"
    judges = client.get(f"{S}/judges", headers=auth("organizer")).json()["judges"]
    judge = next(j for j in judges if j["assigned"] < 3)
    queue = client.get(f"{S}/assignments", headers=auth("organizer")).json()
    assigned = {
        a["project"] for j in queue["judges"] if j["id"] == judge["id"] for a in j["assignments"]
    }
    target = next(
        p["id"] for p in client.get(f"{S}/projects").json()["projects"] if p["id"] not in assigned
    )
    r = client.post(
        f"{S}/assignments",
        headers=auth("organizer"),
        json={"judge": judge["id"], "projects": [target]},
    )
    assert r.status_code == 201 and r.json()["created"] == 1
    r = client.post(
        f"{S}/assignments",
        headers=auth("organizer"),
        json={"judge": judge["id"], "projects": [target]},
    )
    assert r.json()["created"] == 0, "idempotent"
    rows = client.get(f"{S}/assignments", headers=auth("organizer")).json()["judges"]
    new = next(
        a
        for j in rows
        if j["id"] == judge["id"]
        for a in j["assignments"]
        if a["project"] == target
    )
    assert (
        client.delete(f"{S}/assignments/{new['id']}", headers=auth("organizer")).status_code == 204
    )
    done = next(a for j in rows for a in j["assignments"] if a["status"] == "done")
    assert (
        client.delete(f"{S}/assignments/{done['id']}", headers=auth("organizer")).status_code == 409
    )
    assert (
        client.post(
            f"{S}/assignments",
            headers=auth("judge_a"),
            json={"judge": judge["id"], "projects": [target]},
        ).status_code
        == 403
    )
