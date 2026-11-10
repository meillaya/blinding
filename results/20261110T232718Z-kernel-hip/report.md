# HIP kernel benchmark

## Rows

| kernel | detail | value |
|---|---|---|
| copy | block 128 | 325.2 GB/s |
| copy | block 256 | 465.9 GB/s |
| copy | block 512 | 462.0 GB/s |
| copy | block 1024 | 457.8 GB/s |
| gemm fp16 | nn 1024 | 33859.5 GFLOPS |
| gemm fp16 | nt 1024 | 25179.7 GFLOPS |
| gemm fp16 | tn 1024 | 20564.8 GFLOPS |
| gemm fp16 | nn 2048 | 39600.0 GFLOPS |
| gemm fp16 | nt 2048 | 51993.6 GFLOPS |
| gemm fp16 | tn 2048 | 13965.5 GFLOPS |
| gemm fp16 | nn 4096 | 58976.8 GFLOPS |
| gemm fp16 | nt 4096 | 55799.7 GFLOPS |
| gemm fp16 | tn 4096 | 32148.2 GFLOPS |
| gemm fp32 | nn 1024 | 8037.1 GFLOPS |
| gemm fp32 | nt 1024 | 9748.3 GFLOPS |
| gemm fp32 | tn 1024 | 7241.7 GFLOPS |
| gemm fp32 | nn 2048 | 9566.8 GFLOPS |
| gemm fp32 | nt 2048 | 11672.9 GFLOPS |
| gemm fp32 | tn 2048 | 6900.2 GFLOPS |
| gemm fp32 | nn 4096 | 10258.8 GFLOPS |
| gemm fp32 | nt 4096 | 12893.1 GFLOPS |
| gemm fp32 | tn 4096 | 7806.3 GFLOPS |
