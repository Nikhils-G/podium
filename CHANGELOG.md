# Changelog

## 1.0.0 — unreleased

The first release: every tier of the DOGFOOD 2026 brief and all four bonuses.

- **Core** — accounts and sessions, per-event roles, events with dates, tracks and prizes, teams by
  invite link, draft → submit → edit until the deadline (enforced server-side), public gallery with
  search and filters.
- **Judging** — judge invitations by link, weighted rubric, manual and balanced auto-assignment,
  an autosaving review console, judging isolation enforced in the backend, a live progress
  dashboard, per-judge normalization with a documented method, CSV export at every stage.
- **Public** — community voting (accounts, single-use codes or an open link, optional quadratic
  budgets), comments, results hidden until the window closes and the organizer publishes,
  randomized ballots, rate limits, burst flagging and a hash-chained audit trail.
- **Stretch** — a REST API for every console action with a server-rendered reference, HMAC-signed
  webhooks with retries, ed25519-signed certificates and judge records with public verification,
  an embeddable gallery, and whole-event JSON import and export.
- **Bonuses** — the normalization proof on the fixture data, Bradley-Terry pairwise judging,
  `THREAT-MODEL.md`, and API-first design with a full OpenAPI document.

Hardening before the release:

- Imports only touch their own event and are audited; rate limits key on the real client; after
  the deadline only organizers withdraw or restore, and nobody once results are published; the
  audit chain stays intact under concurrent writes; teams are final after the deadline.
- "Extend" on the timeline adds time to the deadline instead of resetting it to now; results
  publish only when judging is closed and no vote is running; `PATCH /events` changes only the
  fields it names.
- WCAG AA colour in both themes (axe: no issues on 30 pages), every time shows its zone, error
  summaries on every form, 44 px touch targets on phones.
- CI: lint, migrations, tests grouped by tier, the official checker against a fresh build and
  inside a container with no network.
