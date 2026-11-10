# E6 live: probing the platform's API (week 4, step 7c)

Design: `docs/scoring.md`, "Probing the API". k3d cluster on the laptop, 10 Nov 2026, model
`fraud-lgbm-v1-535eb73c`. Manisha's offline E6: `docs/results/e6.md`.

`make k8s-probe-attack` (`load/probe_attack.py`): E6's attacker against the real API, as
merchant-1. For each of 5 fraud cards the model flags, 30 variants on the same card, 40 minutes
apart, each changing one field it controls (amount 50-100%, e-mail domains, device, card brand or
type) and keeping changes that improve the answer.

## D3: the rule works

| Run | Seed | Cards | Approved after 5 flags | Cards with an approval before 5 flags |
|---|---|---|---|---|
| **Probe detection on** | 1794254637 | 5 | **0** | 4 |
| Probe detection off (`D3=off`) | 1794254758 | 5 | **45** (on 2 cards: 25 and 20) | 3 of the other cards |

**Pass: with D3 on, no variant was approved once a card had 5 flags.** With D3 off, two cards
kept getting approvals after their 5th flag; D3 turns exactly those into reviews.

## D1: clients see only the decision

Every answer to merchant-1, in both runs, had only `txn_id` and `decision`, including one where
merchant-1 sent `X-Consumer-Groups: tester` itself (Kong replaces that header). The load-test
client (role `tester`) still gets the full answer: a 2,000-payment replay afterwards reported
774 cold starts and 149 degraded answers. ST2 passes with the new role: 60 of 60 calls
(`docs/results/security.md`).

## Finding: the attack beats D3 more often than E6 predicted

In E6, 17.5% of probing attacks got an approval before D3 froze the card. Live, 4 of the 5 cards
did, each within 1-4 tries. Five cards say little about a rate, so we ran the same attacker
offline against the same model, on 100 cards per seed (`python -m load.probe_offline`):

| How each try is scored | Seed 1794254637 | Seed 7 |
|---|---|---|
| E6, Manisha's simulation (golden days 92-109) | 17.5% (3 seeds) | |
| Our attacker, card history frozen as E6 assumes | 37% | 35% |
| **Our attacker, its own tries added to the card's history, as on the platform** | **57%** | **52%** |

(Share of attacks whose first approval comes before the card's 5th flag: those D3 can't stop.)

Two differences explain it:

1. **The attacker's own tries become the card's history** (37% to 52-57%). The platform saves
   every payment, flagged or not, so the next one sees it: after the first try, the attacker's
   e-mail domain and device are no longer new to the card (`new_P_emaildomain`,
   `new_DeviceType` drop to 0), and `secs_since_last` becomes a calm 40 minutes. Each flagged try
   makes the next one look more like a regular customer. E6 scored every variant against the
   card's history as it was before the attack, so it couldn't see this. It's the same rule as
   training (every earlier payment counts), so scoring is right to do it; it's the attacker who
   benefits.
2. **The live attacker differs a little** (17.5% to 35-37%): its time moves 40 minutes with every
   try, so the hour changes along with the chosen field; the payments are from the stream period
   (days 110-182) instead of the golden days; and the payment itself counts as the first of the 5
   flags. Each makes approval come sooner.

**What it means:** D3 is built as specified and stops every approval after 5 flags, but on the
platform it stops about half of the probing attacks, not the 82.5% E6 measured. Options, to agree
with Manisha (model side): fewer flags before freezing (2 or 3; E6's false-positive cost would need
measuring again), or not letting a card's recently flagged payments make its e-mail and device
"familiar" (a feature change, so training would change too). Not changed before Gate 3 without her.

## Notes

- Each run writes about 155 payments to the cluster's Cassandra, under new txn ids
  (`probe<seed>-...`) on real cards.
- False positives aren't measured live: E6 measured them over 90 days of real payments (+0.001
  points for D1 + D3). Replay of 2,000 payments after deploying: 0 held by probe detection.
- That replay's latency (p95 3.5 s, 149 degraded) is the overloaded laptop right after a full
  redeploy that briefly lost every node (`docs/results/scaling.md` has the normal numbers), not
  D3: D3 counts in the history read scoring already makes.
