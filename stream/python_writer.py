"""The feature job without Spark: a plain Python consumer (week 4, step 1). Design: docs/spark.md

The fallback if the Spark job isn't ready, and the baseline its throughput is compared with. Same
state code (stream/state.py), same rows and writes (stream/sink.py), same metrics.

    python -m stream.python_writer                    read txn.raw from the start, keep going
    python -m stream.python_writer --from latest      only payments sent from now on
    python -m stream.python_writer --until-idle 10    stop after 10 s without new payments and
                                                      print payments per second (throughput)

State is kept in memory only, so this always starts from the beginning of the topic (or from
now) and rebuilds it; writes are upserts, so rewriting rows is harmless. The Spark job keeps its
state in a checkpoint instead.
"""

import argparse
import asyncio
import os
import time
from datetime import UTC, datetime

from aiokafka import AIOKafkaConsumer
from prometheus_client import start_http_server

from kafka_login import aiokafka_options
from stream import metrics as m
from stream import state as st
from stream.sink import FeatureSink, feature_params, parse, user_params

TOPIC = os.getenv("TOPIC", "txn.raw")


def process(states, records):
    """records: (parsed message or None, kafka timestamp in seconds) in arrival order.
    Updates `states` in place; returns (feature rows, user rows, outcome counts, delays)."""
    written_at = datetime.now(UTC)
    features, users, counts, kafka_times = [], {}, {}, []
    for msg, kafka_t in records:
        if msg is None:
            counts["unreadable"] = counts.get("unreadable", 0) + 1
            continue
        uid = msg["user_id"]
        status, feats, new = st.update(states.get(uid, st.empty()), msg["txn_id"], uid, msg["t"],
                                       msg["amount"], msg)
        counts[status] = counts.get(status, 0) + 1
        if feats is not None:
            features.append(feature_params(msg["txn_id"], uid, msg["t"], feats, written_at))
            kafka_times.append(kafka_t)
        if status == st.NEW:
            states[uid] = new
            users[uid] = user_params(uid, st.summary(new, new["last_t"]), written_at)
    delays = [written_at.timestamp() - k for k in kafka_times]
    return features, list(users.values()), counts, delays


async def run(bootstrap, start, until_idle, batch):
    # No consumer group: nothing is committed, so every start begins at `start` (earliest or
    # latest) on every partition, and the state is rebuilt from there. Subscribing lets the
    # client look up the topic's partitions itself (asking before its first metadata fetch gave
    # none: the first cluster run crashed on that)
    consumer = AIOKafkaConsumer(TOPIC, bootstrap_servers=bootstrap, enable_auto_commit=False,
                                group_id=None, auto_offset_reset=start, **aiokafka_options())
    await consumer.start()
    sink = FeatureSink()
    states, total, first, last_new = {}, 0, None, time.monotonic()
    try:
        while True:
            got = await consumer.getmany(timeout_ms=500, max_records=batch)
            records = [(parse(r.value), r.timestamp / 1000) for rs in got.values() for r in rs]
            if not records:
                if until_idle and time.monotonic() - last_new > until_idle:
                    break
                continue
            started = time.perf_counter()
            first = first or time.monotonic()
            features, users, counts, delays = process(states, records)
            sink.write(features, users)
            for outcome, n in counts.items():
                m.PAYMENTS.labels(outcome).inc(n)
            m.ROWS.labels("features_by_txn").inc(len(features))
            m.ROWS.labels("user_features").inc(len(users))
            for d in delays:
                m.DELAY.observe(d)
            m.BATCH.observe(time.perf_counter() - started)
            m.LAST_BATCH.set(len(records))
            total += len(records)
            last_new = time.monotonic()
    finally:
        await consumer.stop()
        sink.close()
    if total:
        seconds = max(last_new - first, 1e-6)   # first batch read -> last batch written
        print(f"Python writer: {total:,} payments in {seconds:.1f} s "
              f"({total / seconds:,.0f} per second), {len(states):,} users in state")


def main():
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("--from", dest="start", choices=["earliest", "latest"], default="earliest")
    p.add_argument("--until-idle", type=float, default=0, help="stop after this many idle seconds")
    p.add_argument("--batch", type=int, default=5000, help="payments per batch at most")
    p.add_argument("--metrics-port", type=int, default=int(os.getenv("METRICS_PORT", "0")))
    args = p.parse_args()
    if args.metrics_port:
        start_http_server(args.metrics_port)
    asyncio.run(run(os.getenv("KAFKA_BOOTSTRAP", "localhost:9092"), args.start, args.until_idle,
                    args.batch))


if __name__ == "__main__":
    main()
