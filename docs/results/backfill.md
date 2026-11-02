# Backfill: real history for live users (week 3, step 3)

Compose stack, k3d off, `make backfill`. Design: `docs/cassandra.md`.

## The load

337,614 payments of known users from days 1-109, at about 2,300 rows per second overall, about
2.4 minutes. Stored and expected counts matched for 1,000 random users, and again on a later check
after a replay had added newer rows.

## Before and after: the same first 5,000 stream payments

178 are really fraud.

| | Before (empty history) | After backfill |
|---|---:|---:|
| Cold starts | 4,073 | **1,994** |
| Approve / review / decline | 4,921 / 42 / 37 | 4,918 / 44 / 38 |
| Frauds flagged | 62 | 63 |
| False alarms | 17 | 19 |
| Scores that changed | | 1,008 |
| Decisions that changed | | 3 |
| Payments per second, at full speed | 81 | 60 |
| p50 latency at full speed | 272 ms | 355 ms |

- **History now reaches the model:** cold starts halved and 1,008 scores changed. The remaining
  1,994 include 527 unknown users, who never have history, and users first seen in these days.
- **Almost no change in fraud results here:** only 3 decisions changed, 1 catching a fraud (0.37 to
  0.91, now declined) and 2 false alarms. That fits E2's small gain (+0.003 PR-AUC). 5,000 payments
  and 178 frauds are too few to say more. The full stream and the parity test are the real checks.
- **Possibly slower:** 25% fewer payments per second, as users have more history to read. This is
  one run each at full speed, and earlier runs have varied a lot, so treat it as a question for the
  baseline, not a result.
