# Drift on the stream days

Reference: training days 1-73 (264,730 payments). Compared: stream days 110-182 (210,440 payments), the period the live system replays. Model `fraud-lgbm-v1`. PSI levels: < 0.1 stable, 0.1-0.2 moderate, > 0.2 major.

## Features: whole stream vs training (8 major, 5 moderate out of 65)

| feature         |    PSI | level    |   KS statistic |   KS p-value |
|:----------------|-------:|:---------|---------------:|-------------:|
| M8              | 0.6468 | major    |       nan      |          nan |
| M9              | 0.6467 | major    |       nan      |          nan |
| M7              | 0.6466 | major    |       nan      |          nan |
| M3              | 0.5228 | major    |       nan      |          nan |
| M2              | 0.5227 | major    |       nan      |          nan |
| M1              | 0.5226 | major    |       nan      |          nan |
| D11             | 0.5135 | major    |         0.0985 |            0 |
| n_prev_txns     | 0.21   | major    |         0.2039 |            0 |
| secs_since_last | 0.1653 | moderate |         0.1576 |            0 |
| ProductCD       | 0.1433 | moderate |       nan      |          nan |
| C9              | 0.1397 | moderate |         0.1707 |            0 |
| D10             | 0.1341 | moderate |         0.0788 |            0 |
| D15             | 0.119  | moderate |         0.1168 |            0 |
| D4              | 0.071  | stable   |         0.0888 |            0 |
| C12             | 0.0675 | stable   |         0.0975 |            0 |

KS is only for numeric features. With this many payments almost any difference has a tiny p-value, so the KS statistic (size of the gap) and PSI are the useful numbers.

## Week by week

|   week | days    |   payments |   features moderate |   features major | worst feature   |   score PSI |   fraud rate |   fraud flagged |
|-------:|:--------|-----------:|--------------------:|-----------------:|:----------------|------------:|-------------:|----------------:|
|      1 | 110-116 |      20980 |                   6 |                8 | M9 (0.66)       |      0.0117 |       0.0463 |          0.4336 |
|      2 | 117-123 |      22376 |                   6 |                8 | M9 (0.67)       |      0.015  |       0.0425 |          0.4579 |
|      3 | 124-130 |      21717 |                   9 |                9 | M9 (0.79)       |      0.0177 |       0.033  |          0.4324 |
|      4 | 131-137 |      18589 |                   5 |                8 | M7 (0.55)       |      0.0138 |       0.0368 |          0.3085 |
|      5 | 138-144 |      18417 |                   6 |                8 | M3 (0.59)       |      0.014  |       0.0364 |          0.313  |
|      6 | 145-151 |      19035 |                   5 |                8 | M8 (0.67)       |      0.0205 |       0.0296 |          0.3014 |
|      7 | 152-158 |      22018 |                   7 |               11 | M9 (0.81)       |      0.0282 |       0.0301 |          0.3937 |
|      8 | 159-165 |      20716 |                   2 |               11 | M7 (0.55)       |      0.0188 |       0.031  |          0.3988 |
|      9 | 166-172 |      20454 |                   3 |                9 | M9 (0.57)       |      0.0175 |       0.0354 |          0.3434 |
|     10 | 173-179 |      18027 |                   5 |                9 | M9 (0.74)       |      0.0193 |       0.0413 |          0.4295 |
|     11 | 180-182 |       8111 |                   5 |                9 | M9 (0.83)       |      0.0343 |       0.0418 |          0.3923 |

`fraud flagged` = share of real fraud sent to review or decline that week.

## ADWIN (streaming)

| Stream watched | Needs labels | Drift detected on days |
|---|---|---|
| model score | no | 116 |
| model error \|label - score\| | yes, 14 days late | none |
| missed fraud (1 per real fraud) | yes, 14 days late | 129, 140, 167, 188 |

Error alarms are reported on the day the label arrives (payment day + 14), which is when a live system could raise them.
