"""Metrics of the feature job, the same for Spark and the Python writer (docs/spark.md)."""

from prometheus_client import Counter, Gauge, Histogram

# What happened to each payment read from txn.raw: new, unknown_user, duplicate, late, unreadable
PAYMENTS = Counter("feature_job_payments_total", "Payments read from txn.raw, by outcome",
                   ["outcome"])
ROWS = Counter("feature_job_rows_written_total", "Rows written to Cassandra", ["table"])
BATCH = Histogram("feature_job_batch_seconds", "Time to process and write one batch",
                  buckets=(0.05, 0.1, 0.25, 0.5, 1, 2, 5, 10, 30, 60))
# From the payment being saved in txn.raw (its Kafka timestamp) to its features being written:
# how fresh the features in Cassandra are
DELAY = Histogram("feature_job_delay_seconds", "txn.raw -> features written",
                  buckets=(0.25, 0.5, 1, 2, 3, 5, 10, 30, 60, 300, 1800))
# Writes Cassandra didn't accept the first time (a pause, a timeout), tried again
WRITE_RETRIES = Counter("feature_job_write_retries_total", "Cassandra writes tried again")
LAST_BATCH = Gauge("feature_job_last_batch_rows", "Payments in the most recent batch")
