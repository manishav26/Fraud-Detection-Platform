"""Write stream payments straight into a Redpanda topic, for the feature job's parity and
throughput runs only (week 4, step 1; docs/spark.md). Test tool: it skips Kong and ingest.

    python -m stream.load_raw --topic txn.parity --limit 10000
    python -m stream.load_raw --topic txn.bench --limit 100000 --prefix bench-

Messages are exactly what ingest writes to txn.raw (load.replay builds them the same way), keyed
by user_id like ingest, in time order. --prefix makes txn ids unique for repeated runs.
"""

import argparse
import asyncio
import json
import os
import time
from pathlib import Path

from aiokafka import AIOKafkaProducer

from kafka_login import aiokafka_options
from load.replay import attr_columns, load_rows, to_request
from pseudonym import pseudonym

ROOT = Path(__file__).resolve().parents[1]


async def send(topic, rows, prefix, bootstrap):
    producer = AIOKafkaProducer(bootstrap_servers=bootstrap, acks="all", linger_ms=20,
                                max_batch_size=1_048_576,
                                value_serializer=lambda v: json.dumps(v).encode(),
                                # the `tools` user: may write only the test topics (docs/kafka.md)
                                **aiokafka_options())
    await producer.start()
    attrs, pending, sent = attr_columns(), [], 0
    try:
        for row in rows.to_dict("records"):
            msg = to_request(row, attrs)
            msg["txn_id"] = prefix + msg["txn_id"]
            # what ingest would have written: this tool skips ingest (docs/privacy.md)
            msg["user_id"] = pseudonym(msg["user_id"])
            pending.append(await producer.send(topic, msg, key=msg["user_id"].encode()))
            if len(pending) >= 5000:
                await asyncio.gather(*pending)
                sent += len(pending)
                pending = []
        await asyncio.gather(*pending)
        sent += len(pending)
    finally:
        await producer.stop()
    return sent


def main():
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("--topic", required=True)
    p.add_argument("--limit", type=int, default=10_000)
    p.add_argument("--offset", type=int, default=0)
    p.add_argument("--prefix", default="")
    p.add_argument("--data", default=ROOT / "data" / "processed" / "stream.parquet")
    args = p.parse_args()
    if args.topic == "txn.raw":
        raise SystemExit("not txn.raw: this tool is for the feature job's own test topics")
    rows = load_rows(args.data, args.limit, args.offset)
    start = time.perf_counter()
    n = asyncio.run(send(args.topic, rows, args.prefix, os.getenv("KAFKA_BOOTSTRAP", "localhost:9092")))
    print(f"wrote {n:,} payments to {args.topic} in {time.perf_counter() - start:.1f} s")


if __name__ == "__main__":
    main()
