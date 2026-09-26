"""Pairwise judging with a Bradley-Terry estimator.

Judges compare two of their assigned projects at a time. Strengths p_i are fitted with Hunter's
minorization-maximization iteration  p_i ← W_i / Σ_j n_ij / (p_i + p_j), with one virtual win and
one virtual loss for every project against a fixed reference of strength 1 so the comparison graph
is always connected and every strength is finite. Strengths are normalised to geometric mean 1
and reported as log-strength; the ranking is compared to the normalized-score ranking with
Spearman's ρ."""

import math
import random
from dataclasses import dataclass, field
from itertools import combinations

from sqlalchemy import func, select
from sqlalchemy.orm import Session as DbSession

from podium.errors import Closed, Forbidden, NotFound, ValidationFailed
from podium.models import (
    Assignment,
    ComparisonSource,
    Event,
    PairwiseComparison,
    Project,
    ProjectStatus,
    Review,
    ReviewStatus,
    User,
)
from podium.services import audit, scoring
from podium.services.reviews import judging_is_open

SKIP_REASONS = ("cannot_decide", "conflict", "not_enough_info")


# --- choosing pairs -----------------------------------------------------------------------------


def assigned_projects(db: DbSession, event: Event, judge: User) -> list[Project]:
    return list(
        db.execute(
            select(Project)
            .join(Assignment, Assignment.project_id == Project.id)
            .where(
                Assignment.event_id == event.id,
                Assignment.judge_id == judge.id,
                Project.status == ProjectStatus.submitted,
            )
            .order_by(Project.id)
        ).scalars()
    )


def _pair_counts(db: DbSession, event: Event, judge: User) -> dict[tuple[int, int], int]:
    counts: dict[tuple[int, int], int] = {}
    for a, b in db.execute(
        select(PairwiseComparison.project_a_id, PairwiseComparison.project_b_id).where(
            PairwiseComparison.event_id == event.id, PairwiseComparison.judge_id == judge.id
        )
    ).all():
        key = (min(a, b), max(a, b))
        counts[key] = counts.get(key, 0) + 1
    return counts


@dataclass
class CompareState:
    pair: tuple[Project, Project] | None
    done: int
    total: int
    projects: int


def next_pair(db: DbSession, event: Event, judge: User) -> CompareState:
    """The least-compared pair among the judge's assignments (ties broken randomly), until every
    pair has been seen once."""
    projects = assigned_projects(db, event, judge)
    pairs = list(combinations(projects, 2))
    counts = _pair_counts(db, event, judge)
    done = sum(1 for a, b in pairs if counts.get((min(a.id, b.id), max(a.id, b.id)), 0) > 0)
    if not pairs:
        return CompareState(None, 0, 0, len(projects))
    remaining = [(a, b) for a, b in pairs if counts.get((min(a.id, b.id), max(a.id, b.id)), 0) == 0]
    if not remaining:
        return CompareState(None, done, len(pairs), len(projects))
    rng = random.Random(f"{event.id}:{judge.id}:{done}")
    a, b = rng.choice(remaining)
    if rng.random() < 0.5:  # never always show the lower id on the left
        a, b = b, a
    return CompareState((a, b), done, len(pairs), len(projects))


def record(
    db: DbSession,
    event: Event,
    judge: User,
    a_public: str,
    b_public: str,
    winner_public: str | None,
    skip_reason: str | None = None,
    *,
    ip_hash: str | None = None,
) -> PairwiseComparison:
    if not judging_is_open(event):
        raise Closed("Judging isn't open, so comparisons can't be recorded.")
    mine = {p.public_id: p for p in assigned_projects(db, event, judge)}
    if a_public not in mine or b_public not in mine:
        raise Forbidden("You can only compare projects assigned to you.")
    if a_public == b_public:
        raise ValidationFailed(errors={"pair": "Pick two different projects."})
    if winner_public is None:
        if skip_reason not in SKIP_REASONS:
            raise ValidationFailed(errors={"skip_reason": "Say why you're skipping."})
    elif winner_public not in (a_public, b_public):
        raise ValidationFailed(errors={"winner": "The winner must be one of the two projects."})
    row = PairwiseComparison(
        event_id=event.id,
        judge_id=judge.id,
        project_a_id=mine[a_public].id,
        project_b_id=mine[b_public].id,
        winner_id=mine[winner_public].id if winner_public else None,
        skip_reason=skip_reason if winner_public is None else None,
        source=ComparisonSource.judge,
    )
    db.add(row)
    db.flush()
    audit.record(
        db,
        "comparison.recorded",
        "comparison",
        row.id,
        event_id=event.id,
        actor_id=judge.id,
        meta={"a": a_public, "b": b_public, "winner": winner_public, "skip": row.skip_reason},
        ip_hash=ip_hash,
    )
    db.commit()
    return row


def undo_last(db: DbSession, event: Event, judge: User) -> bool:
    row = (
        db.execute(
            select(PairwiseComparison)
            .where(
                PairwiseComparison.event_id == event.id,
                PairwiseComparison.judge_id == judge.id,
                PairwiseComparison.source == ComparisonSource.judge,
            )
            .order_by(PairwiseComparison.id.desc())
        )
        .scalars()
        .first()
    )
    if row is None:
        return False
    audit.record(
        db, "comparison.undone", "comparison", row.id, event_id=event.id, actor_id=judge.id
    )
    db.delete(row)
    db.commit()
    return True


# --- estimator ------------------------------------------------------------------------------------


def bradley_terry(
    items: list[int],
    comparisons: list[tuple[int, int]],
    *,
    prior: float = 1.0,
    iterations: int = 500,
    tolerance: float = 1e-7,
) -> dict[int, float]:
    """Return log-strength per item (geometric mean 1 → mean log 0). `comparisons` are
    (winner, loser) pairs. Items never compared end at the reference strength."""
    if not items:
        return {}
    wins = dict.fromkeys(items, 0.0)
    n: dict[tuple[int, int], float] = {}
    for w, loser in comparisons:
        if w not in wins or loser not in wins:
            continue
        wins[w] += 1.0
        key = (min(w, loser), max(w, loser))
        n[key] = n.get(key, 0.0) + 1.0
    neighbours: dict[int, list[tuple[int, float]]] = {i: [] for i in items}
    for (i, j), count in n.items():
        neighbours[i].append((j, count))
        neighbours[j].append((i, count))
    p = dict.fromkeys(items, 1.0)
    for _ in range(iterations):
        new = {}
        for i in items:
            numerator = wins[i] + prior  # one virtual win against the reference
            denominator = 2 * prior / (p[i] + 1.0)  # two virtual games against reference (p=1)
            for j, count in neighbours[i]:
                denominator += count / (p[i] + p[j])
            new[i] = numerator / denominator
        mean_log = sum(math.log(v) for v in new.values()) / len(new)
        scale = math.exp(-mean_log)
        new = {i: v * scale for i, v in new.items()}
        delta = max(abs(math.log(new[i]) - math.log(p[i])) for i in items)
        p = new
        if delta < tolerance:
            break
    return {i: math.log(v) for i, v in p.items()}


def spearman(rank_a: dict[int, int], rank_b: dict[int, int]) -> float | None:
    common = [i for i in rank_a if i in rank_b]
    n = len(common)
    if n < 3:
        return None
    d2 = sum((rank_a[i] - rank_b[i]) ** 2 for i in common)
    return 1 - 6 * d2 / (n * (n * n - 1))


@dataclass
class PairwiseRow:
    project: Project
    log_strength: float
    rank: int
    comparisons: int
    wins: int
    rank_normalized: int | None


@dataclass
class PairwiseResults:
    rows: list[PairwiseRow] = field(default_factory=list)
    comparisons: int = 0
    judge_comparisons: int = 0
    derived_comparisons: int = 0
    skipped: int = 0
    judges: int = 0
    rho: float | None = None


def results(db: DbSession, event: Event) -> PairwiseResults:
    projects = list(
        db.execute(
            select(Project)
            .where(Project.event_id == event.id, Project.status == ProjectStatus.submitted)
            .order_by(Project.id)
        ).scalars()
    )
    rows = (
        db.execute(select(PairwiseComparison).where(PairwiseComparison.event_id == event.id))
        .scalars()
        .all()
    )
    out = PairwiseResults(comparisons=len(rows), judges=len({r.judge_id for r in rows}))
    pairs: list[tuple[int, int]] = []
    per_project: dict[int, int] = {}
    wins: dict[int, int] = {}
    for r in rows:
        if r.source == ComparisonSource.derived:
            out.derived_comparisons += 1
        else:
            out.judge_comparisons += 1
        if r.winner_id is None:
            out.skipped += 1
            continue
        loser = r.project_b_id if r.winner_id == r.project_a_id else r.project_a_id
        pairs.append((r.winner_id, loser))
        for pid in (r.project_a_id, r.project_b_id):
            per_project[pid] = per_project.get(pid, 0) + 1
        wins[r.winner_id] = wins.get(r.winner_id, 0) + 1
    if not pairs:
        return out
    strengths = bradley_terry([p.id for p in projects], pairs)
    compared = [p for p in projects if per_project.get(p.id)]
    compared.sort(key=lambda p: (-strengths[p.id], p.title.lower()))
    normalized = scoring.compute(db, event)
    norm_rank = {r.project.id: r.rank_norm for r in normalized.projects if r.rank_norm}
    bt_rank: dict[int, int] = {}
    for i, p in enumerate(compared):
        bt_rank[p.id] = i + 1
        out.rows.append(
            PairwiseRow(
                project=p,
                log_strength=strengths[p.id],
                rank=i + 1,
                comparisons=per_project.get(p.id, 0),
                wins=wins.get(p.id, 0),
                rank_normalized=norm_rank.get(p.id),
            )
        )
    out.rho = spearman(bt_rank, norm_rank)
    return out


def derive_from_scores(db: DbSession, event: Event) -> int:
    """Seed comparisons from submitted reviews: for each judge, every pair of projects they scored
    with different totals becomes a comparison, flagged `derived`. Only when none exist yet."""
    existing = db.execute(
        select(func.count())
        .select_from(PairwiseComparison)
        .where(PairwiseComparison.event_id == event.id)
    ).scalar_one()
    if existing:
        return 0
    computed = scoring.compute(db, event)
    by_judge: dict[int, list[tuple[int, float]]] = {}
    for s in computed.reviews:
        if s.review.status == ReviewStatus.submitted:
            by_judge.setdefault(s.judge_id, []).append((s.project_id, s.raw))
    created = 0
    for judge_id, scored in by_judge.items():
        for (pa, ra), (pb, rb) in combinations(scored, 2):
            if abs(ra - rb) < 1e-9:
                continue
            db.add(
                PairwiseComparison(
                    event_id=event.id,
                    judge_id=judge_id,
                    project_a_id=pa,
                    project_b_id=pb,
                    winner_id=pa if ra > rb else pb,
                    source=ComparisonSource.derived,
                )
            )
            created += 1
    db.flush()
    return created


_ = (NotFound, Review)
