# Eval qwen2.5-7b-instruct:q4_k_m

## Cases

| id | ok | text |
|---|---|---|
| repeat-blue-heron | pass | BLUE HERON |
| repeat-number-sequence | pass | 7 13 21 34 |
| add-37-48 | pass | 85 |
| add-63-29 | pass | 92 |
| subtract-52-27 | pass | 25 |
| subtract-71-38 | pass | 33 |
| json-sum | pass | {"sum": 42} |
| json-status | pass | {"status": "ok"} |
| json-identity | fail | ```json { "name": "blinding", "version": "0.1.0" } ``` |
| json-two-keys | pass | {"key1": "value1", "key2": "value2"} |
| contains-kitten | pass | kitten |
| contains-atlas | pass | The explorer used the atlas to plan his journey through unfa |
| contains-hello | pass | hello |
| regex-three-letters | pass | ABC |
| regex-ready-period | pass | ready. |
| refusal-format | pass | I cannot predict tomorrow's lottery numbers as it goes again |

## Score

passed 15 of 16 (93.8%, Wilson 71.7% to 98.9%)
