# Profile qwen2.5-0.5b-instruct:q4_k_m

## Kernels

| kernel | calls | total s | mean ms | share |
|---|---|---|---|---|
| void mul_mat_vec_q<(ggml_type)6, 1, true, false, false>(void const*, void const* | 6957 | 0.067762 | 0.010 | 37.1% |
| void mul_mat_vec_q<(ggml_type)8, 1, false, false, false>(void const*, void const | 67 | 0.033849 | 0.505 | 18.5% |
| quantize_q8_1(float const*, void*, long, long, long, long, long, unsigned int, H | 9431 | 0.012252 | 0.001 | 6.7% |
| void mul_mat_q<(ggml_type)6, 128, false>(char const*, int const*, int const*, in | 260 | 0.011030 | 0.042 | 6.0% |
| void rms_norm_f32<256, true, false>(float const*, float*, int, long, long, long, | 3283 | 0.010152 | 0.003 | 5.6% |
| void flash_attn_tile<64, 64, 2, 1, false>(char const*, char const*, char const*, | 1560 | 0.007216 | 0.005 | 4.0% |
| void mul_mat_vec_q<(ggml_type)14, 1, true, false, false>(void const*, void const | 780 | 0.007075 | 0.009 | 3.9% |
| void mul_mat_vec_q<(ggml_type)12, 1, true, false, false>(void const*, void const | 782 | 0.005256 | 0.007 | 2.9% |
| void rope_neox<true, false, float, __half>(float const*, __half*, int, int, int, | 1608 | 0.004509 | 0.003 | 2.5% |
| void rope_neox<true, false, float, float>(float const*, float*, int, int, int, i | 1608 | 0.003926 | 0.002 | 2.2% |
| void flash_attn_combine_results<64>(float const*, HIP_vector_type<float, 2u> con | 1560 | 0.003119 | 0.002 | 1.7% |
| void k_set_rows<float, long, __half>(float const*, long const*, __half*, long, l | 1608 | 0.002250 | 0.001 | 1.2% |
| void mul_mat_q<(ggml_type)12, 128, false>(char const*, int const*, int const*, i | 22 | 0.002080 | 0.095 | 1.1% |
| void flash_attn_ext_f16<64, 64, 8, 8, false, false, false>(char const*, char con | 48 | 0.001772 | 0.037 | 1.0% |
| void mul_mat_vec_q<(ggml_type)8, 1, true, false, false>(void const*, void const* | 780 | 0.001498 | 0.002 | 0.8% |
| __amd_rocclr_copyBuffer | 519 | 0.001429 | 0.003 | 0.8% |
| Cijk_Alik_Bljk_HB_MT64x64x32_MI16x16x16x1_SN_1LDSB1_APM1_ABV0_ACED0_AF0EM1_AF1EM | 24 | 0.001155 | 0.048 | 0.6% |
| void k_bin_bcast<&(op_add(float, float)), float, float, float, float const*>(flo | 303 | 0.000953 | 0.003 | 0.5% |
| void unary_gated_op_kernel<&(op_silu(float)), float>(float const*, float const*, | 46 | 0.000820 | 0.018 | 0.4% |
| __amd_rocclr_fillBufferAligned | 328 | 0.000812 | 0.002 | 0.4% |
| void quantize_mmq_q8_1<(mmq_q8_1_ds_layout)0, false>(float const*, int const*, v | 284 | 0.000781 | 0.003 | 0.4% |
| void flash_attn_stream_k_fixup_general<64, 8, 8>(float*, HIP_vector_type<float,  | 48 | 0.000684 | 0.014 | 0.4% |
| void mul_mat_q<(ggml_type)8, 128, false>(char const*, int const*, int const*, in | 24 | 0.000498 | 0.021 | 0.3% |
| void convert_unary<float, __half>(void const*, __half*, long, long, long, HIP_ve | 24 | 0.000442 | 0.018 | 0.2% |
| void dequantize_block_q6_K<__half>(void const*, __half*) | 24 | 0.000407 | 0.017 | 0.2% |
| void mul_mat_vec_q<(ggml_type)6, 1, false, false, false>(void const*, void const | 65 | 0.000296 | 0.005 | 0.2% |
| void k_get_rows_float<float, float>(float const*, int const*, float*, long, long | 134 | 0.000222 | 0.002 | 0.1% |
| void quantize_mmq_q8_1<(mmq_q8_1_ds_layout)1, false>(float const*, int const*, v | 22 | 0.000193 | 0.009 | 0.1% |
| void convert_unary<__half, float>(void const*, float*, long, long, long, HIP_vec | 24 | 0.000110 | 0.005 | 0.1% |

## Trace

.sandbox/results/20261114T210229Z-profile-qwen2.5-0.5b-instruct-q4_k_m/trace/trace_kernel_trace.csv
