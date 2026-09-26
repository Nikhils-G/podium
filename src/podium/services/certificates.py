"""Signed certificates and records. Each is a canonical JSON payload signed with the instance's
ed25519 key; anyone can verify a serial on /verify/{serial} or offline with the public key."""

import base64
import contextlib
import json
import secrets
from dataclasses import dataclass
from pathlib import Path

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey, Ed25519PublicKey
from sqlalchemy import func, select
from sqlalchemy.orm import Session as DbSession

from podium.config import Settings
from podium.errors import Conflict, NotFound
from podium.models import (
    Certificate,
    CertificateKind,
    Event,
    EventRole,
    InstanceSetting,
    Project,
    ProjectStatus,
    Review,
    ReviewStatus,
    Role,
    Team,
    TeamMember,
    Track,
    User,
    utcnow,
)
from podium.services import audit, webhooks

PUBLIC_KEY_SETTING = "signing_public_key"


# --- keys -----------------------------------------------------------------------------------------


def _key_path(settings: Settings) -> Path:
    return settings.data_dir / "keys" / "signing.key"


def private_key(settings: Settings) -> Ed25519PrivateKey:
    path = _key_path(settings)
    if path.exists():
        return serialization.load_pem_private_key(path.read_bytes(), password=None)
    path.parent.mkdir(parents=True, exist_ok=True)
    key = Ed25519PrivateKey.generate()
    pem = key.private_bytes(
        serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()
    )
    path.write_bytes(pem)
    with contextlib.suppress(OSError):
        path.chmod(0o600)
    return key


def public_key_hex(db: DbSession, settings: Settings) -> str:
    row = db.get(InstanceSetting, PUBLIC_KEY_SETTING)
    if row is not None:
        return row.value
    pub = (
        private_key(settings)
        .public_key()
        .public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)
    )
    db.add(InstanceSetting(key=PUBLIC_KEY_SETTING, value=pub.hex()))
    db.commit()
    return pub.hex()


def canonical(payload: dict) -> bytes:
    return json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()


def sign(settings: Settings, payload: dict) -> str:
    return base64.b64encode(private_key(settings).sign(canonical(payload))).decode()


def verify_signature(public_hex: str, payload: dict, signature_b64: str) -> bool:
    try:
        Ed25519PublicKey.from_public_bytes(bytes.fromhex(public_hex)).verify(
            base64.b64decode(signature_b64), canonical(payload)
        )
        return True
    except Exception:
        return False


# --- issuing --------------------------------------------------------------------------------------


def _serial(event: Event) -> str:
    alphabet = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"
    tail = "".join(secrets.choice(alphabet) for _ in range(8))
    return f"PDM-{event.public_id.upper().replace('_', '')}-{tail}"


def issue(
    db: DbSession,
    settings: Settings,
    event: Event,
    kind: CertificateKind,
    *,
    user: User | None,
    team: Team | None,
    details: dict,
    issued_by: User,
) -> Certificate:
    serial = _serial(event)
    payload = {
        "serial": serial,
        "kind": kind.value,
        "event": {"id": event.public_id, "slug": event.slug, "name": event.name},
        "recipient": {"id": user.public_id, "name": user.name} if user else None,
        "team": {"id": team.public_id, "name": team.name} if team else None,
        "issued_at": utcnow().isoformat(),
        "issuer": settings.base_url,
        **details,
    }
    cert = Certificate(
        event_id=event.id,
        kind=kind,
        user_id=user.id if user else None,
        team_id=team.id if team else None,
        serial=serial,
        payload=payload,
        signature=sign(settings, payload),
    )
    db.add(cert)
    db.flush()
    webhooks.emit(
        db,
        event,
        "certificate.issued",
        {"serial": serial, "kind": kind.value, "recipient": user.public_id if user else None},
    )
    audit.record(
        db,
        "certificate.issued",
        "certificate",
        serial,
        event_id=event.id,
        actor_id=issued_by.id,
        meta={"kind": kind.value, "recipient": user.public_id if user else None},
    )
    return cert


def existing(
    db: DbSession, event: Event, kind: CertificateKind, user_id: int | None
) -> Certificate | None:
    return db.execute(
        select(Certificate).where(
            Certificate.event_id == event.id,
            Certificate.kind == kind,
            Certificate.user_id == user_id,
            Certificate.revoked_at.is_(None),
        )
    ).scalar_one_or_none()


@dataclass
class IssueReport:
    issued: int = 0
    skipped: int = 0


def issue_participation(
    db: DbSession, settings: Settings, event: Event, issued_by: User
) -> IssueReport:
    """One certificate per member of every team with a submitted project."""
    report = IssueReport()
    rows = db.execute(
        select(User, Team, Project)
        .join(TeamMember, TeamMember.user_id == User.id)
        .join(Team, Team.id == TeamMember.team_id)
        .join(Project, Project.team_id == Team.id)
        .where(Team.event_id == event.id, Project.status == ProjectStatus.submitted)
        .order_by(Team.id, User.id)
    ).all()
    seen: set[int] = set()
    for user, team, project in rows:
        if user.id in seen:
            continue
        seen.add(user.id)
        if existing(db, event, CertificateKind.participation, user.id):
            report.skipped += 1
            continue
        issue(
            db,
            settings,
            event,
            CertificateKind.participation,
            user=user,
            team=team,
            details={"project": {"id": project.public_id, "title": project.title}},
            issued_by=issued_by,
        )
        report.issued += 1
    db.commit()
    return report


def issue_judge_records(
    db: DbSession, settings: Settings, event: Event, issued_by: User
) -> IssueReport:
    """A signed participation record for every judge who submitted at least one review."""
    report = IssueReport()
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
    for judge in judges:
        n = db.execute(
            select(func.count())
            .select_from(Review)
            .where(
                Review.event_id == event.id,
                Review.judge_id == judge.id,
                Review.status == ReviewStatus.submitted,
            )
        ).scalar_one()
        if not n:
            report.skipped += 1
            continue
        if existing(db, event, CertificateKind.judge, judge.id):
            report.skipped += 1
            continue
        tracks = (
            [
                t
                for t in db.execute(
                    select(Track.name).join(Track.event).where(Track.event_id == event.id)
                ).scalars()
            ]
            if False
            else []
        )
        issue(
            db,
            settings,
            event,
            CertificateKind.judge,
            user=judge,
            team=None,
            details={"reviews_submitted": int(n), "tracks": tracks},
            issued_by=issued_by,
        )
        report.issued += 1
    db.commit()
    return report


def issue_winners(
    db: DbSession, settings: Settings, event: Event, issued_by: User, top: int = 3
) -> IssueReport:
    from podium.services import scoring

    if event.results_published_at is None:
        raise Conflict("Publish results before issuing winner certificates.")
    report = IssueReport()
    awarded = sorted(
        (p for p in event.prizes if p.project_id is not None), key=lambda p: p.position
    )
    if awarded:
        # organizers awarded prizes explicitly: those are the winners
        for prize in awarded:
            members = (
                db.execute(
                    select(User)
                    .join(TeamMember, TeamMember.user_id == User.id)
                    .where(TeamMember.team_id == prize.project.team_id)
                )
                .scalars()
                .all()
            )
            for member in members:
                if existing(db, event, CertificateKind.winner, member.id):
                    report.skipped += 1
                    continue
                issue(
                    db,
                    settings,
                    event,
                    CertificateKind.winner,
                    user=member,
                    team=prize.project.team,
                    details={
                        "project": {"id": prize.project.public_id, "title": prize.project.title},
                        "prize": {
                            "id": prize.public_id,
                            "name": prize.name,
                            "track": prize.track.name if prize.track else None,
                        },
                    },
                    issued_by=issued_by,
                )
                report.issued += 1
        db.commit()
        return report
    results = scoring.compute(db, event)  # no awards: fall back to the top three
    ranked = [
        p
        for p in results.projects
        if (p.rank_norm if results.basis.value == "normalized" else p.rank_raw)
    ]
    for p in ranked:
        rank = p.rank_norm if results.basis.value == "normalized" else p.rank_raw
        if rank > top:
            break
        members = (
            db.execute(
                select(User)
                .join(TeamMember, TeamMember.user_id == User.id)
                .where(TeamMember.team_id == p.project.team_id)
            )
            .scalars()
            .all()
        )
        for member in members:
            if existing(db, event, CertificateKind.winner, member.id):
                report.skipped += 1
                continue
            issue(
                db,
                settings,
                event,
                CertificateKind.winner,
                user=member,
                team=p.project.team,
                details={
                    "project": {"id": p.project.public_id, "title": p.project.title},
                    "rank": rank,
                },
                issued_by=issued_by,
            )
            report.issued += 1
    db.commit()
    return report


# --- lookup / verify ------------------------------------------------------------------------------


@dataclass
class Verification:
    status: str  # valid | revoked | invalid | not_found
    certificate: Certificate | None
    public_key: str


def by_serial(db: DbSession, serial: str) -> Certificate:
    cert = db.execute(
        select(Certificate).where(Certificate.serial == serial.strip().upper())
    ).scalar_one_or_none()
    if cert is None:
        raise NotFound("No certificate with that serial.")
    return cert


def verify(db: DbSession, settings: Settings, serial: str) -> Verification:
    public = public_key_hex(db, settings)
    try:
        cert = by_serial(db, serial)
    except NotFound:
        return Verification("not_found", None, public)
    if not verify_signature(public, cert.payload, cert.signature):
        return Verification("invalid", cert, public)
    if cert.revoked_at is not None:
        return Verification("revoked", cert, public)
    return Verification("valid", cert, public)


def revoke(db: DbSession, event: Event, user: User, cert: Certificate) -> Certificate:
    cert.revoked_at = utcnow()
    audit.record(
        db, "certificate.revoked", "certificate", cert.serial, event_id=event.id, actor_id=user.id
    )
    db.commit()
    return cert


def for_event(db: DbSession, event: Event) -> list[Certificate]:
    return list(
        db.execute(
            select(Certificate)
            .where(Certificate.event_id == event.id)
            .order_by(Certificate.id.desc())
        ).scalars()
    )


def for_user(db: DbSession, event: Event, user: User) -> list[Certificate]:
    return list(
        db.execute(
            select(Certificate)
            .where(Certificate.event_id == event.id, Certificate.user_id == user.id)
            .order_by(Certificate.id.desc())
        ).scalars()
    )
