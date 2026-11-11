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

## Results

*(to be filled in from the VM runs)*

| Level | Stale reads, healthy | Stale reads, one node behind | History read p95 | `/score` p95 | End-to-end p95 |
|---|---|---|---|---|---|
| ONE | | | | | |
| QUORUM | | | | | |

**Limits:** all 3 Cassandra nodes share one VM, so a QUORUM read waits for a second copy over a
local network, not between machines or zones: its latency cost here is smaller than in a real
deployment. The "behind" node is made by switching off hinted handoff, which is what an outage
longer than the hint window does.
