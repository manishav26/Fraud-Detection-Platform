# Proxy user id check

- **v1**: card1 + addr1 + start_day
- **v2**: card1 + card2 + card3 + card5 + addr1 + P_emaildomain + start_day

|                                     |         v1 |         v2 |
|:------------------------------------|-----------:|-----------:|
| number of ids                       | 217850     | 278237     |
| median txns per id                  |      1     |      1     |
| 90th pct txns per id                |      6     |      4     |
| max txns per id                     |   1414     |   1414     |
| share of ids with only 1 txn        |      0.575 |      0.652 |
| share of rows with >=3 earlier txns |      0.378 |      0.274 |
