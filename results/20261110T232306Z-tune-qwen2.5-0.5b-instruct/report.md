# Tune qwen2.5-0.5b-instruct

## Rows

| quant | threads | ngl | pp tok/s | tg tok/s | GiB |
|---|---|---|---|---|---|
| q4_0 | 8 | 0 | 734.8 | 91.0 | 0.39 |
| q4_0 | 16 | 0 | 363.4 | 15.6 | 0.39 |
| q4_k_m | 8 | 0 | 418.9 | 85.2 | 0.45 |
| q4_k_m | 16 | 0 | 266.7 | 13.8 | 0.45 |
| q8_0 | 8 | 0 | 506.2 | 59.7 | 0.62 |
| q8_0 | 16 | 0 | 155.0 | 7.9 | 0.62 |

## Recommendation

tuning recommendation from 6 configurations
pareto frontier: 1 of 6 rows
interactive: q4_0 threads=8 gpu_layers=0 tg 91.0 +/- 0.2 tok/s
prompt heavy: q4_0 threads=8 gpu_layers=0 pp 734.8 +/- 12.4 tok/s
