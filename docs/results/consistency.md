# Consistency: ONE vs QUORUM (week 5, on the VM)

The plan: run the same load with Cassandra at ONE and at QUORUM, count stale reads, compare P95
(Chapter 5's trade-off between speed and consistency). On the VM's 3-node Cassandra
(`docs/cassandra.md`, "Three nodes"), 3 copies of every row.

## Design

**Stale read:** a read that misses a write Cassandra already confirmed. With 3 copies, QUORUM
(2 of 3, `LOCAL_QUORUM` in our single data centre) writes to 2 and reads from 2, and 2 + 2 > 3, so
every read includes a copy with the latest write. ONE writes to 1 and reads from 1, which can be
different copies.

**Two conditions**, because on 3 healthy nodes on one machine all copies get each write within
about a millisecond, and ONE would almost never be stale either:

| Condition | What happens | What it shows |
|---|---|---|
| Healthy | all 3 nodes up | QUORUM's latency cost; stale reads in normal running |
| One node behind | `cassandra-2` is out for 3 minutes (`nodetool disablebinary`, `disablegossip`), then back without the writes it missed: hinted handoff is off on the other two (`disablehandoff`), as after an outage longer than Cassandra's 3-hour hint window | a read at ONE can be answered by the copy that missed the write; QUORUM always also asks one that has it |

**Counting stale reads** (`load/consistency_check.py`): scoring's own table, prepared queries
and code (`store/history.py`) at the level under test, on test ids of its own (`cc<run>-<n>`, one
payment each, dated 2031, after every real payment), so no real card is touched.
- Healthy: 5,000 times, write a payment, wait for Cassandra's confirmation, read it back at once.
- One node behind: write 3,000 test ids while `cassandra-2` is out, read each back once after it
  returns. Expected: about a third stale at ONE (the driver sends each read to the first copy of
  that id, and `cassandra-2` is first for about a third of them), none at QUORUM.
- The checker reaches each Cassandra pod through its own tunnel (127.0.0.2-4, Linux only), and
  maps each node's address to its tunnel, so reads are routed to a node holding the id, as in
  scoring. The run fails unless the reads reached the nodes it is about (all 3 when healthy;
  `cassandra-2` among them once it is back), so "0 stale" can't just mean the reads went elsewhere.
- Afterwards: hinted handoff on, `nodetool repair -full fraud`, and the same ids read again at ONE
  (expected: none stale).

**P95, from the same runs:** real payments through Kong at a fixed rate (`load.replay --rps`).
Scoring runs at the level under test (`CASSANDRA_CONSISTENCY`: its reads and writes), at a fixed 2
copies (HPA min = max = 2, put back afterwards). Prometheus gives scoring's history read p95 and
`/score` p95 (`observability.snapshot`); the replay gives the end-to-end p95. The feature job
stays at `LOCAL_QUORUM`.

**Runs** (`make k8s-consistency LEVEL=ONE|LOCAL_QUORUM CONDITION=healthy|behind ROUND=1|2`,
`scripts/consistency_test.sh`): 30/s, 3 minutes per phase; the rate should be about 60% of what
the ramp test finds the VM handles, the same for every run.

| Order | Run | Payments | Rows of `stream.parquet` |
|---|---|---|---|
| 1-4 | healthy: ONE 1, QUORUM 1, ONE 2, QUORUM 2 (alternating, so a drift in the VM shows) | 5,400 each | 167,240-188,839 |
| 5 | behind, ONE (out, then back) | 10,800 | 188,840-199,639 |
| 6 | behind, QUORUM | 10,800 | 199,640-210,439 |

The last 43,200 rows are this test's alone: ramp and scaling use rows before 167,240. Each run's
output: `data/consistency/<condition>-<level>-<round>/summary.md` (not in git).

## Results (VM, 11-12 Nov)

| Level | Stale reads, healthy | Stale reads, one node behind | Same ids after the repair (read at ONE) | End-to-end p95 (healthy, rounds 1 / 2) | scoring history read p95 | scoring /score p95 |
|---|---|---|---|---|---|---|
| **ONE** | **0 of 10,000** | **1,514 of 3,000 (50.5%)** | 0 of 3,000 | 23.9 / 25.4 ms | 5 / 8 ms | 10 / 17 ms |
| **QUORUM** | **0 of 10,000** | **0 of 3,000** | 0 of 3,000 | 25.4 / 25.8 ms | 5 / 5 ms | 10 / 10 ms |

Every payment of every run was answered (5,400 per phase, all 200); the "behind" runs' payment
p95 was 23-26 ms at both levels. Each run confirmed its setup: both scoring copies at the level
under test; `cassandra-2` seen as down by both others within 10 s and up again within 1 s; the
checker's reads reaching all three nodes (healthy) and `cassandra-2` (after it was back).

**What it shows.** With all copies up, neither level returned a stale read in 20,000 checks: the
three copies get each write within about a millisecond, so ONE's risk is a window too short to hit
here. With one copy behind, **ONE answered half the reads from the stale copy, and QUORUM none**:
2 + 2 > 3, so a QUORUM read always includes a copy that has the write, and Cassandra returns the
newest. The price on this VM is about 1 ms at p95 (within run-to-run noise; the history-read
histogram's smallest step is 5 ms, too coarse to separate the two). Between machines or zones a
QUORUM read waits for a second copy over a real network, so the cost there is larger. For fraud
scoring, a stale read means a payment scored without the card's latest payments, exactly the
history the velocity features need, so the platform keeps LOCAL_QUORUM.

**Why 50% and not the predicted third.** The checker records which node *coordinated* each read
(`stale_by_node`: `cassandra-2` 1,065 of its 1,065 reads, `cassandra-1` 449 of its 929,
`cassandra-0` none). At ONE the coordinator asks the one copy it currently rates fastest, as far as
we understand Cassandra's dynamic snitch, and the node that has just rejoined is the idlest, so it
was chosen more often than one time in three; the stale data came from `cassandra-2`'s copy even
when another node coordinated. The prediction assumed even routing; the measured 50% is the real
exposure.

**The repair works:** after `nodetool repair -full fraud`, the same 3,000 ids read at ONE were all
current.

**Limits:** all three nodes share one VM (8 vCPU); the "behind" node is made by switching off
hinted handoff, as an outage longer than the 3-hour hint window would. The rows were the
consistency test's own (167,240-210,439); 30 payments a second.
