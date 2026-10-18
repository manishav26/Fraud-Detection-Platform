# Data

The data files are **not** in this repo (Kaggle rules and GitHub size limits).
Each person downloads them and recreates the processed files with the scripts below.

## Dataset: IEEE-CIS Fraud Detection

- Source: https://www.kaggle.com/c/ieee-fraud-detection
- Files used: `train_transaction.csv`, `train_identity.csv`
  (the Kaggle test files have no fraud labels, so they are not used)
- Size: 590,540 transactions, 394 columns, 3.5% fraud, 182 days

## How to use

### 1. Download the data

1. Sign in to Kaggle and accept the competition rules on the link above.
2. Download `ieee-fraud-detection.zip` from the competition's Data tab.

### 2. Put the files in place

From the repo folder:

```bash
mkdir -p data/raw/ieee-cis
unzip ~/Downloads/ieee-fraud-detection.zip train_transaction.csv train_identity.csv -d data/raw/ieee-cis
```

### 3. Install the Python packages

```bash
pip install pandas pyarrow numpy matplotlib jupyter scikit-learn lightgbm tabulate
```

### 4. Create the time-ordered splits

```bash
python ml/prep/split_data.py
```

This takes about a minute and writes:

```
data/processed/train.parquet
data/processed/val.parquet
data/processed/stream.parquet
```

Load them in code with:

```python
import pandas as pd
train = pd.read_parquet("data/processed/train.parquet")
```

### 5. Check the proxy user id

```bash
python ml/prep/proxy_id.py
```

This prints statistics for two proxy user id definitions and saves them to
`docs/results/proxy_id.md`.

## Folder layout (local only, ignored by git)

```
data/
├── README.md          this file (the only file committed)
├── raw/ieee-cis/      original Kaggle CSVs, never edited
└── processed/         files created by the scripts
```

## Time-ordered splits

- The transaction and identity files are joined on `TransactionID` (left join, so every
  transaction is kept; about 24% have identity data).
- `day = TransactionDT // 86400`, which gives days 1 to 182.
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

## Proxy user id

### Why we need it

Many strong fraud signals depend on a customer's own history: "6 purchases in the last hour",
"this amount is 10 times their usual", "this device is new for them". To compute these, we must
know which transactions belong to the same customer. IEEE-CIS removed all customer and card
identifiers for privacy, so every row is a standalone transaction.

### What it is

A **proxy user id** is a stand-in customer id built from columns that, together, stay the same
for one card. Transactions that share the same value are treated as the same customer. It is an
approximation, not a real id: two different cards can occasionally merge into one id, and one
card can occasionally split into two. This approach follows the winning solution of the IEEE-CIS
Kaggle competition.

### How it is built

| Column | Meaning | Why it helps |
|---|---|---|
| `card1` | Coded partial card identifier | Narrows down the card, but many cards share it |
| `addr1` | Coded billing region | Narrows down the location |
| card start day | `floor(TransactionDT / 86400 - D1)` | `D1` is days since the card was first used. Transaction day minus D1 is the day the card started, which stays constant for every transaction of that card |

Example: three transactions with `card1 = 9500`, `addr1 = 204` and start day `12` all get the id
`9500|204|12`, so they are treated as one customer. D1 is missing for only 0.2% of rows.

### How we measured it

`ml/prep/proxy_id.py` builds two versions and reports:

| Measure | What it tells us |
|---|---|
| Number of ids | How many "customers" the id finds |
| Transactions per id | Real cardholders usually have a handful to a few dozen |
| Ids with only one transaction | If very high, the id may be too strict and split customers apart |
| Max transactions per id | If very large, the id may be merging different customers |
| Rows with at least 3 earlier transactions | **Key measure:** how many transactions have enough history for per-user features to be useful |

### Results

| | v1: card1 + addr1 + start day | v2: v1 + card2, card3, card5, email domain |
|---|---|---|
| Number of ids | 217,850 | 278,237 |
| Median transactions per id | 1 | 1 |
| 90th percentile transactions per id | 6 | 4 |
| Ids with only one transaction | 57.5% | 65.2% |
| Rows with at least 3 earlier transactions | 37.8% | 27.4% |

### What it means

- **We use v1.** It gives per-user history for 38% of transactions. v2 adds more columns, which
  splits the same customer into several ids and leaves only 27% of transactions with history.
- **The proxy is moderate, not weak.** It is good enough for the main plan. Experiment E2 will
  measure how much per-user features actually improve the model.
- **Most ids appear once (median of 1).** IEEE-CIS is a sample of one company's transactions over
  six months, so many cards appear only once. These transactions use default history values
  (a "cold start").
- **A few ids are very large.** The biggest has 1,414 transactions (0.24% of the data), possibly
  a heavy card or several cards sharing the same values. This is small enough not to distort the
  model.
- **Negative start days are normal.** They mean the card was first used before the dataset begins.
- **Ids containing missing values** (for example a missing `addr1`) are treated as unknown users
  rather than one large customer.
- In the live platform, the proxy id is sent as `user_id` and is hashed before storage, so no raw
  identifiers are kept.

  ### Missing values in the id

Some ids contain `nan`, for example `12616|nan|-491.0`. This means one of the parts was missing
for those transactions, usually `addr1` (the billing region). The dataset does not explain why;
likely reasons are that the address was not collected for some purchase or product types, or
did not fit the region coding. When the id is built, a missing value becomes the text `"nan"`,
so different customers who share the same `card1` and start day but have no address can be
grouped into one id by mistake.

How we handle it: transactions whose id contains a missing part are treated as **unknown users**
(cold start, default history values) instead of being merged into one large customer. 