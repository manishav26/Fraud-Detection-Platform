## Time-ordered splits

Created by `ml/prep/split_data.py`, which writes `data/processed/{train,val,stream}.parquet`.

- The transaction and identity files are joined on `TransactionID` (left join, so all
  590,540 transactions are kept; about 24% have identity data).
- `day = TransactionDT // 86400`. The data covers days 1 to 182.
- Split by whole days, never shuffled: first 40% of days = train, next 20% = validation,
  last 40% = stream (replayed in time order for the drift experiments).
- A check confirms no time overlap: max(train time) < min(val time) < min(stream time).

| Split | Days | Rows | Share of rows | Fraud rate |
|---|---|---|---|---|
| Train | 1–73 | 264,730 | 44.8% | 3.32% |
| Validation | 74–109 | 115,370 | 19.5% | 3.64% |
| Stream | 110–182 | 210,440 | 35.6% | 3.65% |

Train holds more than 40% of the rows because the early days have higher volume
(a peak around days 15–28).