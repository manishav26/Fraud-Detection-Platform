# E2 extra: normalized D columns

D_norm = transaction day - D for D1, D2, D3, D4, D5, D6, D7, D10, D11, D12, D13, D14, D15 (D8, D9 kept raw). Trained on days 1-73, evaluated on days 74-109.

| variant                   |   features |   PR-AUC |   recall @ 1% FPR |   frauds caught @ 1% FPR |   frauds total |
|:--------------------------|-----------:|---------:|------------------:|-------------------------:|---------------:|
| 1. raw D (baseline)       |         53 |   0.5568 |            0.457  |                     1919 |           4197 |
| 2. normalized D           |         53 |   0.5713 |            0.4932 |                     2070 |           4197 |
| 3. normalized D + history |         65 |   0.5742 |            0.4801 |                     2015 |           4197 |

## Top 15 features (variant 3, split count)

|              |   importance |
|:-------------|-------------:|
| card1        |         1722 |
| card2        |         1558 |
| D1_norm      |         1548 |
| log_amount   |         1438 |
| addr1        |         1413 |
| D15_norm     |         1284 |
| D10_norm     |         1054 |
| D2_norm      |         1017 |
| D4_norm      |          922 |
| card5        |          709 |
| C13          |          693 |
| D8           |          659 |
| hour         |          609 |
| dist1        |          541 |
| amt_mean_30d |          538 |
