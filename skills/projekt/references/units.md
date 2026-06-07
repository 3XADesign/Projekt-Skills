# Estimation units — story points → hours

**The trap:** `POST /ai/suggest-estimation` returns **story_points only**, but issues store
`estimated_hours`. You must convert, and you must flag AI-derived values for human review.

- Conversion table: `assets/points_hours.json` (Fibonacci → hours; sane defaults, **calibrate per org**).
- Every value written from an AI suggestion is marked AI-suggested (prefix the issue's estimate note or
  add an `ai-estimated` label) so a human can confirm.
- On AI **503** (daily quota), don't block: fall back to **median estimated_hours of sibling issues**
  (same project/sprint/type). Mark those as `heuristic` too.

Default map (`points_hours.json`):

| Points | Hours |
|---|---|
| 1 | 2 |
| 2 | 4 |
| 3 | 8 |
| 5 | 16 |
| 8 | 32 |
| 13 | 56 |
| 21 | 96 |

Calibrate by comparing planned vs logged hours (`/time-summary`, `/workload`) after a few sprints and
editing `points_hours.json` — it's the single source of truth for the `projekt-estimate` skill.
