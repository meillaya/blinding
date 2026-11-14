# HIP kernel benchmark

## Rows

| kernel | detail | value |
|---|---|---|
| copy vec4-i1-w4 | block 128 | 305.1 GB/s (48.9% peak) |
| copy vec4-i1-w4 | block 256 | 472.7 GB/s (75.7% peak) |
| copy vec4-i1-w4 | block 512 | 475.4 GB/s (76.2% peak) |
| copy vec4-i1-w4 | block 1024 | 463.4 GB/s (74.3% peak) |
| gemm fp16 | nn 1024 | 34376.9 GFLOPS (46.1% peak) |
| gemm fp16 | nt 1024 | 27291.3 GFLOPS (36.6% peak) |
| gemm fp16 | tn 1024 | 23976.5 GFLOPS (32.1% peak) |
| gemm fp16 | nn 2048 | 39318.7 GFLOPS (52.7% peak) |
| gemm fp16 | nt 2048 | 52720.9 GFLOPS (70.7% peak) |
| gemm fp16 | tn 2048 | 13974.6 GFLOPS (18.7% peak) |
| gemm fp16 | nn 4096 | 59240.5 GFLOPS (79.4% peak) |
| gemm fp16 | nt 4096 | 55901.7 GFLOPS (74.9% peak) |
| gemm fp16 | tn 4096 | 32391.4 GFLOPS (43.4% peak) |
| gemm fp32 | nn 1024 | 8183.1 GFLOPS (21.9% peak) |
| gemm fp32 | nt 1024 | 10083.9 GFLOPS (27.0% peak) |
| gemm fp32 | tn 1024 | 7435.4 GFLOPS (19.9% peak) |
| gemm fp32 | nn 2048 | 9807.7 GFLOPS (26.3% peak) |
| gemm fp32 | nt 2048 | 11900.1 GFLOPS (31.9% peak) |
| gemm fp32 | tn 2048 | 8742.7 GFLOPS (23.4% peak) |
| gemm fp32 | nn 4096 | 10235.1 GFLOPS (27.4% peak) |
| gemm fp32 | nt 4096 | 12935.5 GFLOPS (34.7% peak) |
| gemm fp32 | tn 4096 | 8452.3 GFLOPS (22.7% peak) |
