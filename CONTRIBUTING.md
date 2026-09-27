# Contributing

## Set up

```
uv sync
make migrate && make seed   # a local SQLite database with the fixture event and demo logins
make dev                    # http://127.0.0.1:8080
```

## Before you open a pull request

```
make test    # pytest
make lint    # ruff check + ruff format --check
```

CI runs the same checks, rebuilds the Docker image, runs the organizer's acceptance checker
against it and inside a container with no network, and fails if the output differs from the
committed `acceptance-report.txt`. Never edit that file by hand.

## House rules

- Authorization and business rules live in `src/podium/services/`; routers in `web/` and `api/`
  stay thin and never query models for a decision.
- Every schema change is an Alembic migration (`uv run alembic revision --autogenerate -m "..."`);
  CI fails when the models and the migrations disagree.
- Store raw per-criterion scores; compute totals and normalized scores on read.
- Times are UTC in the database and at the API; the UI shows local time with its zone.
- No CDN or external asset of any kind: the portal must work with the network off.
- Every feature comes with tests for its invariants (who may do what, deadlines, the scoring math),
  and every page handles the empty, error and partial states, not only the happy path.

Commit messages are one short subject line in plain English, plus a few lines of body when the
reason isn't obvious.
