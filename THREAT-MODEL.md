# Threat model

What Podium protects, who it protects it from, and what it deliberately does not promise. Written
for organizers deciding how to configure an event and for reviewers checking the claims against
the code.

## Assets

1. **Judging integrity** — reviews and scores must come only from assigned judges, be visible only
   to the right people, and be aggregated by a method nobody can quietly tilt.
2. **Vote integrity** — community votes should reflect people, not scripts, and counts must stay
   hidden until the organizers publish.
3. **Accounts and sessions** — a participant's submission, a judge's reviews and an organizer's
   controls must not be usable by someone else.
4. **The audit trail** — the record of who did what must be complete and tamper-evident.
5. **Availability of the portal during the event window** — deadlines are enforced by the server
   clock; the server must stay up and honest about time.

## Actors

- *Participant* wanting a better result for their team.
- *Judge* wanting to see peers' scores, or to score projects they were not assigned.
- *Voter or bot* wanting to stuff the ballot for a project.
- *Team* trying to game the deadline (edit after close, argue about time zones).
- *Organizer* making a mistake (rather than acting maliciously) — the system should make mistakes
  visible and reversible, not silently absorbed.
- *Outsider* probing the HTTP surface with curl.

## Threats and mitigations

| Threat | Mitigation | Where |
|---|---|---|
| Judge reads another judge's scores by typing a URL | Every review read goes through `services.authz`; `/judges/{id}/reviews` returns 403 unless caller is that judge or an organizer. Enforced in the service, not the template. Covered by the role×endpoint test matrix. | `services/authz.py`, `api/v1/judging.py`, `tests/test_authz_matrix.py` |
| Judge scores a project they were not assigned | `reviews.assignment_for` raises 403 for any project outside the judge's assignments. | `services/reviews.py` |
| Judge on a competing team | One role per person per event (`event_roles` unique on event+user). Accepting a judge invite is refused for anyone already a participant; joining a team is refused for judges. The auto-assigner additionally checks team membership. | `services/judges.py`, `services/teams.py`, `services/assignments.py` |
| Organizer tilts results by re-weighting after seeing scores | Weights can change (totals are computed on read), but every change is an audit entry with old and new values, and the rubric's structure locks when judging opens. | `services/rubric.py`, audit log |
| A judge scores every project identically, or is far harsher than others | Normalization standardises each judge against their own distribution with shrinkage; flat scorers contribute zero ranking information. Both are flagged on the dashboard. Method documented in JUDGING.md. | `services/scoring.py` |
| Ballot stuffing from one person | *Account mode*: one vote per project per account. *Code mode*: single-use codes, hashed at rest. *Link mode*: one vote per signed browser cookie — explicitly the weakest, and the UI says so; bursts from one network address are flagged for organizer review. Votes are unique per (event, project, voter) at the database level. | `services/voting.py`, `models/voting.py` |
| Sybil accounts for voting | Not solved by software alone. Organizers who need strong guarantees should use code mode with codes issued to known people. Podium exposes the trade-off in the settings copy rather than hiding it. | `organizer/voting.html` |
| Bots posting votes | Honeypot field (silently rejected and logged), per-address rate limits on vote, voting-code redeem (10/min, one bucket shared by the page and `POST /api/v1/events/{slug}/voting/codes/redeem`), comment, login and register, CSRF on every HTML form. | `services/voting.py`, `security/ratelimit.py`, `security/csrf.py` |
| Spoofed client address | Rate limits, vote-burst flags and audit IP hashes use the socket peer. A client-sent `X-Forwarded-For` is ignored; uvicorn `--proxy-headers` honours forwarded headers only from the proxies listed in `FORWARDED_ALLOW_IPS` (default `127.0.0.1,::1`). | `security/ratelimit.client_ip`, `entrypoint.sh`, `docker-compose.yml` |
| Position bias in ballots | The whole submitted set is shuffled deterministically per voter *before* paging, so page 1 favours nobody. Signed-in voters are keyed by account or code; visitors receive a signed, random, first-party `voter` cookie during an open window (no personal data, 90 days) that fixes their order and, in link mode, becomes their one-ballot identity. Sorting is disabled while a ballot is open. | `services/projects.gallery`, `services/voting.ballot_order`, `web/community.set_voter_cookie` |
| Early leak of vote counts or rankings | Tallies are exposed only when the window has closed **and** results are published; both checks are in the service and apply to the API as well as pages. Organizers see everything, always. | `services/voting.tallies_visible`, `results_visible` |
| Deadline gaming — submitting, editing, withdrawing or restoring after the close via the API, clock/timezone confusion, or begging for "just one more edit" | Every window check compares against the server's UTC clock, for HTML and JSON alike (the API is not a back door). Creating a project is checked by a route dependency *before* the body is read; edits, withdrawals and restores are checked in the service, so any payload that passes validation after the close gets 403 (unparseable JSON gets 422 before any check runs). Deadlines are shown in UTC and local time so nobody is surprised. The timeline's Extend buttons count from the current deadline and can never move it earlier. Organizers can unlock one project for a bounded time, and every unlock is audited with who, which project and for how long. What we don't stop: an organizer choosing to unlock — that is a policy call, made visible rather than prevented. | `security/deps.require_submissions_open`, `services/projects.update_project`, `services/projects.withdraw_project`, `services/projects.unlock_project`, `services/events.shifted` |
| Late submission after the deadline | Creating a project is checked by a route dependency that runs before the request body is read; edits, withdrawals and restores are checked in the service. A closed event answers 403 to any payload that passes validation; malformed JSON gets 422 before any check. Organizer "unlock" is per project, time-boxed and audited. | `security/deps.require_submissions_open`, `services/projects.py` |
| A team withdraws or restores its project after the deadline or after results are published, silently reordering the ranking (results are computed from submitted projects) | After the submission deadline only an organizer can withdraw or restore a project (403 for team members unless the organizer unlocked that project). Once results are published nobody can, organizers included (409); a change after publishing needs an explicit, audited unpublish and republish. | `services/projects.withdraw_project`, `tests/test_teams_and_submissions.py` |
| Results published before they are final | Publishing is refused while judging is open or a community vote is still running, and when there is nothing to publish yet (judging never closed and no finished vote). Once results are published the submission deadline no longer moves, the timeline never reopens submissions after judging has started, and an import can't change the event. | `services/events.publish_blocker`, `tests/test_lifecycle_truth.py` |
| Session theft | Session tokens are random 256-bit values stored only as SHA-256 hashes; cookies are HttpOnly, SameSite=Lax, Secure under HTTPS. Logout revokes server-side. | `security/sessions.py` |
| Password attacks | argon2id hashing, login rate-limited per address, generic failure message, failed attempts audited. | `security/passwords.py`, `web/auth.py` |
| CSRF | Double-submit cookie on every HTML form. Cookie-authenticated API calls rely on SameSite=Lax: a cross-site POST carries no session cookie. JSON bodies also need a CORS preflight, which Podium never grants. Body-less API actions accept a plain form post, and sibling subdomains count as same-site, so do not host Podium next to untrusted subdomains of the same site. | `security/csrf.py` |
| XSS / injection | Jinja autoescaping everywhere; user content rendered as text; strict Content-Security-Policy (`default-src 'self'`, per-request script nonce, no inline styles); parameterised queries only. | `security/headers.py`, templates |
| Clickjacking | `frame-ancestors 'none'` and `X-Frame-Options: DENY` everywhere except the embeddable gallery route. | `security/headers.py` |
| Audit log tampering | Append-only rows chained by SHA-256 over the previous hash and the canonical row; "Verify chain" recomputes the whole chain. A deleted or edited row breaks verification from that point. Appends are serialised: a row is inserted first (taking SQLite's write lock, or a transaction-scoped advisory lock on PostgreSQL) and linked to its predecessor only then, so concurrent requests cannot fork the chain; the test suite verifies the chain after every run. | `services/audit.py`, `tests/test_audit_chain.py` |
| Stolen or leaked API token | Tokens are random 256-bit values shown once and stored as SHA-256; they can be revoked individually from the account page; every use updates `last_used_at`. | `services/tokens.py` |
| Forged webhook deliveries to a consumer | Every delivery carries `X-Podium-Signature: sha256=HMAC(secret, body)` with a per-hook secret shown once; consumers verify before trusting the payload. Deliveries never include personal data beyond public ids and titles. | `services/webhooks.py` |
| Malicious import file | Rows are matched by id only within the target event: a file that names a track, team, project or prize id belonging to another event is refused with 409 before anything is written, in a dry run too. Creating an event by import follows the event-creation rule (instance admins only unless `PODIUM_OPEN_EVENT_CREATION=true`), and updating an existing event requires organizing it. Dry run is the default (inside a rolled-back savepoint), and both the web form and `POST /api/v1/events/import` are capped at 20 MB (413). Every applied import is audited as `event.imported` with the actor, the row counts and the file's SHA-256; the fixture seed on first boot is recorded once, as System. An import never touches an event whose results are published, a file can't mark results published while judging or voting is still open, and colliding event names or judge ids get fresh ones instead of failing. | `services/importexport.py`, `seed/fixtures.foreign_ids`, `tests/test_import_integrity.py` |
| Forged certificates | Records are canonical JSON signed with the instance's ed25519 key; `/verify/{serial}` and the published public key let anyone check offline; revocation is explicit and audited. | `services/certificates.py` |
| Demo session revoked by a visitor signing out | Demo sessions are shared tokens the acceptance checker relies on; signing out of one only drops the cookie. | `security/sessions.py` |
| Demo credentials left enabled in production | Demo sessions are derived from the instance secret and only seeded when `PODIUM_DEMO_ACCOUNTS=true`; the code default is off and only `docker-compose.yml` / `make dev` turn it on. A banner shows on every page while demo mode or the default secret is active, and the app refuses to boot with the default secret behind https. | `seed/demo.py`, `config.py`, `main.py` |
| Anyone with an account creates events or takes over an instance | Event creation is limited to instance admins unless `PODIUM_OPEN_EVENT_CREATION=true`. On an empty instance the first registered account becomes admin (audited `user.bootstrap_admin`); seeded installs never reach that branch. API tokens default to read-only and expire after 90 days. | `security/deps.require_can_create_event`, `services/auth.register`, `services/tokens.py` |

## Residual risks (not mitigated by design)

- Link-mode voting can be gamed by clearing cookies; it exists because some events want zero
  friction and accept that. The flagging is a signal for humans, not a guarantee.
- A malicious organizer can void legitimate votes or unpublish results. Every such action is in
  the audit log with a reason, which is the accountability model: organizers are trusted, not
  unaccountable.
- Rate limits are in-process; behind a multi-worker deployment they should be moved to the
  database or a shared store (documented in `security/ratelimit.py`).
- Podium does not send email. Invitations and voting codes are links and codes the organizer
  distributes; the trade-off is no external dependency and no phishing surface owned by Podium.
- Accounts created by an import share `PODIUM_DEMO_PASSWORD`, because Podium sends no email and so
  cannot send a password reset. On a real instance, import people who already have accounts, or
  set a strong `PODIUM_DEMO_PASSWORD` and have them change it at `/account`.
- Webhook URLs can point at the private network (loopback, LAN). This is deliberate: offline events
  point webhooks at local services. Organizers are trusted, and a delivery stores only its status
  and error, never the response body.
- Every attendee behind one venue NAT shares one address, so they share one login and vote bucket.
  The limits are sized for bots, not for a crowd on one IP.
- The demo-secret boot guard keys on `PODIUM_BASE_URL`. A default `http://` base URL behind a TLS
  proxy is not caught; the demo banner is then the remaining signal.
- Published results are computed on read, not frozen. Changes an organizer makes after publishing
  that feed the computation (rubric weights, voided votes) change the published ranking; each is in
  the audit log, so it is accountable rather than prevented. Withdrawing and restoring projects is
  blocked outright until results are unpublished.
- Audit rows reference users and events with `ON DELETE SET NULL`. No delete path exists today,
  but adding one would rewrite `actor_id`/`event_id` on hashed rows and break chain verification;
  a future deletion feature must tombstone instead of deleting.
