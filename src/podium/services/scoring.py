"""Scoring math. Raw per-criterion values are the only stored facts; everything here is derived.

raw score    r = Σ_c w_c · (v_c − min_c)/(max_c − min_c) / Σ_c w_c × 100          (0–100)
per judge j  n_j, μ_j, σ_j (population std) over submitted reviews; global μ_g, σ_g
shrinkage    μ̂_j = (n_j μ_j + k μ_g)/(n_j + k),  σ̂_j = (n_j σ_j + k σ_g)/(n_j + k),  k = 2
z-score      z = (r − μ̂_j)/σ̂_j, except a flat scorer (n_j ≥ 2, σ_j = 0) contributes z = 0
project      z̄_p = mean z over its reviews, shown as μ_g + σ_g·z̄_p clipped to 0–100;
             disagreement d_p = population std of its z values.
"""

from dataclasses import dataclass, field
from statistics import fmean, pstdev

from sqlalchemy import select
from sqlalchemy.orm import Session as DbSession

from podium.models import (
    Event,
    NormalizationMethod,
    Project,
    ProjectStatus,
    RankingBasis,
    Review,
    ReviewStatus,
    RubricCriterion,
    ScoreItem,
    Track,
    User,
)

SHRINKAGE_K = 2.0
EPSILON = 1e-9


def raw_score(values: dict[int, int], criteria: list[RubricCriterion]) -> float | None:
    """Weighted 0–100 score over the criteria that were scored. None when nothing was scored."""
    total_weight = 0.0
    total = 0.0
    for crit in criteria:
        if crit.id not in values:
            continue
        span = crit.max_score - crit.min_score
        if span <= 0:
            continue
        total += crit.weight * (values[crit.id] - crit.min_score) / span
        total_weight += crit.weight
    if total_weight <= 0:
        return None
    return 100.0 * total / total_weight


@dataclass
class JudgeStats:
    judge: User
    n: int
    mean: float
    std: float
    shrunk_mean: float
    shrunk_std: float
    flat: bool  # scores every project identically → contributes nothing to ranking

    @property
    def few(self) -> bool:
        return self.n < 3


@dataclass
class ReviewScore:
    review: Review
    judge_id: int
    project_id: int
    raw: float
    z: float = 0.0


@dataclass
class ProjectResult:
    project: Project
    track: Track | None
    n: int
    raw_mean: float | None
    z_mean: float | None
    normalized: float | None  # μ_g + σ_g·z̄, clipped 0–100
    disagreement: float | None
    rank_raw: int | None = None
    rank_norm: int | None = None
    tied_raw: bool = False
    tied_norm: bool = False

    @property
    def rank_delta(self) -> int | None:
        if self.rank_raw is None or self.rank_norm is None:
            return None
        return self.rank_raw - self.rank_norm


@dataclass
class Results:
    event: Event
    criteria: list[RubricCriterion]
    reviews: list[ReviewScore]
    judges: list[JudgeStats]
    projects: list[ProjectResult]
    global_mean: float | None
    global_std: float | None
    k: float = SHRINKAGE_K
    method: NormalizationMethod = NormalizationMethod.zscore
    basis: RankingBasis = RankingBasis.normalized
    unscored: list[Project] = field(default_factory=list)


def normalize(
    scores: list[ReviewScore], judges: dict[int, User], *, k: float = SHRINKAGE_K
) -> tuple[list[JudgeStats], float | None, float | None]:
    """Fill z on each ReviewScore in place; return per-judge stats and the global μ, σ."""
    if not scores:
        return [], None, None
    raws = [s.raw for s in scores]
    mu_g, sigma_g = fmean(raws), pstdev(raws) if len(raws) > 1 else 0.0
    by_judge: dict[int, list[ReviewScore]] = {}
    for s in scores:
        by_judge.setdefault(s.judge_id, []).append(s)
    stats: list[JudgeStats] = []
    for judge_id, rows in by_judge.items():
        n = len(rows)
        mean = fmean(r.raw for r in rows)
        std = pstdev([r.raw for r in rows]) if n > 1 else 0.0
        flat = n >= 2 and std < EPSILON
        shrunk_mean = (n * mean + k * mu_g) / (n + k)
        shrunk_std = (n * std + k * sigma_g) / (n + k)
        for r in rows:
            if flat or shrunk_std < EPSILON:
                r.z = 0.0
            else:
                r.z = (r.raw - shrunk_mean) / shrunk_std
        stats.append(
            JudgeStats(
                judge=judges[judge_id],
                n=n,
                mean=mean,
                std=std,
                shrunk_mean=shrunk_mean,
                shrunk_std=shrunk_std,
                flat=flat,
            )
        )
    stats.sort(key=lambda s: (-s.n, s.judge.name))
    return stats, mu_g, sigma_g


def _rank(items: list[ProjectResult], key, attr_rank: str, attr_tie: str) -> None:
    ordered = [p for p in items if key(p) is not None]
    ordered.sort(key=lambda p: -key(p))
    rank = 0
    for i, p in enumerate(ordered):
        if i == 0 or abs(key(p) - key(ordered[i - 1])) > 1e-9:
            rank = i + 1
        setattr(p, attr_rank, rank)
    counts: dict[int, int] = {}
    for p in ordered:
        counts[getattr(p, attr_rank)] = counts.get(getattr(p, attr_rank), 0) + 1
    for p in ordered:
        setattr(p, attr_tie, counts[getattr(p, attr_rank)] > 1)


def compute(db: DbSession, event: Event, *, include_withdrawn: bool = False) -> Results:
    criteria = list(
        db.execute(
            select(RubricCriterion)
            .where(RubricCriterion.event_id == event.id)
            .order_by(RubricCriterion.position)
        ).scalars()
    )
    rows = db.execute(
        select(Review, User)
        .join(User, User.id == Review.judge_id)
        .where(Review.event_id == event.id, Review.status == ReviewStatus.submitted)
    ).all()
    items = (
        db.execute(
            select(ScoreItem)
            .join(Review, Review.id == ScoreItem.review_id)
            .where(Review.event_id == event.id, Review.status == ReviewStatus.submitted)
        )
        .scalars()
        .all()
    )
    values: dict[int, dict[int, int]] = {}
    for item in items:
        values.setdefault(item.review_id, {})[item.criterion_id] = item.value
    judges: dict[int, User] = {}
    scores: list[ReviewScore] = []
    for review, judge in rows:
        judges[judge.id] = judge
        raw = raw_score(values.get(review.id, {}), criteria)
        if raw is None:
            continue
        scores.append(
            ReviewScore(review=review, judge_id=judge.id, project_id=review.project_id, raw=raw)
        )
    stats, mu_g, sigma_g = normalize(scores, judges)
    method = event.normalization_method
    if method == NormalizationMethod.none:
        for s in scores:
            s.z = 0.0

    query = (
        select(Project, Track)
        .outerjoin(Track, Track.id == Project.track_id)
        .where(Project.event_id == event.id)
    )
    query = (
        query.where(Project.status != ProjectStatus.draft)
        if include_withdrawn
        else query.where(Project.status == ProjectStatus.submitted)
    )
    by_project: dict[int, list[ReviewScore]] = {}
    for s in scores:
        by_project.setdefault(s.project_id, []).append(s)
    results: list[ProjectResult] = []
    unscored: list[Project] = []
    for project, track in db.execute(query.order_by(Project.id)).all():
        rs = by_project.get(project.id, [])
        if not rs:
            unscored.append(project)
            results.append(
                ProjectResult(
                    project=project,
                    track=track,
                    n=0,
                    raw_mean=None,
                    z_mean=None,
                    normalized=None,
                    disagreement=None,
                )
            )
            continue
        raw_mean = fmean(r.raw for r in rs)
        z_mean = fmean(r.z for r in rs)
        normalized = (
            max(0.0, min(100.0, mu_g + (sigma_g or 0.0) * z_mean))
            if method == NormalizationMethod.zscore and mu_g is not None
            else raw_mean
        )
        disagreement = pstdev([r.z for r in rs]) if len(rs) > 1 else None  # one review: no spread
        results.append(
            ProjectResult(
                project=project,
                track=track,
                n=len(rs),
                raw_mean=raw_mean,
                z_mean=z_mean,
                normalized=normalized,
                disagreement=disagreement,
            )
        )
    _rank(results, lambda p: p.raw_mean, "rank_raw", "tied_raw")
    _rank(
        results,
        lambda p: p.z_mean if method == NormalizationMethod.zscore else p.raw_mean,
        "rank_norm",
        "tied_norm",
    )
    basis = event.published_ranking
    results.sort(
        key=lambda p: (
            (p.rank_norm if basis == RankingBasis.normalized else p.rank_raw) or 10**9,
            -(p.n or 0),
            p.project.title.lower(),
        )
    )
    return Results(
        event=event,
        criteria=criteria,
        reviews=scores,
        judges=stats,
        projects=results,
        global_mean=mu_g,
        global_std=sigma_g,
        method=method,
        basis=basis,
        unscored=unscored,
    )
