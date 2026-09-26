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
| Bots posting votes | Honeypot field (silently rejected and logged), per-IP rate limits on vote/comment/login/register, CSRF on every HTML form. | `services/voting.py`, `security/ratelimit.py`, `security/csrf.py` |
| Position bias in ballots | Ballot order is a deterministic shuffle keyed by voter identity: stable for one voter, different across voters. | `services/voting.ballot_order` |
| Early leak of vote counts or rankings | Tallies are exposed only when the window has closed **and** results are published; both checks are in the service and apply to the API as well as pages. Organizers see everything, always. | `services/voting.tallies_visible`, `results_visible` |
| Late submission after the deadline | The window check is a route dependency that runs before the request body is parsed; a closed event answers 403 to any payload. Organizer "unlock" is per project, time-boxed and audited. | `security/deps.require_submissions_open` |
| Session theft | Session tokens are random 256-bit values stored only as SHA-256 hashes; cookies are HttpOnly, SameSite=Lax, Secure under HTTPS. Logout revokes server-side. | `security/sessions.py` |
| Password attacks | argon2id hashing, login rate-limited per address, generic failure message, failed attempts audited. | `security/passwords.py`, `web/auth.py` |
| CSRF | Double-submit cookie on every HTML form; the JSON API is protected by content-type (browsers cannot send cross-site JSON without a preflight and Podium enables no CORS on mutating routes). | `security/csrf.py` |
| XSS / injection | Jinja autoescaping everywhere; user content rendered as text; strict Content-Security-Policy (`default-src 'self'`, per-request script nonce, no inline styles); parameterised queries only. | `security/headers.py`, templates |
| Clickjacking | `frame-ancestors 'none'` and `X-Frame-Options: DENY` everywhere except the embeddable gallery route. | `security/headers.py` |
| Audit log tampering | Append-only rows chained by SHA-256 over the previous hash and the canonical row; "Verify chain" recomputes the whole chain. A deleted or edited row breaks verification from that point. | `services/audit.py` |
| Stolen or leaked API token | Tokens are random 256-bit values shown once and stored as SHA-256; they can be revoked individually from the account page; every use updates `last_used_at`. | `services/tokens.py` |
| Forged webhook deliveries to a consumer | Every delivery carries `X-Podium-Signature: sha256=HMAC(secret, body)` with a per-hook secret shown once; consumers verify before trusting the payload. Deliveries never include personal data beyond public ids and titles. | `services/webhooks.py` |
| Malicious import file | Imports are validated for shape, matched by id (no arbitrary writes), refused for another event unless the caller organizes it, dry-run by default inside a rolled-back savepoint, size-capped at 20 MB, and audited. | `services/importexport.py`, `web/organizer_more.py` |
| Forged certificates | Records are canonical JSON signed with the instance's ed25519 key; `/verify/{serial}` and the published public key let anyone check offline; revocation is explicit and audited. | `services/certificates.py` |
| Demo session revoked by a visitor signing out | Demo sessions are shared tokens the acceptance checker relies on; signing out of one only drops the cookie. | `security/sessions.py` |
| Demo credentials left enabled in production | Demo sessions are derived from the instance secret and only seeded when `PODIUM_DEMO_ACCOUNTS=true` (default for evaluation). The README tells operators to disable it and rotate the secret. | `seed/demo.py`, README |

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
