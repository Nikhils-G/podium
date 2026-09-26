# Demo video script (5 minutes)

Record at 1440×900, light theme, browser zoom 110%. Two browser profiles help: one organizer, one
participant/judge. Start from a fresh `docker compose up`.

| Time | Screen | Say / show |
|---|---|---|
| 0:00 | Terminal | `docker compose up`. Point at the boot log: migrations, `seeded. test logins:` with the four cookies. "One command, no network needed at runtime." |
| 0:25 | Sign in | The demo panel: one click as **organizer** — you land on the organizer dashboard, not a home page. Point at the header: event name, stage badge, Manage / Gallery / Results, "Your events". |
| 0:35 | Organizer dashboard | Run-of-show stepper; KPIs; **Next step** card (one primary action per stage); **Attention panel**: duplicate `prj_41`, flat scorer, projects below target, a pending invitation. "The fixtures planted these; Podium found them." |
| 1:00 | Create an event | Header avatar → home → **Create an event**: "Demo Night", dates typed in your local time (label says so; stored as UTC), team size 4, public. Settings: add two tracks and a prize — they save in place. Rubric: add a criterion with weight 2. |
| 1:40 | Second profile: participant | Register → the event page is a hub: **Take part** card → create team, copy the join link → **Submit your project**: title, track, links → Save draft (banner: "Draft — not submitted") → **Submit project**. Show it in the gallery. |
| 2:15 | Organizer → Judges | Invite a judge by email → the link is shown once (Regenerate / Revoke exist). Open it in the second profile as a new user with that email → accept → lands on the judge queue; header shows "Judge 0/3". |
| 2:35 | Organizer → Assignments | Auto-assign: **Preview plan** (loads, shortfalls) → Apply. Dashboard → Next step: **Open judging**. |
| 2:50 | Judge console | Queue → **Score** → keys `1–5`, live weighted total, "Draft saved" autosave → **Submit review** → "Edit review" reopens it. Compare tab: pick with ←/→, progress counts only real picks. |
| 3:20 | curl | As the judge, `curl -H "Cookie: session=…" /api/v1/events/sample-hack-2026/judges/jdg_26/reviews` → **403**. "Isolation is in the backend." |
| 3:35 | Organizer → Results (Sample Hack) | Raw vs normalized ranking with thin-result badges, rank-shift chart (names both sides, "Table view"), judge calibration with the flat scorer, Bradley-Terry table with ρ and its "demonstration data" note. **Prizes**: award "Best overall" from the suggestion. |
| 4:00 | Organizer → Voting | Set the window at the top of the Voting page; mode = accounts, quadratic on, credits 10. Vote as the participant from a project page (randomized gallery order note); counts hidden until close + publish. |
| 4:20 | Results → Publish results | The button is disabled with a reason until judging is closed; close judging from Next step, publish with the confirmation → public results: podium, prizes, community favourite. |
| 4:35 | Certificates | Issue judge records (only after judging closed) and winner certificates (from the awards) → open one → Verify page (QR, signature valid). |
| 4:45 | /api/docs + Data | OpenAPI: both auth schemes, the error shape on every operation. Data: download `export.json`; import it → **Dry run** → "Apply this import" → "nothing new". |
| 4:55 | Terminal | `make check` → `claimed T1 T2 T3 T4, verified T1 T2`. "T3 and T4 have no automated checks; you just saw them." |
