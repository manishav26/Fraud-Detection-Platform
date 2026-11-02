# Reasons for each decision (TreeSHAP)

For every payment the model returns the top 3 features that pushed the score toward fraud, using LightGBM's exact TreeSHAP values (`pred_contrib=True`, no extra package). Only features that raised the risk are listed, largest first. Model `fraud-lgbm-v1`, test days 92-109 (60,166 payments).

## Speed (one payment at a time, 1,000 payments, laptop; budget 10 ms)

| step               |   P50 ms |   P95 ms |   P99 ms |
|:-------------------|---------:|---------:|---------:|
| score only         |     0.06 |     0.07 |     0.07 |
| reasons (TreeSHAP) |     3.26 |     3.32 |     3.38 |

Reasons P95 3.32 ms: within the 10 ms budget.

## Most common reasons (1,084 flagged payments: review or decline)

| feature       |   in top 3 (flagged) |   top reason (flagged) |
|:--------------|---------------------:|-----------------------:|
| C1            |                  658 |                    478 |
| C13           |                  461 |                     61 |
| C14           |                  412 |                    348 |
| R_emaildomain |                  262 |                      6 |
| C11           |                  194 |                     12 |
| C4            |                  193 |                      5 |
| C7            |                  182 |                      1 |
| amount        |                  164 |                     25 |
| card6         |                  116 |                     24 |
| D3            |                   94 |                     40 |

## Highest-risk examples

|   fraud probability | decision   |   really fraud | reasons                                                              |
|--------------------:|:-----------|---------------:|:---------------------------------------------------------------------|
|               1     | decline    |              1 | C13=0 (+1.39); amount=350.00 (+1.16); R_emaildomain=mail.com (+1.09) |
|               0.997 | decline    |              1 | C1=12 (+1.89); C13=0 (+1.39); C11=10 (+1.37)                         |
|               0.987 | decline    |              1 | amount=350.00 (+1.17); C13=0 (+1.05); R_emaildomain=mail.com (+1.04) |
|               0.987 | decline    |              1 | C1=72 (+2.32); C13=2 (+0.77); C6=19 (+0.76)                          |
|               0.987 | decline    |              1 | C1=201 (+2.13); C4=54 (+0.79); C7=54 (+0.61)                         |

The number after each reason is its SHAP value in log-odds: how much that feature raised the model's raw score for this payment.
