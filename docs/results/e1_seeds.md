# E1 with 3 seeds: model choice and raw vs normalized D

65 features (request-time + customer history), trained on days 1-73, scored on days 74-109 (4,197 frauds in 115,370 payments). Tree models use row/column sampling 0.8 and seeds 1, 2, 3; logistic regression has no randomness and runs once. Normalized D = transaction day - D for D1, D2, D3, D4, D5, D6, D7, D10, D11, D12, D13, D14, D15.

## Summary (mean and standard deviation over seeds)

| model               | D columns   |   runs |   PR-AUC mean |   PR-AUC std |   recall @ 1% FPR mean |   recall @ 1% FPR std |
|:--------------------|:------------|-------:|--------------:|-------------:|-----------------------:|----------------------:|
| LightGBM            | raw         |      3 |        0.5682 |       0.0018 |                 0.473  |                0.0017 |
| XGBoost             | raw         |      3 |        0.5644 |       0.0032 |                 0.4698 |                0.0031 |
| Logistic regression | raw         |      1 |        0.2263 |       0      |                 0.1706 |                0      |
| LightGBM            | normalized  |      3 |        0.5825 |       0.0024 |                 0.4885 |                0.0013 |
| XGBoost             | normalized  |      3 |        0.5846 |       0.0013 |                 0.4908 |                0.001  |
| Logistic regression | normalized  |      1 |        0.22   |       0      |                 0.1613 |                0      |

## Every run

| model               | D columns   | seed   |   PR-AUC |   recall @ 1% FPR |   minutes |
|:--------------------|:------------|:-------|---------:|------------------:|----------:|
| LightGBM            | raw         | 1      |   0.5686 |            0.4739 |    0.0832 |
| XGBoost             | raw         | 1      |   0.5673 |            0.4708 |    0.0998 |
| LightGBM            | raw         | 2      |   0.5662 |            0.4711 |    0.0814 |
| XGBoost             | raw         | 2      |   0.565  |            0.4663 |    0.0982 |
| LightGBM            | raw         | 3      |   0.5697 |            0.4739 |    0.0808 |
| XGBoost             | raw         | 3      |   0.561  |            0.4722 |    0.0977 |
| Logistic regression | raw         | -      |   0.2263 |            0.1706 |    0.3014 |
| LightGBM            | normalized  | 1      |   0.5834 |            0.4884 |    0.0812 |
| XGBoost             | normalized  | 1      |   0.586  |            0.4899 |    0.0985 |
| LightGBM            | normalized  | 2      |   0.5798 |            0.4873 |    0.0807 |
| XGBoost             | normalized  | 2      |   0.5845 |            0.4918 |    0.0999 |
| LightGBM            | normalized  | 3      |   0.5845 |            0.4899 |    0.081  |
| XGBoost             | normalized  | 3      |   0.5833 |            0.4908 |    0.0988 |
| Logistic regression | normalized  | -      |   0.22   |            0.1613 |    0.3059 |



## Decision

Keep LightGBM with raw D columns for v1.

- LightGBM and XGBoost are tied (difference within one seed standard deviation), so we keep
  LightGBM, which is already packaged, pinned and served.
- Normalized D gives a small but consistent gain: +0.014 PR-AUC and +1.5 points recall at
  1% FPR (about 65 more frauds out of 4,197), better in all 6 runs. It needs a serving change
  (day number from `ts`), a schema update and a new model, so it is left as a planned v2
  improvement rather than changed before Gate 2.
- Logistic regression is far behind (PR-AUC about 0.22), so tree models are needed.
