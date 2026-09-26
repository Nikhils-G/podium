# Demo video script (5 minutes)

Record at 1440×900, light theme, browser zoom 110%. Two browser profiles help: one organizer, one
participant/judge. Start from a fresh `docker compose up`.

| Time | Screen | Say / show |
|---|---|---|
| 0:00 | Terminal | `docker compose up`. Point at the boot log: migrations, `seeded. test logins:` with the four cookies. "One command, no network needed at runtime." |
| 0:25 | Home → Sign in | The demo panel: one click as **organizer**. |
| 0:35 | Organizer dashboard | Run-of-show stepper; KPIs; **Attention panel**: duplicate `prj_41`, flat scorer `jdg_07`, projects below target. "The fixtures planted these; Podium found them." |
| 1:00 | Events → Create event | Create "Demo Night": dates (open now, close +2 days), team size 4, public. Add two tracks and a prize. Add a rubric criterion with weight 2. |
| 1:40 | Second profile: participant | Register, `/e/demo-night/team` → create team, copy invite link; `/join/…` in the first profile? (skip if short) → Submit a project: title, track, links → **Submit**. Show it in the gallery. |
| 2:15 | Organizer → Judges | Invite a judge by email → copy the link. Open it in the second profile as a new user → accept → judge console. |
| 2:35 | Organizer → Assignments | Auto-assign: preview (loads, shortfalls) → apply. Open judging from the dashboard. |
| 2:50 | Judge console | Queue → open project → score with keys `1–5`, live weighted total, autosave pill → **Submit review**. Compare tab: pick with ←/→. |
| 3:20 | curl | As the judge, `curl -H "Cookie: session=…" /api/v1/events/sample-hack-2026/judges/jdg_26/reviews` → **403**. "Isolation is in the backend." |
| 3:35 | Organizer → Results (Sample Hack) | Raw vs normalized ranking, rank-shift chart, judge calibration with the flat scorer, Bradley-Terry table with ρ. |
| 4:00 | Organizer → Voting | Mode = accounts, quadratic on, credits 10; set a window in Settings; vote as participant from the gallery (randomized order note); counts hidden. |
| 4:20 | Dashboard → Publish results | Confirmation text → public results page with the podium and community favourite. |
| 4:35 | Certificates → issue judge records | Open one → Verify page (QR, signature valid). |
| 4:45 | /api/docs + Data | Scroll the OpenAPI reference; download `export.json`; import it as a dry run → "nothing new". |
| 4:55 | Terminal | `make check` → `claimed T1 T2 T3 T4, verified T1 T2`. "T3 and T4 have no automated checks; you just saw them." |
