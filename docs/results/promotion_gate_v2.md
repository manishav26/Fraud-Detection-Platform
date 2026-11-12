# Promotion gate

Test days 169-182 (37,693 payments). Rules: PR-AUC at least +0.01 over the current model, score + reasons p95 at most 5.0 ms (one payment at a time, one thread), flagged share within 2%.

**KEEP the current model: safe failed.**

| model     | folder               |   PR-AUC |   flagged share |   p95 ms |
|:----------|:---------------------|---------:|----------------:|---------:|
| candidate | models/fraud-lgbm-v2 |   0.5201 |          0.0227 |   3.167  |
| current   | models/fraud-lgbm-v1 |   0.455  |          0.0169 |   3.1307 |

| check   | value          | rule      | passed   |
|:--------|:---------------|:----------|:---------|
| better  | +0.0651 PR-AUC | >= +0.01  | True     |
| fast    | 3.17 ms        | <= 5.0 ms | True     |
| safe    | 2.27% flagged  | <= 2%     | False    |
