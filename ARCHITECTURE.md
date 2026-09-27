# Architecture

Podium is a modular monolith: one FastAPI process, one SQLite file, one container. This document
explains the shape, the decisions behind it, and what we would steal from it for the next project.

## The shape

```
browser ──HTML + htmx──▶  web/        ─┐
curl / SDK / checker ─JSON─▶ api/v1/  ─┼─▶ services/  (every business rule, every authorization check)
                                       │        │
                                       │        ▼
                                       │   models/  (SQLAlchemy 2, all timestamps UTC)  ──▶ SQLite (WAL) in /data
                                       │
                                       └─▶ tasks/webhook_worker  (in-process loop, persisted deliveries)
```

**Routers are thin.** A web route parses a form, calls a service, renders a template. An API route
parses JSON, calls the *same* service, returns JSON. Neither contains a rule, and neither queries
the database directly for anything that carries authorization weight. That is what makes the
"backend-enforced isolation" claim true rather than aspirational: there is one function that
decides who may read a judge's reviews (`services/authz.py`), and both the HTML console and the
JSON API go through it. The role × endpoint matrix in `tests/test_authz_matrix.py` exercises it.

**Everything derived is computed on read.** Reviews store one integer per criterion. Weighted
totals, normalized scores, rankings, tallies and the Bradley-Terry strengths are all computed
from those facts when a page or export asks for them. Changing a weight re-weights every review
consistently; nothing stored goes stale; and the "proof" tables in `JUDGING.md` are just the
Results page rendered as Markdown.

**The seeder is the importer.** `docker compose up` loads `fixtures/fixtures.json` through the
same code path as *Organizer → Data → Import*. Rows are matched by their public ids, so
re-running is idempotent and an export re-imports losslessly. Fixture ids (`prj_07`, `jdg_24`)
survive as public ids, which is why `.dogfood.toml` can name `jdg_24` and the URL stays stable.

## Event lifecycle

An event's stage is never stored; `services/events.stage_of()` derives it from set-once
timestamps and flags, in this priority: **archived** → **published** (results) → **voting**
(window open now) → **judging** (opened, not closed) → **judging closed** (opened and closed,
results not yet published) → **draft** (not public) → **upcoming** → **submissions closed** →
**open**. The organizer dashboard turns that into a run-of-show timeline whose actions are
exactly the ones the service layer would accept at that moment, so the UI can never suggest
undoing the last step. Voting is independent of judging: it can run before, during or after,
and "Close judging" stays reachable while voting is open.

## Request lifecycle

1. `SecurityHeadersMiddleware` mints a per-request CSP nonce and sets the headers
   (`default-src 'self'`, script nonce, no inline styles, frame-ancestors none except `/embed`).
2. Dependencies resolve in declaration order: `current_user` (session cookie or bearer token) →
   `load_event` (404 for drafts unless organizer) → `require_event_role` / `require_organizer` →
   window checks such as `require_submissions_open`. **All of these run before the request body is
   parsed**, so a closed event answers 403 to any payload, never 422 or 500.
3. The route calls a service. Services raise typed errors (`NotFound`, `Forbidden`, `Closed`,
   `Conflict`, `ValidationFailed`, `RateLimited`) which `main.py` maps once: JSON for `/api/` and
   htmx requests, an error page (or a login redirect) for pages.
4. Services write audit rows (hash-chained) and queue webhook deliveries inside the same
   transaction as the change, then commit.

## Decisions and why

| Decision | Alternative considered | Why this one |
|---|---|---|
| SQLite in WAL mode as the default | Postgres in compose from day one | One-command boot that can't race a DB container; backup is one file; the schema is portable and `PODIUM_DATABASE_URL` switches to Postgres without code changes |
| Server-rendered Jinja2 + htmx | React SPA | Must run with the network off: zero CDN assets, one build stage. htmx gives live search, autosave and polling without a bundler. The API exists for anyone who wants a different front end |
| Roles per event (`event_roles`), admin global | a role column on `users` | A person judges one event and competes in another; conflicts of interest become structurally impossible (one role per person per event) |
| Deterministic demo session tokens (HMAC of secret + role) | random per boot | The committed `.dogfood.toml` must work on a judge's fresh clone; demo sessions are also immune to "Sign out" |
| Sync SQLAlchemy in a threadpool | async SQLAlchemy | Simpler, correct with SQLite, no async driver foot-guns; latency at hackathon scale is not the constraint |
| Hash-chained audit log | plain log table | Ten lines of code turn "we have an audit trail" into "the audit trail is tamper-evident" |
| Per-judge z-score with shrinkage, flat scorers neutralised | raw means; plain z-score | Documented in `JUDGING.md`; robust to the fixture's awkward cases (one review, identical scores, incomplete batches) |
| Webhook deliveries as rows + in-process worker | fire-and-forget HTTP, Celery | Retries survive restarts, are visible and re-deliverable in the UI, and need no broker |
| Signed certificates with a local ed25519 key | PDFs, external issuers | Verifiable by anyone with the published public key; print-ready HTML; no dependency |
| Strict CSP with nonces, no inline styles | relaxed CSP | Forces the offline rule (nothing external can slip in) and blocks XSS classes by construction. Only `/api/docs/console` relaxes styles for Swagger UI |
| API reference rendered on the server from the OpenAPI document | Swagger UI as the main docs page | Reads without JavaScript, matches the product's design system, and cannot drift: permissions come from each route's dependency tree, examples from the schemas, code is highlighted in Python |
| No scroll regions inside a page | sticky side columns with their own scrollbars | Nested scrollbars trap keyboard and magnifier users. A side column sticks only while it fits under the header (a 20-line measure in `app.js`); a taller one scrolls with the page. Only wide tables on phones and the phone menu may scroll sideways or inside themselves |

## Decisions worth stealing

- **One authorization choke point** (`reviews_visible_to`, `assert_can_view_judge_reviews`) shared by
  HTML and JSON.
- **Window checks as dependencies** that run before body parsing.
- **Seeder = importer** with public ids that survive round trips.
- **Computed-on-read scoring** so rubric weights are safe to change.
- **Deterministic demo tokens** so a committed config works on a stranger's machine.
- **Preview → apply** for anything algorithmic that changes many rows (auto-assignment, import).
- **Per-voter deterministic ballot shuffle**: stable for one voter, different across voters.
- **A page sweep that proves its own coverage**: every HTML route × every role, plain and htmx,
  with a guard test that fails if the sweep ever finds too few routes (it once found none).

## Directory layout

```
src/podium/
  main.py            app factory, middleware, exception mapping, lifespan (webhook worker)
  config.py          Settings (PODIUM_* env)
  db.py errors.py
  models/            one module per aggregate; UTCDateTime type; public_id columns
  security/          passwords (argon2id), sessions (hashed tokens), csrf, ratelimit, headers, deps
  services/          authz, auth, events, teams, projects, judges, rubric, assignments, reviews,
                     scoring, pairwise, voting, comments, exports, importexport, webhooks,
                     certificates, tokens, audit, dashboard
  web/               HTML routers (public, auth, participant, organizer*, judge, community,
                     account, certificates) — thin
  api/v1/            JSON routers — thin, documented, tagged
  seed/              fixtures importer, demo accounts, boot printer
  tasks/             webhook worker
  templates/         base + layouts + macros (ui, viz) + partials + pages
  static/            css/app.css (tokens, light/dark), js/app.js, vendored htmx & swagger-ui, fonts
migrations/          Alembic
tests/               pytest suite on a temp DB seeded from the real fixtures
tools/run.py         the organizer's checker, unchanged; fixtures/fixtures.json, unchanged
```

## Trade-offs we accepted

- In-process rate limiting and worker: one container per instance. Scaling out means moving both
  to the database or Redis (small change; the interfaces are single functions).
- No email: invitations and voter codes are distributed by the organizer. Fewer moving parts and
  no phishing surface, at the cost of one manual step.
- No file uploads: projects link out. Avoids storage, virus scanning and size policy.
- SQLite's single writer: fine for an event with hundreds of users; Postgres is the path beyond.

## What we would do next

Judge conflict-of-interest declarations per project, organizer-defined review templates (guided
rubric descriptions), per-track rubrics, notifications via webhooks to chat tools, and a Postgres
compose profile for larger instances.
