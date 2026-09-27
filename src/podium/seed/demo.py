"""Demo identities the acceptance checker and the demo video use. Their session tokens are
derived from the secret key, so the committed .dogfood.toml keeps working on a fresh install."""

from dataclasses import dataclass

from sqlalchemy import func, select
from sqlalchemy.orm import Session as DbSession

from podium.config import Settings
from podium.models import Event, EventRole, Review, Role, Team, TeamMember, User
from podium.security.passwords import hash_password
from podium.security.sessions import create_session, demo_session_token

DEMO_EVENT_PUBLIC_ID = "evt_01"


@dataclass
class DemoLogin:
    role: str
    email: str
    cookie: str
    note: str = ""


def _ensure_user(db: DbSession, email: str, name: str, password: str, *, admin=False) -> User:
    user = db.execute(select(User).where(User.email == email)).scalar_one_or_none()
    if user is None:
        user = User(email=email, name=name, password_hash=hash_password(password), is_admin=admin)
        db.add(user)
        db.flush()
    elif admin and not user.is_admin:
        user.is_admin = True  # a demo identity promoted in a later release keeps working
    return user


def _ensure_role(db: DbSession, event: Event, user: User, role: Role) -> None:
    row = db.execute(
        select(EventRole).where(EventRole.event_id == event.id, EventRole.user_id == user.id)
    ).scalar_one_or_none()
    if row is None:
        db.add(EventRole(event_id=event.id, user_id=user.id, role=role))


def ensure_demo_accounts(db: DbSession, settings: Settings) -> list[DemoLogin]:
    event = db.execute(
        select(Event).where(Event.public_id == DEMO_EVENT_PUBLIC_ID)
    ).scalar_one_or_none()
    if event is None:
        event = db.execute(select(Event).order_by(Event.id)).scalars().first()
    logins: list[DemoLogin] = []
    password = settings.demo_password

    admin = _ensure_user(db, "admin@podium.local", "Podium Admin", password, admin=True)
    # The demo organizer is also an instance admin so the demo can create events while event
    # creation stays closed to everyone else (judges, participants) by default.
    organizer = _ensure_user(db, "organizer@podium.local", "Olivia Organizer", password, admin=True)
    people: list[tuple[str, User, str]] = [("admin", admin, "")]
    if event is not None:
        _ensure_role(db, event, organizer, Role.organizer)
        people.append(("organizer", organizer, ""))
        # judge_a = the judge with the most reviews; judge_b = the runner-up
        ranked = db.execute(
            select(Review.judge_id, func.count(Review.id).label("n"))
            .where(Review.event_id == event.id)
            .group_by(Review.judge_id)
            .order_by(func.count(Review.id).desc(), Review.judge_id)
            .limit(2)
        ).all()
        if len(ranked) >= 2:
            judge_a = db.get(User, ranked[0][0])
            judge_b = db.get(User, ranked[1][0])
            people.append(("judge_a", judge_a, f"{judge_a.public_id}, {ranked[0][1]} reviews"))
            people.append(("judge_b", judge_b, f"{judge_b.public_id}, {ranked[1][1]} reviews"))
        member = (
            db.execute(
                select(User)
                .join(TeamMember, TeamMember.user_id == User.id)
                .join(Team, Team.id == TeamMember.team_id)
                .where(Team.event_id == event.id)
                .order_by(Team.id, TeamMember.id)
            )
            .scalars()
            .first()
        )
        if member is not None:
            people.append(("participant", member, ""))
    else:
        people.append(("organizer", organizer, "no event yet"))

    for role, user, note in people:
        token = demo_session_token(settings.secret_key, role)
        create_session(db, user, days=3650, label=f"demo:{role}", token=token)
        logins.append(
            DemoLogin(role=role, email=user.email, cookie=f"Cookie: session={token}", note=note)
        )
    db.commit()
    return logins
