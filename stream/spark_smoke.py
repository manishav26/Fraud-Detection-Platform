"""Spark smoke test without a cluster: the real job code, in the real image (docs/spark.md).

    make spark-smoke        (prepare on the laptop, then run inside fraud/feature-job:dev)

prepare  (laptop) writes the first N stream payments as txn.raw messages, plus the features the
         offline training code (add_history) gives them
run      (in the image, read-only filesystem, non-root) streams those messages through the same
         build_query as the live job, with a file source in place of Kafka and a sink that keeps
         rows in memory in place of Cassandra, in micro-batches of 2,000; then compares
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
    print(f"Spark smoke: {len(sink.rows):,} rows written, {equal:,} of {len(expected):,} equal "
          f"to the training code ({equal / len(expected):.3%}); {seconds:.0f} s including start-up")
    spark.stop()
    return equal == len(expected)


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
