# Admin promote and rollback (week 4, step 6)

Design: `docs/mlflow.md`. Run on compose, then on the k3d cluster (2 scoring pods), 8 Nov 2026.
Models: tuned v1 (`fraud-lgbm-v1-535eb73c`, registry version 4 on both) and the week-2 model
(`fraud-lgbm-v1-ea89630b`, version 5 on compose, 1 on the cluster).

## Who may: ST2 with the model routes (compose and cluster, identical)

| Route | no token | client | analyst | admin |
|---|---|---|---|---|
| See the live model | 401 | 403 | 403 | 200 |
| Model history | 401 | 403 | 403 | 200 |
| Promote a model | 401 | 403 | 403 | 400 |
| Roll back the model | 401 | 403 | 403 | 400 |

Admin's 400 is ST2's unsigned test promotion being refused after Kong and the role check: access is
proven without changing the model. All 32 calls (8 routes x 4 callers) match the roles table.

The first run gave admin **429** on the last two rows: Kong's limit on these routes was 2 per
second, and ST2 sends 4 admin calls in one second (`make promote` sends 2). Raised to 5 per second
and 60 per minute.

## The demo flow (cluster)

| Step | Command | Result |
|---|---|---|
| 1 | `make k8s-promote VERSION=4 OVERRIDE="record tuned v1 ..."` | Gate vs the pinned model (the same files): +0.0000 PR-AUC, so **keep**; overridden. Live: `535eb73c` |
| 2 | `make k8s-promote VERSION=1` | Gate: **-0.0028 PR-AUC, keep. Not promoted**, nothing signed |
| 3 | `make k8s-promote VERSION=1 OVERRIDE="demo: show a live promotion"` | Live: `ea89630b`, recorded as admin |
| 4 | `make k8s-rollback REASON="week-2 model flags more payments"` | Back to version 4 with no gate. Live: `535eb73c` |

**Both scoring pods** logged the same sequence:

```
now serving fraud-lgbm-v1-535eb73c (registry version 4, sequence 1794098023392)   <- at startup
now serving fraud-lgbm-v1-535eb73c (registry version 4, sequence 1794159325114)   <- step 1
now serving fraud-lgbm-v1-ea89630b (registry version 1, sequence 1794159645379)   <- step 3
now serving fraud-lgbm-v1-535eb73c (registry version 4, sequence 1794159721895)   <- step 4
```

**The audit log** (`make k8s-model-history`), newest first:

| Row | Time (UTC) | Who | Action | Version | Gate | Reason |
|---|---|---|---|---|---|---|
| 1799 | 17:42:01 | admin | model_rolled_back | 4 (`535eb73c`) | none (rollback) | week-2 model flags more payments |
| 1798 | 17:40:45 | admin | model_promoted | 1 (`ea89630b`) | keep: -0.0028 PR-AUC, 17.06 ms, 1.80% flagged | demo: show a live promotion |
| 1797 | 17:35:25 | admin | model_promoted | 4 (`535eb73c`) | keep: +0.0000 PR-AUC, 12.33 ms, 1.33% flagged | record tuned v1 as live through the audited path |

`GET /v1/model` then answered version 4, rolled back by admin, with `previous: "1"`.
`make k8s-verify-audit`: `OK: 1,776 rows, chain intact`.

**Compose** gave the same results: the gate stopped version 5 (-0.0028 PR-AUC), the override and
the rollback went through, scoring logged `535eb73c` -> `ea89630b` -> `535eb73c`, and
`make verify-audit` said `OK: 3,912 rows, chain intact`.

## What this shows

- **Every model change is named and recorded before it goes out:** who, when, which files (hash),
  the gate's verdict and numbers, and why. An override is visible in the log, not hidden.
- **The gate stops a worse model by default:** the week-2 model went live only with a written
  reason.
- **Rollback is one command and needs no gate**, and it can only go to the version live before.
- **The signing key never left the laptop:** model-admin-svc only checked and passed on what was
  signed there.

## Finding: the 5 ms "fast" rule fails on this laptop

The gate's speed check (score + reasons, one payment at a time, p95) on this laptop:

| Model | Here (Intel, Docker running) | Manisha's machine (`docs/results/promotion_gate.md`) |
|---|---|---|
| tuned v1 | 12.33 ms | 3.04 ms |
| week-2 model | 15.86–28.13 ms | 3.59 ms |

The same code is 4–8 times slower here, so the 5 ms rule fails even for tuned v1. This matches
the cluster's `reasons` step in the baseline (24–49 ms p95, `docs/results/baseline.md`): the
explanations, not the model call, are what is slow on this CPU. For the gate, either measure speed
on the machine the platform runs on and set the limit from that, or keep 5 ms as the target and
treat the laptop as the limit. To decide with Manisha.

## Known limit (closed in week 5)

Anything that could reach Redpanda could publish to `model.events` directly, skipping the audit
row (it still couldn't forge a signature). Closed by Redpanda users and topic permissions
(`docs/kafka.md`): only model-admin's user may write `model.events`, and `make k8s-kafka-check`
shows ingest, scoring and the laptop tools refused.
