"""Spark smoke test without a cluster: the real job code, in the real image (docs/spark.md).

    make spark-smoke        (prepare on the laptop, then run inside fraud/feature-job:dev)

prepare  (laptop) writes the first N stream payments as txn.raw messages, plus the features the
         offline training code (add_history) gives them
run      (in the image, read-only filesystem, non-root) streams those messages through the same
         build_query as the live job, with a file source in place of Kafka and a sink that keeps
         rows in memory in place of Cassandra, in micro-batches of 2,000; then compares.
         The first batch also holds BAD messages (a time Spark can't read, not JSON, no txn_id,
         text for the amount): each must be counted as unreadable and the batch must go on
"""

import argparse
import json
import math
import os
import sys
import time
import zlib
from pathlib import Path

from features.history import HISTORY_FEATURES

# Messages the job must drop and count, never fail on. The first is what ST5's fuzzer once got
# past ingest: Java allows offsets up to 18:00 only, and the strict cast crash-looped the job.
BAD = [
    json.dumps({"txn_id": "bad-time", "user_id": "u-bad", "amount": 1.0,
                "ts": "0387-08-26T14:46:05-21:11", "attrs": {}}),
    "not json at all",
    json.dumps({"user_id": "u-bad", "amount": 1.0, "ts": "2017-12-02T00:00:00Z"}),
    json.dumps({"txn_id": "bad-amount", "user_id": "u-bad", "amount": "x",
                "ts": "2017-12-02T00:00:00Z"}),
]


def prepare(folder, limit):
    from load.replay import attr_columns, load_rows, to_request
    from stream.parity import offline

    folder.mkdir(parents=True, exist_ok=True)
    attrs = attr_columns()
    rows = load_rows(Path("data/processed/stream.parquet"), limit)
    per_partition = {}
    for i, r in enumerate(rows.to_dict("records")):
        msg = to_request(r, attrs)
        part = zlib.crc32(msg["user_id"].encode()) % 6   # keyed by user, like txn.raw
        per_partition.setdefault(part, []).append(
            {"value": json.dumps(msg), "partition": part, "offset": i, "kafka_t": 0.0})
    (folder / "in").mkdir(exist_ok=True)
    # Several files, so the file source makes several micro-batches (maxFilesPerTrigger=1)
    chunk = 2000
    msgs = sorted((m for ms in per_partition.values() for m in ms), key=lambda m: m["offset"])
    msgs[1:1] = [{"value": v, "partition": 0, "offset": limit + i, "kafka_t": 0.0}
                 for i, v in enumerate(BAD)]   # in the first micro-batch, among good ones
    for k in range(0, len(msgs), chunk):
        with open(folder / "in" / f"{k:08d}.json", "w") as f:
            f.writelines(json.dumps(m) + "\n" for m in msgs[k:k + chunk])
    expected = offline(limit)[["txn_id", *HISTORY_FEATURES]]
    expected.to_json(folder / "expected.json", orient="records")
    print(f"prepared {len(msgs):,} messages in {folder}")


class MemorySink:
    def __init__(self):
        self.rows = {}

    def write(self, feature_rows, user_rows):
        for r in feature_rows:
            self.rows[r[2]] = dict(zip(HISTORY_FEATURES, r[4:4 + len(HISTORY_FEATURES)]))


def same(a, b):
    a = float("nan") if a is None else float(a)
    b = float("nan") if b is None else float(b)
    if math.isnan(a) or math.isnan(b):
        return math.isnan(a) and math.isnan(b)
    return math.isclose(a, b, rel_tol=1e-9, abs_tol=1e-9)


def run(folder):
    from pyspark.sql.types import DoubleType, LongType, StringType, StructField, StructType

    from stream import spark_job

    spark = spark_job.session()
    spark.sparkContext.setLogLevel("WARN")
    schema = StructType([StructField("value", StringType()), StructField("partition", LongType()),
                         StructField("offset", LongType()), StructField("kafka_t", DoubleType())])
    source = (spark.readStream.schema(schema).option("maxFilesPerTrigger", 1)
              .json(str(folder / "in")))
    sink = MemorySink()
    start = time.perf_counter()
    os.environ["CHECKPOINT"] = f"/tmp/smoke-checkpoint-{int(time.time())}"   # always empty state
    query = spark_job.build_query(spark, sink, source=source, trigger={"availableNow": True})
    query.awaitTermination()
    seconds = time.perf_counter() - start
    expected = json.loads((folder / "expected.json").read_text())
    equal = sum(1 for e in expected if e["txn_id"] in sink.rows
                and all(same(sink.rows[e["txn_id"]][f], e[f]) for f in HISTORY_FEATURES))
    from stream import metrics
    unreadable = metrics.PAYMENTS.labels("unreadable")._value.get()
    bad_written = [t for t in ("bad-time", "bad-amount") if t in sink.rows]
    print(f"Spark smoke: {len(sink.rows):,} rows written, {equal:,} of {len(expected):,} equal "
          f"to the training code ({equal / len(expected):.3%}); {seconds:.0f} s including start-up")
    print(f"bad messages: {unreadable:.0f} of {len(BAD)} counted as unreadable, "
          f"{len(bad_written)} written")
    spark.stop()
    return equal == len(expected) and unreadable == len(BAD) and not bad_written


def main():
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("step", choices=["prepare", "run"])
    p.add_argument("--folder", type=Path, default=Path("data/spark-smoke"))
    p.add_argument("--limit", type=int, default=10_000)
    args = p.parse_args()
    if args.step == "prepare":
        prepare(args.folder, args.limit)
    else:
        sys.exit(0 if run(args.folder) else 1)


if __name__ == "__main__":
    main()
