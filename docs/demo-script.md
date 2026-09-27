# Demo video script (5 minutes)

One event, **Demo Night**, from creation to published results, then the fixture event at scale.
Record at 1440×900, light theme. Use two browser profiles: **A** is the organizer; **B** signs in
with one click as the participant, then judge_a, then judge_b (the demo panel on **Sign in**).
Start from a clean stack: `docker compose down -v && docker compose up --build`.

Type these beforehand so nothing is typed on camera: the event name `Demo Night`, the team name
`Night Owls`, the project title `Lantern`, the judge email `diego.herrera@example.org`, and the
curl command in the 2:10 beat.

| Time | Who / screen | Show and say |
|---|---|---|
| 0:00 | Terminal | The boot log: migrations, then `seeded. test logins:` with five session cookies. "One command, nothing fetched at run time." |
| 0:15 | A · organizer dashboard (Sample Hack) | The run-of-show timeline and the **Attention** panel: *Possible duplicate submission "Dry Harbour" (prj_41)* with **Withdraw duplicate**, the flat scorer jdg_07, and *8 projects have fewer than 3 submitted reviews*. "The fixtures planted these; Podium found them." |
| 0:35 | A · home → **Create an event** | `Demo Night`, submissions open now and close tomorrow (the dates are in your local time and stored in UTC), public. **Create event** → "Event created". Settings: **Add track**, **Add prize**. Rubric: **Add criterion** Impact ×2, Execution ×1. |
| 1:05 | B · participant | Open Demo Night → **Create team** `Night Owls` → "Team created" and **Copy link**. **Submit** → `Lantern` → **Save draft** (the draft banner) → **Submit project**. Show it in the gallery. |
| 1:35 | A · Judges | Invite `diego.herrera@example.org` → **Create invite link** → copy it. B signs in as judge_a, opens the link → **Accept and start judging** → "You're judging Demo Night". |
| 1:50 | A · Assignments → dashboard | **Preview plan** → **Apply 1 assignment**. Timeline: Submissions **Close now** (the dialog names the consequence) → Judging **Open judging** → notice "Judging opened". |
| 2:10 | B · judge_a review | **Score**: keys `1`–`5` on each criterion, the weighted total moves, "Saving…" then "Draft saved" with the time and its zone → **Submit review**. Terminal: `curl -H "Cookie: session=jdg_a_ab5d1b797ef777b2aa2a" http://localhost:8080/api/v1/events/sample-hack-2026/judges/jdg_26/reviews` → **403**. "Isolation is in the backend, not the template." |
| 2:40 | A · Voting | Window from now to 10 minutes from now, **Signed-in accounts** → **Save window**. B signs in as judge_b (no role in Demo Night) → **Open the ballot** → vote for Lantern. B as the participant → the vote for their own team is refused inside the control. Counts stay hidden. |
| 3:10 | A · timeline | Voting **Close now** → Judging **Close judging** → **Publish results** (refused while judging or voting is still open, and the button says why until then) → "Results published" → the public results page → **How this event was judged**. |
| 3:40 | A · Certificates + Audit log | **Issue judge records** and **Issue winner certificates** → open one → `/verify` says valid. Audit log: "Closed judging", "Published the results"; **Verify chain** → OK. |
| 4:00 | A · Results (Sample Hack) | 41 projects at scale: raw vs normalized side by side, the rank-shift chart, calibration with jdg_07 neutralised, the Confidence column (the leader is #1 in about half of the bootstrap re-draws), Bradley-Terry with its ρ. "JUDGING.md explains every number." |
| 4:35 | Terminal + GitHub | `python3 tools/run.py .dogfood.toml` → 7 of 7, `claimed T1 T2 T3 T4, verified T1 T2`. The Actions run: the tier-named test steps, the checker against a fresh build, and the network-off job, all green. "T3 and T4 have no automated checks; you just watched them." |

Before recording, rehearse twice with a stopwatch on a reset stack: every named button must be on
screen, "Demo Night" must appear at create, submit, judge and publish, and the total must stay
under 5:00. Don't run the rate-limit curl loop from the threat model right before recording; it
blocks code redeem for a minute.
