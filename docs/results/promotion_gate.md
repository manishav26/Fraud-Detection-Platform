# Promotion gate

Golden set: test days 92-109 (60,166 payments). Rules: PR-AUC at least +0.01 over the current model, score + reasons p95 at most 5.0 ms (one payment at a time, one thread), flagged share within 2%.

**KEEP the current model: better failed.**

| model     | folder                     |   PR-AUC |   flagged share |   p95 ms |
|:----------|:---------------------------|---------:|----------------:|---------:|
| candidate | models/fraud-lgbm-v1-tuned |   0.4966 |          0.0128 |   3.0408 |
| current   | models/fraud-lgbm-v1       |   0.4955 |          0.018  |   3.593  |

| check   | value          | rule      | passed   |
|:--------|:---------------|:----------|:---------|
| better  | +0.0011 PR-AUC | >= +0.01  | False    |
| fast    | 3.04 ms        | <= 5.0 ms | True     |
| safe    | 1.28% flagged  | <= 2%     | True     |
