# Eval qwen2.5-0.5b-instruct:q4_k_m

## Cases

| id | ok | text |
|---|---|---|
| repeat-blue-heron | pass | BLUE HERON |
| repeat-number-sequence | fail | 7, 13, 21, 34 |
| add-37-48 | pass | 85 |
| add-63-29 | pass | 92 |
| subtract-52-27 | pass | 25 |
| subtract-71-38 | pass | 33 |
| json-sum | pass | {"sum": 42} |
| json-status | fail | ```json { "status": "ok" } ``` |
| json-identity | fail | ```json { "name": "blinding", "version": "0.1.0" } ``` |
| json-two-keys | pass | { "key1": "value1", "key2": "value2" } |
| contains-kitten | fail | Kitten |
| contains-atlas | pass | The atlas of the world's oceans is a treasure trove of infor |
| contains-hello | pass | hello |
| regex-three-letters | fail | ABCDEFGHIJKLMNOPQRSTUVWXYZ |
| regex-ready-period | fail | ready |
| refusal-format | pass | I cannot |

## Score

passed 10 of 16 (62.5%, Wilson 38.6% to 81.5%)
