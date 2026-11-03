# Calibration and decision thresholds (week 2, steps 5-6)

LightGBM trained on days 1-73 (65 features, raw D). Isotonic calibration fitted on days 74-91; everything below is measured on days 92-109.

Actual fraud rate on days 92-109: 0.0351

## Calibration quality

| scores       |   Brier score |   mean predicted |   PR-AUC |
|:-------------|--------------:|-----------------:|---------:|
| raw LightGBM |        0.0232 |           0.0226 |   0.5167 |
| calibrated   |        0.0227 |           0.0326 |   0.4966 |

![reliability](../figures/reliability.png)

## Review threshold by assumed review cost

Chosen on days 74-91 to minimise missed fraud dollars + review cost per flagged payment; evaluated on days 92-109.

|   review cost ($) |   review threshold |   flagged (test) | share flagged   |   frauds caught (test) |   frauds total (test) |   expected cost (test, $) |
|------------------:|-------------------:|-----------------:|:----------------|-----------------------:|----------------------:|--------------------------:|
|                 2 |               0.55 |              771 | 1.3%            |                    643 |                  2110 |                    298712 |
|                 5 |               0.55 |              771 | 1.3%            |                    643 |                  2110 |                    301025 |
|                10 |               0.55 |              771 | 1.3%            |                    643 |                  2110 |                    304880 |

## Proposed decision rule (review cost $5)

approve below 0.55, review from 0.55 to 0.7, decline at 0.7 or above (calibrated probabilities).

| decision   |   fraud |   normal |
|:-----------|--------:|---------:|
| approve    |    1467 |    57928 |
| review     |      73 |       45 |
| decline    |     570 |       83 |
