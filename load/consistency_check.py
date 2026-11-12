"""Counts stale reads in Cassandra at one consistency level (week 5; docs/results/consistency.md).

Uses scoring's own table, queries and code (store/history.py) at the level under test, on test
ids of its own (cc<run>-<n>), so no real card is touched. A read is stale if it misses a write
Cassandra already confirmed.

    # healthy: write one payment, wait for the confirmation, read it back at once; N times
    python -m load.consistency_check pairs --level ONE --run healthy-ONE-1-1794300000 --count 5000 --rate 28
    # one node behind: write while a node is out, read back after it returns
    python -m load.consistency_check seed --level ONE --run behind-ONE-1-1794300000 --count 3000 --rate 17
    python -m load.consistency_check read --level ONE --run behind-ONE-1-1794300000 --count 3000 --rate 17

Run by scripts/consistency_test.sh, on the VM: it reaches each Cassandra pod through its own tunnel
(127.0.0.2-4), and CASSANDRA_TUNNELS="cassandra-0=<pod ip>=127.0.0.2,..." lets the driver map each
node's address to its tunnel, so each read goes to a node holding that test id, as in scoring.
Prints one JSON summary and writes it to --out.
"""

import argparse
import json
import os
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta

from cassandra import DriverException
from cassandra.cluster import NoHostAvailable
from cassandra.policies import AddressTranslator

from store.history import HistoryStore, to_past

LEVELS = ("ONE", "LOCAL_QUORUM")
# What a read or write can fail with: too few copies answering (Unavailable), a timeout on the
# server or the client, or no node reachable. Anything else is a bug and stops the run.
CASSANDRA_ERRORS = (DriverException, NoHostAvailable)
BASE_TS = datetime(2031, 1, 1, tzinfo=UTC)   # after every real payment, so nothing real mixes in


class TunnelTranslator(AddressTranslator):
    """A node's own address (what Cassandra tells the driver) -> the tunnel that reaches it."""

    def __init__(self, to_tunnel):
        self.to_tunnel = to_tunnel

    def translate(self, addr):
        return self.to_tunnel.get(addr, addr)


def parse_tunnels(text):
    """'cassandra-0=10.42.1.5=127.0.0.2,...' -> ({pod ip: tunnel}, {tunnel: pod name})."""
    to_tunnel, names = {}, {}
    for part in filter(None, (text or "").split(",")):
        name, pod_ip, tunnel = part.split("=")
        to_tunnel[pod_ip], names[tunnel] = tunnel, name
    return to_tunnel, names


def run_ids(run, count):
    return [f"cc{run}-{n}" for n in range(count)]


def payment(user_id, n):
    """One test payment. Each test id gets exactly one, so 'stale' is simply 'no row'."""
    return {"txn_id": f"{user_id}-t", "user_id": user_id, "amount": 1.0,
            "ts": BASE_TS + timedelta(seconds=n), "attrs": {}}


def read_back(store, user_id, ts):
    """(rows, coordinator): scoring's own prepared read, also noting which node answered."""
    result = store.session.execute(store.select, (user_id, ts + timedelta(seconds=1)))
    return to_past(result), result.response_future.coordinator_host


def percentile(values, q):
    if not values:
        return None
    values = sorted(values)
    return values[min(len(values) - 1, int(q * len(values)))]


class Tally:
    """Counts from many threads at once."""

    def __init__(self):
        self.lock = threading.Lock()
        self.ok = self.stale = 0
        self.failed = {}
        self.stale_by_node, self.reads_by_node = {}, {}
        self.write_ms, self.read_ms = [], []

    def failure(self, op, error):
        with self.lock:
            key = f"{op}: {type(error).__name__}"
            self.failed[key] = self.failed.get(key, 0) + 1

    def read(self, rows, node, ms):
        with self.lock:
            self.read_ms.append(ms)
            self.reads_by_node[node] = self.reads_by_node.get(node, 0) + 1
            if rows:
                self.ok += 1
            else:
                self.stale += 1
                self.stale_by_node[node] = self.stale_by_node.get(node, 0) + 1

    def wrote(self, ms):
        with self.lock:
            self.write_ms.append(ms)


def paced(count, rate, work, workers=16):
    """Run work(n) for n in 0..count-1, the n-th starting at n / rate seconds (spread over the
    load run instead of all at once). rate 0: as fast as the workers go."""
    start = time.monotonic()

    def one(n):
        if rate:
            delay = start + n / rate - time.monotonic()
            if delay > 0:
                time.sleep(delay)
        work(n)

    with ThreadPoolExecutor(workers) as pool:
        list(pool.map(one, range(count)))


def check(store, mode, run, count, rate, names=None, tally=None):
    """mode: pairs (write, then read back at once), seed (write only), read (read only)."""
    tally = tally or Tally()
    names = names or {}
    ids = run_ids(run, count)

    def node(host):
        return names.get(str(host.address), str(host.address)) if host else "unknown"

    def work(n):
        txn = payment(ids[n], n)
        if mode in ("pairs", "seed"):
            t0 = time.perf_counter()
            try:
                store.save(txn)
            except CASSANDRA_ERRORS as e:   # a failed write is not a stale read: counted apart
                tally.failure("write", e)
                return
            tally.wrote((time.perf_counter() - t0) * 1000)
        if mode in ("pairs", "read"):
            t0 = time.perf_counter()
            try:
                rows, host = read_back(store, txn["user_id"], txn["ts"])
            except CASSANDRA_ERRORS as e:
                tally.failure("read", e)
                return
            tally.read(rows, node(host), (time.perf_counter() - t0) * 1000)

    paced(count, rate, work)
    return tally


def summary(tally, mode, level, run, count):
    reads = tally.ok + tally.stale
    return {
        "run": run, "mode": mode, "level": level, "count": count,
        "reads": reads, "stale": tally.stale,
        "stale_percent": round(100 * tally.stale / reads, 2) if reads else None,
        "stale_by_node": dict(sorted(tally.stale_by_node.items())),
        "reads_by_node": dict(sorted(tally.reads_by_node.items())),
        "writes": len(tally.write_ms), "failed": tally.failed,
        # through the tunnels, so slower than in the cluster: scoring's own p95 is the measure
        "write_ms_p50": percentile(tally.write_ms, 0.5), "write_ms_p95": percentile(tally.write_ms, 0.95),
        "read_ms_p50": percentile(tally.read_ms, 0.5), "read_ms_p95": percentile(tally.read_ms, 0.95),
    }


def main():
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("mode", choices=("pairs", "seed", "read"))
    p.add_argument("--level", choices=LEVELS, required=True)
    p.add_argument("--run", required=True, help="a new name per run: its test ids must be new")
    p.add_argument("--count", type=int, required=True)
    p.add_argument("--rate", type=float, default=0, help="per second; 0 = as fast as possible")
    p.add_argument("--out", help="also write the JSON summary here")
    args = p.parse_args()

    to_tunnel, names = parse_tunnels(os.getenv("CASSANDRA_TUNNELS"))
    hosts = list(names) or None
    store = HistoryStore(hosts=hosts, consistency=args.level,
                         address_translator=TunnelTranslator(to_tunnel) if to_tunnel else None)
    try:
        up = sorted(names.get(str(h.address), str(h.address))
                    for h in store.cluster.metadata.all_hosts() if h.is_up)
        result = summary(check(store, args.mode, args.run, args.count, args.rate, names),
                         args.mode, args.level, args.run, args.count)
        result["nodes_up_at_start"] = up
    finally:
        store.close()
    text = json.dumps(result, indent=2)
    print(text)
    if args.out:
        with open(args.out, "w") as f:
            f.write(text + "\n")


if __name__ == "__main__":
    main()
