# Reasons for each decision (TreeSHAP)

For every payment the model returns the top 3 features that pushed the score toward fraud, using LightGBM's exact TreeSHAP values (`pred_contrib=True`, no extra package). Only features that raised the risk are listed, largest first. Model `fraud-lgbm-v1`, test days 92-109 (60,166 payments).

## Speed (one payment at a time, 1,000 payments, laptop; budget 10 ms)

| step               |   P50 ms |   P95 ms |   P99 ms |
|:-------------------|---------:|---------:|---------:|
| score only         |     0.1  |     0.1  |     0.11 |
| reasons (TreeSHAP) |     2.84 |     2.89 |     2.92 |

Reasons P95 2.89 ms: within the 10 ms budget.

## Most common reasons (771 flagged payments: review or decline)

| feature       |   in top 3 (flagged) |   top reason (flagged) |
|:--------------|---------------------:|-----------------------:|
| C1            |                  546 |                    398 |
| C14           |                  306 |                    257 |
| C13           |                  305 |                     17 |
| C4            |                  298 |                      7 |
| C11           |                  270 |                      5 |
| amount        |                  113 |                     31 |
| R_emaildomain |                   98 |                      5 |
| D8            |                   76 |                     22 |
| D14           |                   48 |                      1 |
| C6            |                   39 |                      5 |

## Highest-risk examples

|   fraud probability | decision   |   really fraud | reasons                                             |
|--------------------:|:-----------|---------------:|:----------------------------------------------------|
|               0.983 | decline    |              1 | C1=195 (+1.70); C4=54 (+1.30); C11=146 (+1.02)      |
|               0.983 | decline    |              1 | C1=81 (+2.04); C4=17 (+1.35); C11=18 (+0.97)        |
|               0.983 | decline    |              1 | C1=198 (+1.79); C4=54 (+1.39); C11=149 (+1.06)      |
|               0.983 | decline    |              1 | C14=0 (+1.89); C1=5 (+1.60); D14=2 (+0.86)          |
|               0.983 | decline    |              1 | C1=75 (+1.56); C4=23 (+1.28); amount=300.00 (+1.22) |

The number after each reason is its SHAP value in log-odds: how much that feature raised the model's raw score for this payment.
