# Podium

**Open-source, self-hostable hackathon submission and judging platform.** One container, one
command, no cloud dependencies. Built for organizers who need weighted rubrics, judging that
can't leak, documented score normalization, community voting that resists stuffing, and a way to
get their data back out.

```
docker compose up
```

That boots a fully seeded portal on <http://localhost:8080> — the DOGFOOD fixture event with 41
projects, 30 judges, 8 tracks and 126 reviews — and prints test logins for every role.

## What it does

| Tier | Feature | Status |
|---|---|---|
| **T1 Core** | Accounts and sessions; per-event roles (participant, judge, organizer, admin, visitor); events with dates, tracks and prizes; teams by invite link; draft → submit → edit until the deadline; deadline enforced server-side; public gallery with search and filters | ✅ |
| **T2 Judging** | Judge invitations by link; organizer-weighted rubric; manual and balanced auto-assignment with preview; judge console with autosaving reviews; **role isolation enforced in the backend**; live progress dashboard; per-judge normalization with a documented method; CSV export at every stage | ✅ |
| **T3 Public** | Community voting (signed-in, single-use codes, or open link), optional quadratic budgets; comments; results and vote counts hidden until the window closes *and* the organizer publishes; per-voter randomized ballots; rate limits, duplicate detection, burst flagging, honeypot, audit trail | ✅ |
| **T4 Stretch** | REST API covering every console action with OpenAPI docs served offline; webhooks with HMAC-signed, retried deliveries; ed25519-signed certificates and judge records with public verification; embeddable gallery; whole-event JSON import/export | ✅ |
| **Bonuses** | Normalization proof on the fixture data (Results page + `JUDGING.md`); Bradley-Terry pairwise judging mode; `THREAT-MODEL.md`; API-first design with a full OpenAPI spec | ✅ |

## Run it

**With Docker (the judged path).** From a clean clone:

```
docker compose up
```

Wait for `seeded. test logins:` in the log, then open <http://localhost:8080>. The image is built
once (that needs the network); running it needs nothing external — every font, script and style is
served from the container.

**Without Docker** (Python 3.12 and [uv](https://docs.astral.sh/uv/)):

```
uv sync
make migrate && make seed      # creates ./data/podium.db and prints the logins
make dev                       # http://127.0.0.1:8080
```

## Demo logins

Every install with `PODIUM_DEMO_ACCOUNTS=true` (the default) seeds these identities. The **Sign
in** page has one-click buttons for them, and the boot log prints the session cookies below, which
are also what `.dogfood.toml` uses.

| Role | Email | Who |
|---|---|---|
| organizer | `organizer@podium.local` | organizes *Sample Hack 2026* |
| judge_a | `diego.herrera@example.org` (`jdg_24`) | the fixture judge with the most reviews (11) |
| judge_b | `jonas.vogel@example.org` (`jdg_26`) | another fixture judge (10 reviews) |
| participant | `priya1@example.org` | a member of team `tm_01` |
| admin | `admin@podium.local` | instance admin |

Password for every demo and fixture user: **`demo-pass`**. Session tokens are derived from
`PODIUM_SECRET_KEY`, so with the default secret they are identical on every fresh install (change
the secret and disable demo accounts before running a real event — see *Operations*).

## A five-minute tour

1. **Sign in as organizer** → you land on the *run-of-show* dashboard. The **Attention** panel
   already shows what the fixture data planted: a possible duplicate submission (`prj_41` re-submits
   `prj_07` from the same team), a judge who scored every project identically (`jdg_07`), and
   projects below the review target.
2. **Results** → the ranking with raw and normalized scores side by side, the rank shift chart,
   judge calibration (spot the flat scorer), and the Bradley-Terry pairwise ranking with its
   agreement (ρ) to the scored one. `JUDGING.md` explains every number.
3. **Progress** → live per-judge and per-track completion, missing reviews.
4. **Sign in as judge_a** → the queue, then open a project: segmented 1–5 scoring with a live
   weighted total, autosave, keyboard shortcuts; the **Compare** tab for pairwise choices. Try to
   open `/api/v1/events/sample-hack-2026/judges/jdg_26/reviews` as this judge — **403**.
5. **Voting** (organizer → Voting) → choose a mode, set a window in Settings, and vote as a
   participant from the gallery; counts stay hidden until you publish.
6. **Integrations / Data / Certificates** → add a webhook and watch deliveries, download
   `export.json` and re-import it (dry run), issue judge records and verify one at `/verify`.
7. **API** → `/api/docs` (served locally), create a token on `/account`, and
   `curl -H "Authorization: Bearer pdm_…" http://localhost:8080/api/v1/events/sample-hack-2026/exports/scores.csv`.

## Acceptance checker

```
python3 tools/run.py .dogfood.toml --fixtures fixtures/fixtures.json
```

`acceptance-report.txt` in this repo is that command's output against the Docker build. Every T1
and T2 check passes. The checker has no automated checks for T3 and T4, so it prints
`claimed but not verified: T3 T4` for any submission that claims them; those tiers are demonstrated
in the UI, the API, the test suite (`tests/test_voting.py`, `tests/test_t4.py`) and the demo video.

The checker's requests are mirrored in `tests/test_acceptance.py`, so a regression on any checked
route fails `make test` before it fails the judges' run.

## Configuration

All configuration is environment variables with the `PODIUM_` prefix (or a `.env` file).

| Variable | Default | Meaning |
|---|---|---|
| `PODIUM_SECRET_KEY` | dev value | signs sessions, CSRF, voter cookies, demo tokens — **set your own** |
| `PODIUM_BASE_URL` | `http://localhost:8080` | used in links, invites, certificates |
| `PODIUM_DATA_DIR` | `./data` (`/data` in Docker) | SQLite database and the signing key |
| `PODIUM_DATABASE_URL` | unset | e.g. `postgresql+psycopg://…` to use Postgres instead of SQLite (driver included) |
| `PODIUM_OPEN_EVENT_CREATION` | `true` | `false` lets only instance admins create events |
| `PODIUM_SEED_FIXTURES` | `true` | load `fixtures/fixtures.json` at boot (idempotent) |
| `PODIUM_DEMO_ACCOUNTS` | `true` | seed the demo identities and their fixed session tokens |
| `PODIUM_DEMO_PASSWORD` | `demo-pass` | password for seeded users |
| `PODIUM_SESSION_DAYS` | `14` | session lifetime |
| `PODIUM_RATE_LIMIT_ENABLED` | `true` | per-address limits on login, register, vote, comment |
| `PODIUM_WEBHOOK_WORKER` | `true` | in-process webhook delivery loop |
| `PODIUM_COOKIE_SECURE` | derived from base URL | force the Secure cookie flag |

## Operations

- **Backup**: copy the data directory (`docker compose cp web:/data ./backup` or the named volume).
  It holds the SQLite file and the certificate signing key; that is the entire state.
- **Upgrade**: pull, `docker compose up --build`. Migrations run on boot (`alembic upgrade head`).
- **Production checklist**: set `PODIUM_SECRET_KEY`, `PODIUM_BASE_URL` (https), `PODIUM_DEMO_ACCOUNTS=false`,
  `PODIUM_SEED_FIXTURES=false`; put a reverse proxy (Caddy, nginx) in front for TLS; keep one
  container per instance (rate limits and the webhook worker are in-process).
- **Postgres**: set `PODIUM_DATABASE_URL`; the `psycopg` driver is installed, the schema uses only
  portable types and the same migrations apply. Export from one instance and import into another to move an event.
- **Email**: Podium never sends email. Judge invitations and voting codes are links and codes the
  organizer distributes; no SMTP configuration is required.
- **Health**: `GET /healthz` → `{"status":"ok"}`; the Docker image has a healthcheck.

## Documentation

- [`ARCHITECTURE.md`](ARCHITECTURE.md) — layers, decisions and their reasons, trade-offs.
- [`DATA-MODEL.md`](DATA-MODEL.md) — schema, invariants, import/export paths.
- [`JUDGING.md`](JUDGING.md) — assignment strategy, scoring math, normalization defended with the fixture numbers, pairwise mode, isolation matrix.
- [`THREAT-MODEL.md`](THREAT-MODEL.md) — what is defended, how, and what isn't.
- `/api/docs` on a running instance — interactive OpenAPI reference (`/api/openapi.json`).

## Development

```
make test      # pytest — 90+ tests on a temp database seeded from the real fixtures
make lint      # ruff
make check     # run the organizer's checker against a running portal → acceptance-report.txt
make clean-verify   # what a judge does: rebuild without cache, boot, run the checker
```

## Honest limitations

- Rate limiting and the webhook worker are in-process; run one container per instance (or move
  both to a shared store before scaling out).
- Open-link voting is deliberately weak (one vote per browser cookie); use codes or accounts when
  it matters. Bursts from one address are flagged for review, not blocked.
- No file uploads: projects link to their repositories, demos and videos.
- English UI; all times are stored and shown in UTC with the viewer's local time alongside.
- The Bradley-Terry ranking on the demo event is computed from comparisons *derived* from the
  fixture scores (clearly labelled) so the feature has data to show; real events use judges' own
  comparisons.

## License

MIT — see [`LICENSE`](LICENSE). Fonts are under the SIL Open Font License (see `src/podium/static/fonts/`);
htmx and Swagger UI are vendored under their own licenses.
