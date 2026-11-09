"""The streaming feature job on Spark Structured Streaming (week 4, step 1). Design: docs/spark.md

    txn.raw (Redpanda, keyed by user_id)
      -> parse
      -> groupBy(user_id).applyInPandasWithState: per-user state (stream/state.py), the 12
         history features of each payment as of that payment
      -> foreachBatch: write features_by_txn and user_features to Cassandra (stream/sink.py)
    checkpoint: offsets and every user's state, so a restart resumes exactly where it stopped

Settings (environment):
  KAFKA_BOOTSTRAP      redpanda:29092
  TOPIC                txn.raw (parity and throughput runs: their own topic)
  STARTING_OFFSETS     earliest (default) or latest; only used when there is no checkpoint yet
  CHECKPOINT           /checkpoint/feature-job
  MAX_PER_TRIGGER      payments per micro-batch at most (default 20000)
  TRIGGER_SECONDS      start a micro-batch at most this often (default 1)
  SPARK_JARS           the Kafka connector jars, baked into the image
  METRICS_PORT         8000
"""

import os
import time
from datetime import UTC, datetime

import pandas as pd

from features.history import HISTORY_FEATURES, NOVELTY_OF
from kafka_login import spark_options
from stream import metrics as m
from stream import state as st
from stream.sink import FeatureSink, feature_params, user_params

TOPIC = os.getenv("TOPIC", "txn.raw")   # the parity and throughput runs use their own topics
# The user's summary after each payment, prefixed: two of its names are also feature names
SUMMARY = ["n_txns", "last_t", "txn_count_24h", "amt_sum_24h"]
USER = [f"user_{k}" for k in SUMMARY]
# One output row per payment: what happened, its features, and the user's summary after it
OUTPUT_COLUMNS = ["txn_id", "user_id", "t", "kafka_t", "status", *HISTORY_FEATURES, *USER]


def apply_group(state_json, rows):
    """One user's payments in one micro-batch -> (new state JSON, output rows as a DataFrame).
    rows: DataFrame with txn_id, user_id, t, amount, the NOVELTY_OF columns, partition, offset,
    kafka_t. Plain pandas, so it is tested without Spark (tests/test_spark_job.py)."""
    s = st.loads(state_json)
    rows = rows.sort_values(["t", "partition", "offset"], kind="stable")
    out = []
    for r in rows.to_dict("records"):
        values = {c: (None if pd.isna(r[c]) else r[c]) for c in NOVELTY_OF}
        status, feats, s = st.update(s, r["txn_id"], r["user_id"], float(r["t"]),
                                     float(r["amount"]), values)
        # Only a counted payment has a summary; unknown users, duplicates and late ones get none
        summary = st.summary(s, s["last_t"]) if status == st.NEW else dict.fromkeys(SUMMARY)
        out.append({"txn_id": r["txn_id"], "user_id": r["user_id"], "t": float(r["t"]),
                    "kafka_t": float(r["kafka_t"]), "status": status,
                    **(feats or st.no_history()), **{f"user_{k}": summary[k] for k in SUMMARY}})
    return st.dumps(s), pd.DataFrame(out, columns=OUTPUT_COLUMNS)


def batch_rows(pdf, written_at):
    """A micro-batch's output -> (feature rows, user rows, outcome counts, delays) for the sink.
    Duplicates and late payments write nothing; unknown users get a features row (all missing,
    as training gives them) but no user_features row."""
    counts = pdf["status"].value_counts().to_dict()
    written = pdf[pdf["status"].isin([st.NEW, st.UNKNOWN_USER])]
    features = [feature_params(r["txn_id"], r["user_id"], r["t"], r, written_at)
                for r in written.to_dict("records")]
    known = pdf[pdf["status"] == st.NEW].sort_values("t").drop_duplicates("user_id", keep="last")
    users = [user_params(r["user_id"], {k: r[f"user_{k}"] for k in SUMMARY}, written_at)
             for r in known.to_dict("records")]
    delays = (written_at.timestamp() - written["kafka_t"]).tolist()
    return features, users, counts, delays


# ---------- Spark: only imported when the job runs ----------

def message_schema():
    from pyspark.sql.types import DoubleType, StringType, StructField, StructType
    return StructType([
        StructField("txn_id", StringType()), StructField("user_id", StringType()),
        StructField("amount", DoubleType()), StructField("ts", StringType()),
        StructField("attrs", StructType([StructField(c, StringType()) for c in NOVELTY_OF]))])


SPARK_GROUP_PREFIX = "spark-kafka-source"   # Spark's default, made explicit


def kafka_source(spark):
    """txn.raw as a stream of (value, partition, offset, kafka_t)."""
    from pyspark.sql import functions as F
    reader = spark.readStream.format("kafka")
    # Its Redpanda user may only read the payment topics, with Spark's own consumer group
    # names (prefix spark-kafka-source, set here so the permission can match it; docs/kafka.md)
    for key, value in spark_options().items():
        reader = reader.option(key, value)
    return (reader
           .option("kafka.bootstrap.servers", os.getenv("KAFKA_BOOTSTRAP", "redpanda:29092"))
           .option("groupIdPrefix", SPARK_GROUP_PREFIX)
           .option("subscribe", TOPIC)
           .option("startingOffsets", os.getenv("STARTING_OFFSETS", "earliest"))
           .option("maxOffsetsPerTrigger", int(os.getenv("MAX_PER_TRIGGER", "20000")))
           .option("failOnDataLoss", "false")   # a topic recreated for a parity run
           .load()
           .select(F.col("value").cast("string").alias("value"), "partition", "offset",
                   F.col("timestamp").cast("double").alias("kafka_t")))


def build_query(spark, sink, source=None, trigger=None):
    """source: a stream with value (JSON text), partition, offset, kafka_t; Kafka by default
    (the smoke test passes a file stream). trigger: keyword arguments for .trigger()."""
    from pyspark.sql import functions as F
    from pyspark.sql.streaming.state import GroupStateTimeout
    from pyspark.sql.types import DoubleType, StringType, StructField, StructType

    raw = kafka_source(spark) if source is None else source
    parsed = (raw.select(F.from_json("value", message_schema()).alias("m"),
                         "partition", "offset", "kafka_t")
              .where(F.col("m.txn_id").isNotNull() & F.col("m.user_id").isNotNull())
              .select("m.txn_id", "m.user_id", "m.amount",
                      F.unix_micros(F.to_timestamp("m.ts")).cast("double").alias("t_us"),
                      *[F.col(f"m.attrs.{c}").alias(c) for c in NOVELTY_OF],
                      "partition", "offset", "kafka_t")
              .withColumn("t", F.col("t_us") / 1e6).drop("t_us")
              .where(F.col("t").isNotNull() & F.col("amount").isNotNull()))

    output = StructType(
        [StructField("txn_id", StringType()), StructField("user_id", StringType()),
         StructField("t", DoubleType()), StructField("kafka_t", DoubleType()),
         StructField("status", StringType())]
        + [StructField(f, DoubleType()) for f in HISTORY_FEATURES]
        # Doubles, not longs: unknown users have no summary (missing), which a long can't hold
        + [StructField(c, DoubleType()) for c in USER])

    def per_user(key, pdfs, state):
        rows = pd.concat(list(pdfs), ignore_index=True)
        new_state, out = apply_group(state.get[0] if state.exists else None, rows)
        state.update((new_state,))
        yield out

    features = parsed.groupBy("user_id").applyInPandasWithState(
        per_user, output, StructType([StructField("state", StringType())]), "append",
        GroupStateTimeout.NoTimeout)

    def write_batch(batch_df, batch_id):
        started = time.perf_counter()
        pdf = batch_df.toPandas()
        if pdf.empty:
            return
        feature_rows, user_rows, counts, delays = batch_rows(pdf, datetime.now(UTC))
        sink.write(feature_rows, user_rows)
        for outcome, n in counts.items():
            m.PAYMENTS.labels(outcome).inc(n)
        m.ROWS.labels("features_by_txn").inc(len(feature_rows))
        m.ROWS.labels("user_features").inc(len(user_rows))
        for d in delays:
            m.DELAY.observe(d)
        m.BATCH.observe(time.perf_counter() - started)
        m.LAST_BATCH.set(len(pdf))

    return (features.writeStream.foreachBatch(write_batch)
            .option("checkpointLocation", os.getenv("CHECKPOINT", "/checkpoint/feature-job"))
            .trigger(**(trigger or {"processingTime": f"{os.getenv('TRIGGER_SECONDS', '1')} seconds"}))
            .start())


def session():
    from pyspark.sql import SparkSession
    return (SparkSession.builder.master(os.getenv("SPARK_MASTER", "local[2]"))
             .appName("feature-job")
             .config("spark.jars", os.getenv("SPARK_JARS", ""))
             # One shuffle partition per topic partition: users are already spread across 6
             .config("spark.sql.shuffle.partitions", "6")
             .config("spark.sql.session.timeZone", "UTC")
             .config("spark.ui.enabled", "false")   # nothing would reach it; saves memory
             .config("spark.ui.showConsoleProgress", "false")   # progress bars fill the log
             .config("spark.local.dir", "/tmp/spark")
             .getOrCreate())


def main():
    from prometheus_client import start_http_server

    start_http_server(int(os.getenv("METRICS_PORT", "8000")))
    spark = session()
    spark.sparkContext.setLogLevel("WARN")
    sink = FeatureSink()
    query = build_query(spark, sink)
    print(f"feature job reading {TOPIC}; checkpoint {os.getenv('CHECKPOINT', '/checkpoint/feature-job')}",
          flush=True)
    last = 0.0
    while query.isActive:
        query.awaitTermination(30)
        p = query.lastProgress
        if p and time.monotonic() - last > 60:   # a line a minute: rows and speed
            print(f"batch {p['batchId']}: {p['numInputRows']} payments, "
                  f"{p.get('processedRowsPerSecond', 0):.0f} per second", flush=True)
            last = time.monotonic()
    # The query stopped (an error): exit non-zero so Kubernetes restarts the pod, which resumes
    # from the checkpoint
    raise SystemExit(f"feature job stopped: {query.exception()}")


if __name__ == "__main__":
    main()
