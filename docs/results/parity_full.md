# Train/serve parity (full mode)

Cassandra holds days 1-109 (make backfill) plus the replay; offline history uses all days, as in training. Online = `store.past()` from Cassandra + `history_for_one`; offline = `add_history`. Replay LIMIT 20,000; 10,000 random payments of known users compared on all 12 history features (tolerance 1e-6).

**PASS: 100.0000% of rows equal (target 99.9%); 0 mismatches not explained by same-second payments.**

| rows                             |   count | all 12 features equal   |
|:---------------------------------|--------:|:------------------------|
| all compared                     |   10000 | 100.0000%               |
| same-second (expected to differ) |       0 | -                       |
| all others                       |   10000 | 100.0000%               |

## Per feature

| feature           | equal     |
|:------------------|:----------|
| n_prev_txns       | 100.0000% |
| txn_count_1h      | 100.0000% |
| txn_count_24h     | 100.0000% |
| txn_count_30d     | 100.0000% |
| amt_sum_24h       | 100.0000% |
| amt_mean_30d      | 100.0000% |
| amt_std_30d       | 100.0000% |
| amount_zscore     | 100.0000% |
| secs_since_last   | 100.0000% |
| new_P_emaildomain | 100.0000% |
| new_DeviceType    | 100.0000% |
| new_ProductCD     | 100.0000% |

## Unexplained mismatches (up to 10)

None.

Same-second rows: training counts an earlier payment of the same user in the same second, live scoring reads only `ts < now`. A replay with `Degraded` above 0 can also skip history writes, which shows up here as mismatches.
