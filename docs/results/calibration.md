# Calibration and decision thresholds (week 2, steps 5-6)

LightGBM trained on days 1-73 (65 features, raw D). Isotonic calibration fitted on days 74-91; everything below is measured on days 92-109.

Actual fraud rate on days 92-109: 0.0351

## Calibration quality

| scores       |   Brier score |   mean predicted |   PR-AUC |
|:-------------|--------------:|-----------------:|---------:|
| raw LightGBM |        0.0232 |           0.0226 |   0.5167 |
| calibrated   |        0.0227 |           0.0326 |   0.4966 |

![reliability](../figures/reliability.png)

## Review threshold: capacity rule vs cost rule

Chosen on days 74-91: the lowest threshold that keeps flagged payments (review + decline) within 2%, since analysts are a fixed team and unused capacity is wasted. The cost rows show the cheapest threshold (missed fraud dollars + a cost per review) within the same cap, for comparison. All outcomes measured on days 92-109.

| rule                       |   review threshold | flagged (days 74-91)   |   flagged (test) | share flagged (test)   |   frauds caught (test) |   frauds total (test) |
|:---------------------------|-------------------:|:-----------------------|-----------------:|:-----------------------|-----------------------:|----------------------:|
| capacity <= 2% (chosen)    |               0.55 | 1.66%                  |              771 | 1.28%                  |                    643 |                  2110 |
| cheapest at $2 per review  |               0.55 | 1.66%                  |              771 | 1.28%                  |                    643 |                  2110 |
| cheapest at $5 per review  |               0.55 | 1.66%                  |              771 | 1.28%                  |                    643 |                  2110 |
| cheapest at $10 per review |               0.55 | 1.66%                  |              771 | 1.28%                  |                    643 |                  2110 |

## Proposed decision rule (capacity 2%)

approve below 0.55, review from 0.55 to 0.7, decline at 0.7 or above (calibrated probabilities).

| decision   |   fraud |   normal |
|:-----------|--------:|---------:|
| approve    |    1467 |    57928 |
| review     |      73 |       45 |
| decline    |     570 |       83 |
