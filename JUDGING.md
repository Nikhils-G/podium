# JUDGING.md — assignment, scoring math, normalization, pairwise mode, isolation

This document defends every number Podium produces. Everything below is computed on read from
the raw per-criterion scores; nothing derived is ever stored, so changing a weight or the method
re-derives every figure consistently. The worked example uses the DOGFOOD fixture data that ships
with the repo (`fixtures/fixtures.json`), so every table here can be reproduced by booting the
portal and opening **Organizer → Results**.

## 1. Assignment

Two strategies, both audited:

- **Manual.** The organizer picks a judge and any number of projects. Refused when the judge is a
  member of the project's team (structurally impossible anyway — one role per person per event —
  but checked).
- **Balanced track round-robin (auto).** Inputs: the target reviews per project *R* (event
  setting, default 3), each judge's track preferences, existing assignments, and a seed. The
  planner walks projects with the fewest existing reviews first; for each it takes *R − existing*
  judges from the project's track (judges with no track preference count as eligible everywhere),
  falling back to all judges only when the track has none, and always picks the judge with the
  lowest current load (ties broken by the seeded shuffle). It never assigns a judge to their own
  team. The plan is **previewed before it is applied** and lists projects that still can't reach
  *R* ("shortfalls") and assignments that had to go outside a judge's tracks. Same seed → same
  plan, so organizers can reproduce and discuss it.

## 2. Rubric and raw score

Criteria are organizer-defined with a weight *w_c*, a minimum and a maximum. A review stores one
integer per criterion. Its raw score on a 0–100 scale is the weighted mean of the criteria the
judge scored, each mapped onto its own scale:

    raw = 100 · Σ_c w_c · (v_c − min_c) / (max_c − min_c)  /  Σ_c w_c

Consequences: weights can change at any time and every review re-weights consistently; a
criterion added later is simply absent from older reviews' sums; criteria with scores are
archived, never deleted, so history stays reproducible. The rubric's *structure* locks when
judging opens so every review is scored on the same instrument.

## 3. Normalization

Judges differ: some are harsh, some generous, some barely use the scale. Comparing raw means
across projects reviewed by different judges therefore mixes project quality with judge habit.
Podium standardises each judge against their own distribution, with **shrinkage** toward the
event so that judges with very few reviews aren't over-corrected.

For judge *j* with *n_j* submitted reviews, mean *μ_j* and population spread *σ_j*; event mean
*μ_g* and spread *σ_g* over all submitted reviews; *k* = 2:

    μ̂_j = (n_j·μ_j + k·μ_g) / (n_j + k)
    σ̂_j = (n_j·σ_j + k·σ_g) / (n_j + k)
    z    = (raw − μ̂_j) / σ̂_j

**Rules for the awkward cases** (all present in the fixtures):

- **Flat scorer** (*n_j* ≥ 2 and *σ_j* = 0): every review gets *z* = 0. A judge who gives every
  project the same total carries no information about which project is better; without this rule
  a tiny difference in shrunk mean would inject a constant bias into every project they touched.
- **Single review** (*n_j* = 1): *μ̂_j* and *σ̂_j* are dominated by the event's values, so the lone
  score is interpreted mostly against the event rather than against a mean of one.
- **Incomplete batches**: a project's score is the mean *z* of whatever reviews it has; there is
  no imputation, and the review count and a **disagreement** figure (spread of its *z* values) are
  shown next to every score so thin or contested results are visible.
- **Fewer than two judges**: normalization is disabled and raw means are used, with a notice.

The project score shown to organizers and published is `μ_g + σ_g · mean(z)` clipped to 0–100,
so it reads on the same scale as raw scores. Ranking is by mean *z*; ties share a rank
("T-2nd"). Organizers choose whether the published ranking uses the normalized or the raw basis;
both are always shown side by side with the rank change.

### Worked example on the fixture data

Submitted reviews: 126 from 30 judges over 41 projects. Event mean μ_g = 64.15, spread σ_g = 16.21, shrinkage k = 2.

| Judge | n | mean μ_j | spread σ_j | shrunk μ̂_j | shrunk σ̂_j | Flag |
|---|---:|---:|---:|---:|---:|---|
| jdg_07 | 3 | 75.0 | 0.0 | 70.7 | 6.5 | flat scorer → z = 0 |
| jdg_23 | 1 | 58.3 | 0.0 | 62.2 | 10.8 | single review → shrunk to event |
| jdg_01 | 1 | 25.0 | 0.0 | 51.1 | 10.8 | single review → shrunk to event |
| jdg_24 | 11 | 59.1 | 14.8 | 59.9 | 15.1 |  |
| jdg_26 | 10 | 67.5 | 11.5 | 66.9 | 12.2 |  |
| jdg_29 | 9 | 63.0 | 9.7 | 63.2 | 10.9 |  |
| jdg_11 | 6 | 65.3 | 17.6 | 65.0 | 17.3 |  |
| jdg_14 | 3 | 50.0 | 13.6 | 55.7 | 14.6 |  |
| jdg_27 | 2 | 50.0 | 8.3 | 57.1 | 12.3 |  |
| jdg_02 | 6 | 80.6 | 18.4 | 76.5 | 17.9 |  |
| jdg_30 | 4 | 77.1 | 12.3 | 72.8 | 13.6 |  |

`jdg_07` gave three projects an identical total and is neutralised; the two single-review judges
are pulled toward the event distribution rather than standardised against themselves.

The biggest rank movements when switching from raw means to normalized scores:

| Project | Reviews | Raw mean | Raw rank | Normalized | Normalized rank | Δ |
|---|---:|---:|---:|---:|---:|---:|
| prj_19 Small Relay | 2 | 66.7 | 13 | 60.5 | 28 | -15 |
| prj_28 Flat Meadow | 3 | 61.1 | 24 | 53.4 | 36 | -12 |
| prj_27 Flat Thread | 3 | 61.1 | 24 | 64.0 | 16 | +8 |
| prj_17 Small Loom | 3 | 63.9 | 16 | 62.3 | 24 | -8 |
| prj_02 Small Meadow | 3 | 63.9 | 16 | 63.0 | 22 | -6 |
| prj_12 Open Beacon | 3 | 61.1 | 24 | 63.6 | 19 | +5 |
| prj_32 Loud Ledger | 3 | 61.1 | 24 | 60.5 | 29 | -5 |
| prj_09 Hollow Signal | 3 | 63.9 | 16 | 63.2 | 20 | -4 |

Top of the normalized ranking (`*` marks a tie):

| Rank | Project | Reviews | Raw mean | Normalized | Disagreement |
|---:|---|---:|---:|---:|---:|
| 1 | prj_34 Iron Switch | 3 | 83.3 | 83.8 | 0.18 |
| 2 | prj_11 Salt Ledger | 4 | 83.3 | 79.4 | 0.13 |
| 3 | prj_33 Slow Trail | 3 | 75.0 | 78.2 | 0.15 |
| 4 | prj_37 Salt Loom | 4 | 77.1 | 76.9 | 1.27 |
| 5 | prj_25 Dry Relay | 3 | 77.8 | 75.3 | 0.61 |
| 6 | prj_10 Still Beacon | 2 | 79.2 | 74.9 | 0.34 |
| 7 | prj_16 Salt Kiln | 3 | 75.0 | 73.7 | 0.55 |
| 8 | prj_41 Dry Harbour | 4 | 70.8 | 70.0 | 0.73 |
| 9 | prj_21 Copper Kiln | 3 | 72.2 | 68.5 | 0.89 |
| 10 | prj_04 Green Switch | 3 | 69.4 | 67.7 | 0.61 |

All figures above regenerate on the Results page and in `scores.csv`.

### Limitations, stated plainly

- Shrinkage removes most, not all, of a systematic judge offset (*k* = 2 keeps roughly
  *k/(n_j + k)* of it). A judge who is harsh *because their projects were worse* is partially
  respected — that is the intended trade-off, and the raw ranking is always one click away.
- Standardisation assumes a judge's scores are roughly on one scale across tracks; a judge who
  is harsh in one track and generous in another is treated as average.
- With very few reviews per project the disagreement figure is noisy; treat it as a flag, not a
  measurement.

### Ranking confidence (bootstrap)

A ranking is only as settled as the reviews behind it. Podium re-draws every project's own
reviews with replacement 300 times (judge calibration held fixed), re-ranks the field each time
with the same tie rule, and counts where each project lands. The organizer's Results page shows,
per project, the share of re-draws in which it came first and in the top three plus its expected
rank; the public podium says "#1 in N% of re-draws". A project with a single review cannot be
re-drawn and shows no estimate, but still acts as a fixed competitor for the others.

On the fixture data (seed = the event id, so the numbers are reproducible):

| Published rank | Project | Reviews | First in | Top 3 in | Expected rank |
|---:|---|---:|---:|---:|---:|
| 1 | prj_34 Iron Switch | 3 | 47% | 98% | 1.7 |
| 2 | prj_11 Salt Ledger | 4 | 0% | 35% | 4.2 |
| 3 | prj_33 Slow Trail | 3 | 0% | 13% | 5.4 |
| 4 | prj_37 Salt Loom | 4 | 30% | 33% | 8.6 |
| 5 | prj_25 Dry Relay | 3 | 7% | 21% | 7.5 |

Read honestly: the winner is a clear top-three project but holds first place in fewer than half
the re-draws, and the fourth-placed Salt Loom takes first in almost a third of them. Two or three
reviews per project is not enough to separate neighbours — which is the point of showing it.

## 4. Pairwise mode (Bradley-Terry)

Judges can also compare two of their assigned projects at a time (Judge console → Compare; the
least-compared pair is offered next, until every pair has been seen). Preferences are fitted with
the Bradley-Terry model, where the probability that *i* beats *j* is *p_i / (p_i + p_j)*, using
Hunter's minorization–maximization iteration

    p_i ← W_i / Σ_j n_ij / (p_i + p_j)

with one virtual win and one virtual loss for every project against a fixed reference of
strength 1, so the comparison graph is always connected and every strength is finite even for
projects compared once. Strengths are normalised to geometric mean 1 and reported as
log-strength. The Results page shows the Bradley-Terry ranking beside the normalized one with
Spearman's ρ between them, computed on the projects both rankings know and re-ranked densely
within that shared set (ranks from sets of different sizes are not comparable; an earlier build
printed ρ = −251 for exactly that reason). ρ is reported twice: over every comparison, and over
the comparisons judges actually made in the compare tab. Only the second one measures agreement.
Derived comparisons are built from the scores themselves, so their ρ is circular by construction
and the page says so.

On the fixture data Podium derives comparisons from the scored reviews (every pair of projects a
judge scored differently becomes a comparison, flagged *derived* and never mixed up with real
judge choices): 254 comparisons (254 derived from the fixture reviews, 0 made in the compare tab), Spearman ρ against the normalized ranking = 0.85.

| BT rank | Project | log strength | Comparisons | Wins | Normalized rank |
|---:|---|---:|---:|---:|---:|
| 1 | prj_34 Iron Switch | +2.53 | 16 | 16 | 1 |
| 2 | prj_33 Slow Trail | +1.79 | 19 | 17 | 3 |
| 3 | prj_37 Salt Loom | +1.54 | 22 | 19 | 4 |
| 4 | prj_07 Dry Harbour | +1.19 | 15 | 11 | 30 |
| 5 | prj_11 Salt Ledger | +1.15 | 17 | 13 | 2 |
| 6 | prj_09 Hollow Signal | +1.02 | 7 | 6 | 20 |
| 7 | prj_25 Dry Relay | +0.97 | 11 | 8 | 5 |
| 8 | prj_02 Small Meadow | +0.88 | 10 | 7 | 22 |

## 5. Role isolation

Authorization lives in the service layer, never in templates. The matrix below is executed as a
test (`tests/test_authz_matrix.py`) on every run.

| Actor | Own reviews | Another judge's reviews | Aggregates / results before publish | Vote counts before publish | Exports | Audit log |
|---|---|---|---|---|---|---|
| Visitor | 401 | 401 | 404 | 404 | 401 | 401 |
| Participant | 403 | 403 | 404 | 404 | 403 | 403 |
| Judge | 200 | **403** | 404 | 404 | 403 | 403 |
| Organizer / admin | 200 | 200 | 200 | 200 | 200 | 200 |

A judge can score only projects assigned to them (403 otherwise), cannot be on a team in the
same event, and sees no aggregate anywhere in the judge console.

## 6. Audit trail

Every sensitive action — role grants, rubric edits with before/after values, assignments,
review submissions, votes (accepted, rejected and voided), event lifecycle changes (publish, open
and close judging, publish and unpublish results, archive), imports (who applied the file, the row
counts and its SHA-256), certificate issuance — is an append-only row whose hash commits to the
previous row's hash. Exports are read-only GETs and are deliberately not audited. Appends are
serialised: each row is inserted first, which takes the database write lock (an advisory lock on
PostgreSQL), and only then linked to the previous row, so two concurrent writers can never link to
the same head. **Organizer → Audit log → Verify chain** recomputes the whole chain; an edited or
deleted row breaks verification from that point on. The log is filterable in the UI and
exportable as CSV.
