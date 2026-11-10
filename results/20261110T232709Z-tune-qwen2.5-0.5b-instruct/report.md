# Tune qwen2.5-0.5b-instruct

## Rows

| quant | threads | ngl | pp tok/s | tg tok/s | GiB |
|---|---|---|---|---|---|
| q4_0 | 8 | 99 | 12414.0 | 302.4 | 0.39 |
| q4_k_m | 8 | 99 | 10923.3 | 291.5 | 0.45 |
| q8_0 | 8 | 99 | 11602.8 | 291.9 | 0.62 |

## Recommendation

tuning recommendation from 3 configurations
pareto frontier: 1 of 3 rows
interactive: q4_0 threads=8 gpu_layers=99 tg 302.4 +/- 4.0 tok/s
prompt heavy: q4_0 threads=8 gpu_layers=99 pp 12414.0 +/- 64.5 tok/s
