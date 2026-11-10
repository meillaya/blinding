/* HIP copy and rocBLAS GEMM benchmark for the Blinding lab.
 *
 * The report needs two facts from the GPU: how fast it moves bytes, and how
 * fast it multiplies matrices. Both come out as one JSON object on stdout.
 * Diagnostics go to stderr, and any HIP or rocBLAS failure exits non-zero
 * without printing a partial object, so a broken run cannot be mistaken for a
 * slow one.
 *
 * Build inside the ROCm image:
 *
 *   hipcc -O3 -std=c++17 -o /usr/local/bin/blinding-hip-bench \
 *       kernel/hip_bench.cpp -lrocblas
 *
 * Clocks and power are not read here. The HIP runtime exposes no stable clock
 * API, and shelling out to rocm-smi from inside a timed benchmark would put
 * other work on the machine being measured. The clocks object stays null; the
 * run record carries the device facts the Python layer adds.
 */

#include <algorithm>
#include <cmath>
#include <cstddef>
#include <cstdint>
#include <cstdlib>
#include <cstring>
#include <exception>
#include <fstream>
#include <iomanip>
#include <iostream>
#include <sstream>
#include <string>
#include <type_traits>
#include <vector>

#include <hip/hip_fp16.h>
#include <hip/hip_runtime.h>
#include <rocblas/rocblas.h>

namespace {

constexpr int kExitRuntimeError = 1;
constexpr int kExitUsage = 2;

// rocBLAS selects and caches a kernel for a shape on the first launch. Three
// warmup launches take that hit before timing starts. Seven timed launches
// average out the launch-to-launch jitter a clock change introduces.
constexpr int kWarmupIterations = 3;
constexpr int kTimedIterations = 7;

// One copy iteration moves 256 MiB: 128 MiB read from the source buffer and
// 128 MiB written to the destination. The working set has to clear the last
// level cache by a wide margin, otherwise the number describes cache
// bandwidth, which is not what a model read reaches.
constexpr std::size_t kCopyMovedBytes = 256ull * 1024ull * 1024ull;
constexpr std::size_t kCopyBufferBytes = kCopyMovedBytes / 2;

// Keep several waves resident per compute unit. One wave leaves the memory
// system idle while the last workgroup retires, which drags the average down.
constexpr int kCopyWavesPerCu = 4;

constexpr int kMaxBlockSize = 1024;
constexpr int kMaxProblemSize = 16384;
constexpr int kFillBlock = 256;
constexpr int kFillGridCap = 65535;
constexpr double kGiga = 1.0e9;
constexpr const char* kProgramName = "blinding-hip-bench";

[[noreturn]] void fail(const std::string& message, int exit_code = kExitRuntimeError) {
    std::cerr << kProgramName << ": " << message << "\n";
    std::exit(exit_code);
}

void check_hip(hipError_t status, const char* call) {
    if (status != hipSuccess) {
        fail(std::string("HIP error in ") + call + ": " + hipGetErrorString(status));
    }
}

#define HIP_CHECK(call) check_hip((call), #call)

const char* rocblas_status_name(rocblas_status status) {
    switch (status) {
        case rocblas_status_success:
            return "success";
        case rocblas_status_invalid_handle:
            return "invalid handle";
        case rocblas_status_not_implemented:
            return "not implemented";
        case rocblas_status_invalid_pointer:
            return "invalid pointer";
        case rocblas_status_invalid_size:
            return "invalid size";
        case rocblas_status_memory_error:
            return "memory error";
        case rocblas_status_internal_error:
            return "internal error";
        case rocblas_status_perf_degraded:
            return "performance degraded";
        case rocblas_status_invalid_value:
            return "invalid value";
        case rocblas_status_arch_mismatch:
            return "architecture mismatch";
        default:
            return "unlisted status code";
    }
}

void check_rocblas(rocblas_status status) {
    if (status != rocblas_status_success) {
        fail(std::string("rocBLAS error: ") + rocblas_status_name(status));
    }
}

std::string trim(const std::string& value) {
    const std::size_t begin = value.find_first_not_of(" \t\r\n");
    if (begin == std::string::npos) {
        return "";
    }
    const std::size_t end = value.find_last_not_of(" \t\r\n");
    return value.substr(begin, end - begin + 1);
}

std::string lowercase(std::string value) {
    for (char& character : value) {
        if (character >= 'A' && character <= 'Z') {
            character = static_cast<char>(character - 'A' + 'a');
        }
    }
    return value;
}

std::string json_string(const std::string& value) {
    std::ostringstream out;
    out << '"';
    for (const char character : value) {
        switch (character) {
            case '"':
                out << "\\\"";
                break;
            case '\\':
                out << "\\\\";
                break;
            case '\n':
                out << "\\n";
                break;
            case '\r':
                out << "\\r";
                break;
            case '\t':
                out << "\\t";
                break;
            default:
                if (static_cast<unsigned char>(character) < 0x20) {
                    out << "\\u" << std::hex << std::setw(4) << std::setfill('0')
                        << static_cast<int>(static_cast<unsigned char>(character)) << std::dec
                        << std::setfill(' ');
                } else {
                    out << character;
                }
        }
    }
    out << '"';
    return out.str();
}

std::string json_number(double value) {
    std::ostringstream out;
    out << std::fixed << std::setprecision(3) << value;
    return out.str();
}

std::string format_version(int version) {
    // HIP_VERSION is major * 10000000 + minor * 100000 + patch. The runtime
    // reports the same encoding, so a build without hip_version.h can still
    // print a readable version.
    std::ostringstream out;
    out << version / 10000000 << "." << (version / 100000) % 100 << "." << version % 100000;
    return out.str();
}

std::string read_first_line(const char* path) {
    std::ifstream stream(path);
    if (!stream) {
        return "";
    }
    std::string line;
    std::getline(stream, line);
    return trim(line);
}

std::size_t ceil_div(std::size_t value, std::size_t divisor) {
    return (value + divisor - 1) / divisor;
}

std::vector<std::string> split_list(const std::string& text) {
    std::vector<std::string> items;
    std::size_t start = 0;
    while (start <= text.size()) {
        const std::size_t comma = text.find(',', start);
        const std::size_t length =
            comma == std::string::npos ? std::string::npos : comma - start;
        const std::string item = trim(text.substr(start, length));
        if (!item.empty()) {
            items.push_back(item);
        }
        if (comma == std::string::npos) {
            break;
        }
        start = comma + 1;
    }
    return items;
}

int parse_int(const std::string& text, const std::string& flag) {
    try {
        std::size_t consumed = 0;
        const int value = std::stoi(text, &consumed);
        if (consumed == text.size()) {
            return value;
        }
    } catch (const std::exception&) {
    }
    fail("invalid integer for " + flag + ": " + text, kExitUsage);
}

std::string next_value(int argc, char** argv, int& index, const std::string& flag) {
    if (index + 1 >= argc) {
        fail(flag + " needs a value", kExitUsage);
    }
    ++index;
    return argv[index];
}

struct Options {
    std::vector<int> blocks{128, 256, 512, 1024};
    std::vector<int> sizes{1024, 2048, 4096};
    std::vector<std::string> dtypes{"fp32", "fp16"};
    std::vector<std::string> layouts{"nn", "nt", "tn"};
};

void print_usage(std::ostream& stream) {
    stream << "usage: " << kProgramName << " [--json] [--blocks 128,256,512,1024]\n"
           << "       [--sizes 1024,2048,4096] [--dtype fp16,fp32] [--layouts nn,nt,tn]\n";
}

std::vector<int> parse_int_list(const std::string& text, const std::string& flag) {
    std::vector<int> values;
    for (const std::string& item : split_list(text)) {
        values.push_back(parse_int(item, flag));
    }
    if (values.empty()) {
        fail(flag + " needs at least one value", kExitUsage);
    }
    return values;
}

std::vector<std::string> parse_string_list(const std::string& text, const std::string& flag) {
    const std::vector<std::string> values = split_list(text);
    if (values.empty()) {
        fail(flag + " needs at least one value", kExitUsage);
    }
    return values;
}

void validate_options(Options& options) {
    for (const int block : options.blocks) {
        if (block <= 0 || block > kMaxBlockSize) {
            fail("block size " + std::to_string(block) + " is outside 1.." +
                     std::to_string(kMaxBlockSize),
                 kExitUsage);
        }
    }
    for (const int size : options.sizes) {
        if (size < 2 || size > kMaxProblemSize) {
            fail("matrix size " + std::to_string(size) + " is outside 2.." +
                     std::to_string(kMaxProblemSize),
                 kExitUsage);
        }
    }
    std::vector<std::string> dtypes;
    for (const std::string& dtype : options.dtypes) {
        const std::string lowered = lowercase(dtype);
        if (lowered != "fp16" && lowered != "fp32") {
            fail("unknown dtype " + dtype + "; expected fp16 or fp32", kExitUsage);
        }
        dtypes.push_back(lowered);
    }
    std::vector<std::string> layouts;
    for (const std::string& layout : options.layouts) {
        const std::string lowered = lowercase(layout);
        if (lowered != "nn" && lowered != "nt" && lowered != "tn") {
            fail("unknown layout " + layout + "; expected nn, nt, or tn", kExitUsage);
        }
        layouts.push_back(lowered);
    }
    options.dtypes = dtypes;
    options.layouts = layouts;
}

Options parse_options(int argc, char** argv) {
    Options options;
    int index = 1;
    while (index < argc) {
        const std::string argument = argv[index];
        if (argument == "--json") {
            // Every mode prints JSON, so the flag is not required to switch
            // formats. It stays accepted because an explicit flag documents
            // what the caller expects from stdout.
        } else if (argument == "--blocks") {
            options.blocks = parse_int_list(next_value(argc, argv, index, argument), argument);
        } else if (argument == "--sizes") {
            options.sizes = parse_int_list(next_value(argc, argv, index, argument), argument);
        } else if (argument == "--dtype" || argument == "--dtypes") {
            // The sandbox gpu-check target spells this flag with a plural, so
            // both spellings have to work; the report only shows the values.
            options.dtypes = parse_string_list(next_value(argc, argv, index, argument), argument);
        } else if (argument == "--layouts") {
            options.layouts = parse_string_list(next_value(argc, argv, index, argument), argument);
        } else if (argument == "--help" || argument == "-h") {
            print_usage(std::cout);
            std::exit(0);
        } else {
            fail("unknown argument " + argument, kExitUsage);
        }
        ++index;
    }
    validate_options(options);
    return options;
}

}  // namespace

static_assert(sizeof(__half) == sizeof(rocblas_half),
              "rocBLAS half precision must stay 16 bits wide");

// 16-byte accesses keep this kernel memory bound instead of instruction bound:
// the bus, not the scheduler, sets the time. The grid-stride loop lets a small
// grid cover a buffer that is far larger than one full occupancy wave.
__global__ void copy_kernel(const float* __restrict__ source, float* __restrict__ destination,
                            std::size_t count) {
    const std::size_t vector_count = count / 4;
    const float4* source_vectors = reinterpret_cast<const float4*>(source);
    float4* destination_vectors = reinterpret_cast<float4*>(destination);
    const std::size_t thread = static_cast<std::size_t>(blockIdx.x) * blockDim.x + threadIdx.x;
    const std::size_t stride = static_cast<std::size_t>(blockDim.x) * gridDim.x;
    for (std::size_t index = thread; index < vector_count; index += stride) {
        destination_vectors[index] = source_vectors[index];
    }
    for (std::size_t index = vector_count * 4 + thread; index < count; index += stride) {
        destination[index] = source[index];
    }
}

template <typename T>
__device__ __forceinline__ T fill_value(std::size_t index) {
    // Small exact binary fractions never become denormals in either dtype, so
    // the GEMM kernels stay on the fast path for the whole timed section.
    const float value = static_cast<float>((index % 13) + 1) * 0.0625f;
    if constexpr (std::is_same_v<T, float>) {
        return value;
    } else {
        return __float2half_rn(value);
    }
}

template <typename T>
__global__ void fill_kernel(T* data, std::size_t count) {
    const std::size_t thread = static_cast<std::size_t>(blockIdx.x) * blockDim.x + threadIdx.x;
    const std::size_t stride = static_cast<std::size_t>(blockDim.x) * gridDim.x;
    for (std::size_t index = thread; index < count; index += stride) {
        data[index] = fill_value<T>(index);
    }
}

namespace {

template <typename T>
T* device_allocate(std::size_t count) {
    T* pointer = nullptr;
    check_hip(hipMalloc(reinterpret_cast<void**>(&pointer), count * sizeof(T)), "hipMalloc");
    return pointer;
}

template <typename T>
void device_release(T* pointer) {
    if (pointer != nullptr) {
        check_hip(hipFree(pointer), "hipFree");
    }
}

void require_device_memory(std::size_t bytes, const std::string& purpose) {
    std::size_t free_bytes = 0;
    std::size_t total_bytes = 0;
    check_hip(hipMemGetInfo(&free_bytes, &total_bytes), "hipMemGetInfo");
    if (free_bytes < bytes) {
        fail("not enough free device memory for " + purpose + ": need " +
             std::to_string(bytes) + " bytes, have " + std::to_string(free_bytes));
    }
}

template <typename T>
void fill_device(T* data, std::size_t count) {
    const std::size_t blocks = std::min<std::size_t>(
        ceil_div(count, static_cast<std::size_t>(kFillBlock)),
        static_cast<std::size_t>(kFillGridCap));
    fill_kernel<T><<<static_cast<int>(blocks), kFillBlock>>>(data, count);
    check_hip(hipGetLastError(), "fill_kernel launch");
}

void launch_copy(const float* source, float* destination, std::size_t count, int block,
                 int grid) {
    copy_kernel<<<grid, block>>>(source, destination, count);
    check_hip(hipGetLastError(), "copy_kernel launch");
}

// Enqueues every timed launch before synchronizing. Waiting between iterations
// would drain the queue and fold host launch latency into the measured window,
// which distorts the smaller GEMM shapes.
class EventTimer {
public:
    EventTimer() : starts_(kTimedIterations, nullptr), stops_(kTimedIterations, nullptr) {
        for (std::size_t index = 0; index < starts_.size(); ++index) {
            HIP_CHECK(hipEventCreate(&starts_[index]));
            HIP_CHECK(hipEventCreate(&stops_[index]));
        }
    }

    ~EventTimer() {
        for (const hipEvent_t event : starts_) {
            (void)hipEventDestroy(event);
        }
        for (const hipEvent_t event : stops_) {
            (void)hipEventDestroy(event);
        }
    }

    EventTimer(const EventTimer&) = delete;
    EventTimer& operator=(const EventTimer&) = delete;

    template <typename Launch>
    double mean_seconds(Launch launch) {
        for (std::size_t index = 0; index < starts_.size(); ++index) {
            HIP_CHECK(hipEventRecord(starts_[index]));
            launch();
            HIP_CHECK(hipEventRecord(stops_[index]));
        }
        double total_ms = 0.0;
        for (std::size_t index = 0; index < starts_.size(); ++index) {
            HIP_CHECK(hipEventSynchronize(stops_[index]));
            float elapsed_ms = 0.0f;
            HIP_CHECK(hipEventElapsedTime(&elapsed_ms, starts_[index], stops_[index]));
            total_ms += static_cast<double>(elapsed_ms);
        }
        return total_ms / 1000.0 / static_cast<double>(kTimedIterations);
    }

private:
    std::vector<hipEvent_t> starts_;
    std::vector<hipEvent_t> stops_;
};

template <typename Launch>
void warm_up(Launch launch) {
    for (int iteration = 0; iteration < kWarmupIterations; ++iteration) {
        launch();
    }
    HIP_CHECK(hipDeviceSynchronize());
}

struct CopyResult {
    int block;
    double gbps;
};

struct GemmResult {
    std::string dtype;
    std::string layout;
    int size;
    double gflops;
};

void benchmark_copy(const hipDeviceProp_t& properties, const std::vector<int>& blocks,
                    EventTimer& timer, std::vector<CopyResult>& results) {
    const std::size_t element_count = kCopyBufferBytes / sizeof(float);
    require_device_memory(kCopyBufferBytes * 2, "the copy buffers");
    float* source = device_allocate<float>(element_count);
    float* destination = device_allocate<float>(element_count);
    fill_device(source, element_count);
    HIP_CHECK(hipDeviceSynchronize());

    const std::size_t vector_count = element_count / 4;
    const int threads_per_cu =
        properties.maxThreadsPerMultiProcessor > 0 ? properties.maxThreadsPerMultiProcessor : 2048;
    const std::size_t resident_blocks = static_cast<std::size_t>(properties.multiProcessorCount) *
                                        static_cast<std::size_t>(threads_per_cu);

    for (const int block : blocks) {
        // The grid follows the device instead of a guessed constant: enough
        // resident workgroups to fill every compute unit several times over,
        // but never more blocks than there is vector work to hand out.
        const std::size_t resident = resident_blocks / static_cast<std::size_t>(block);
        const std::size_t grid =
            std::max<std::size_t>(1, std::min(ceil_div(vector_count, static_cast<std::size_t>(block)),
                                              resident * static_cast<std::size_t>(kCopyWavesPerCu)));
        warm_up([&]() { launch_copy(source, destination, element_count, block, static_cast<int>(grid)); });
        const double seconds = timer.mean_seconds([&]() {
            launch_copy(source, destination, element_count, block, static_cast<int>(grid));
        });
        if (!(seconds > 0.0) || !std::isfinite(seconds)) {
            fail("the copy timer reported a non-positive interval for block size " +
                 std::to_string(block));
        }
        const double gbps = static_cast<double>(kCopyMovedBytes) / seconds / kGiga;
        results.push_back(CopyResult{block, gbps});
    }

    device_release(destination);
    device_release(source);
}

// The layout labels name the rocBLAS transpose flags for A and B: nn keeps both
// operands as stored, nt transposes B, and tn transposes A. These are the three
// hardware paths a square GEMM can take, so the report shows one row per path.
bool layout_operations(const std::string& layout, rocblas_operation* operation_a,
                       rocblas_operation* operation_b) {
    if (layout == "nn") {
        *operation_a = rocblas_operation_none;
        *operation_b = rocblas_operation_none;
        return true;
    }
    if (layout == "nt") {
        *operation_a = rocblas_operation_none;
        *operation_b = rocblas_operation_transpose;
        return true;
    }
    if (layout == "tn") {
        *operation_a = rocblas_operation_transpose;
        *operation_b = rocblas_operation_none;
        return true;
    }
    return false;
}

template <typename T>
rocblas_status launch_gemm(rocblas_handle handle, rocblas_operation operation_a,
                           rocblas_operation operation_b, rocblas_int size, T* a, T* b, T* c) {
    // Square operands make the leading dimension equal to the problem size
    // under either transpose flag.
    const rocblas_int leading_dimension = size;
    if constexpr (std::is_same_v<T, float>) {
        const float alpha = 1.0f;
        const float beta = 0.0f;
        return rocblas_sgemm(handle, operation_a, operation_b, size, size, size, &alpha, a,
                             leading_dimension, b, leading_dimension, &beta, c,
                             leading_dimension);
    } else {
        // rocBLAS defines rocblas_half as a packed struct on some builds and as
        // _Float16 on others, so alpha and beta are set from their IEEE binary16
        // bit patterns rather than through a conversion helper that may not
        // exist on every build.
        rocblas_half alpha{};
        rocblas_half beta{};
        const std::uint16_t one_bits = 0x3c00;
        const std::uint16_t zero_bits = 0x0000;
        std::memcpy(&alpha, &one_bits, sizeof(alpha));
        std::memcpy(&beta, &zero_bits, sizeof(beta));
        return rocblas_hgemm(handle, operation_a, operation_b, size, size, size, &alpha,
                             reinterpret_cast<const rocblas_half*>(a), leading_dimension,
                             reinterpret_cast<const rocblas_half*>(b), leading_dimension, &beta,
                             reinterpret_cast<rocblas_half*>(c), leading_dimension);
    }
}

template <typename T>
void benchmark_gemm(rocblas_handle handle, const std::string& dtype, const std::vector<int>& sizes,
                    const std::vector<std::string>& layouts, EventTimer& timer,
                    std::vector<GemmResult>& results) {
    for (const int size : sizes) {
        const std::size_t elements =
            static_cast<std::size_t>(size) * static_cast<std::size_t>(size);
        const std::size_t bytes = elements * sizeof(T);
        require_device_memory(bytes * 3, dtype + " GEMM at size " + std::to_string(size));
        T* a = device_allocate<T>(elements);
        T* b = device_allocate<T>(elements);
        T* c = device_allocate<T>(elements);
        fill_device(a, elements);
        fill_device(b, elements);
        fill_device(c, elements);
        HIP_CHECK(hipDeviceSynchronize());

        for (const std::string& layout : layouts) {
            rocblas_operation operation_a = rocblas_operation_none;
            rocblas_operation operation_b = rocblas_operation_none;
            if (!layout_operations(layout, &operation_a, &operation_b)) {
                fail("unknown layout " + layout, kExitUsage);
            }
            const rocblas_int problem = static_cast<rocblas_int>(size);
            warm_up([&]() {
                check_rocblas(launch_gemm<T>(handle, operation_a, operation_b, problem, a, b, c));
            });
            const double seconds = timer.mean_seconds([&]() {
                check_rocblas(launch_gemm<T>(handle, operation_a, operation_b, problem, a, b, c));
            });
            if (!(seconds > 0.0) || !std::isfinite(seconds)) {
                fail("the GEMM timer reported a non-positive interval for " + dtype + " " +
                     layout + " " + std::to_string(size));
            }
            // GEMM is 2 * M * N * K floating point operations, and M, N, and K
            // are all the problem size for these square cases.
            const double operations = 2.0 * static_cast<double>(elements) *
                                      static_cast<double>(size);
            results.push_back(GemmResult{dtype, layout, size, operations / seconds / kGiga});
        }

        device_release(c);
        device_release(b);
        device_release(a);
    }
}

std::string render_json(const std::string& device, const std::string& gfx,
                        const std::string& rocm_version, const std::string& rocm_release,
                        const std::vector<CopyResult>& copy,
                        const std::vector<GemmResult>& gemm) {
    std::ostringstream out;
    out << "{\"device\":" << json_string(device) << ",\"gfx\":" << json_string(gfx)
        << ",\"rocm_version\":" << json_string(rocm_version);
    if (!rocm_release.empty()) {
        // The package release in /opt/rocm/.info/version is not always the
        // string the runtime reports, so keep both when the file exists.
        out << ",\"rocm_release\":" << json_string(rocm_release);
    }
    out << ",\"copy\":[";
    for (std::size_t index = 0; index < copy.size(); ++index) {
        if (index != 0) {
            out << ",";
        }
        out << "{\"block\":" << copy[index].block << ",\"gbps\":" << json_number(copy[index].gbps)
            << "}";
    }
    out << "],\"gemm\":[";
    for (std::size_t index = 0; index < gemm.size(); ++index) {
        if (index != 0) {
            out << ",";
        }
        out << "{\"dtype\":" << json_string(gemm[index].dtype)
            << ",\"layout\":" << json_string(gemm[index].layout)
            << ",\"size\":" << gemm[index].size
            << ",\"gflops\":" << json_number(gemm[index].gflops) << "}";
    }
    out << "],\"clocks\":{\"gpu_mhz\":null,\"power_w\":null},\"errors\":[]}";
    return out.str();
}

std::string gfx_architecture(const hipDeviceProp_t& properties) {
#if defined(__HIP_PLATFORM_AMD__)
    // gcnArchName can carry feature flags after a colon (xnack, sramecc). The
    // report wants the architecture alone, so keep the part before the colon.
    std::string architecture = trim(properties.gcnArchName);
    const std::size_t colon = architecture.find(':');
    if (colon != std::string::npos) {
        architecture = architecture.substr(0, colon);
    }
    return architecture;
#else
    (void)properties;
    return "";
#endif
}

}  // namespace

int main(int argc, char** argv) {
    const Options options = parse_options(argc, argv);

    int device_count = 0;
    check_hip(hipGetDeviceCount(&device_count), "hipGetDeviceCount");
    if (device_count <= 0) {
        fail("no HIP device is visible; run this inside the ROCm image with /dev/kfd and a "
             "render node");
    }
    check_hip(hipSetDevice(0), "hipSetDevice");
    hipDeviceProp_t properties{};
    check_hip(hipGetDeviceProperties(&properties, 0), "hipGetDeviceProperties");

    int runtime_version = 0;
    check_hip(hipRuntimeGetVersion(&runtime_version), "hipRuntimeGetVersion");
    const std::string rocm_version = format_version(runtime_version);
    const std::string rocm_release = read_first_line("/opt/rocm/.info/version");
    const std::string gfx = gfx_architecture(properties);

    std::cerr << kProgramName << ": " << properties.name << " ("
              << (gfx.empty() ? "unknown gfx" : gfx) << "), HIP " << rocm_version << "\n";

    std::vector<CopyResult> copy_results;
    std::vector<GemmResult> gemm_results;
    {
        rocblas_handle handle = nullptr;
        check_rocblas(rocblas_create_handle(&handle));
        check_rocblas(rocblas_set_pointer_mode(handle, rocblas_pointer_mode_host));
        EventTimer timer;
        benchmark_copy(properties, options.blocks, timer, copy_results);
        for (const std::string& dtype : options.dtypes) {
            if (dtype == "fp32") {
                benchmark_gemm<float>(handle, dtype, options.sizes, options.layouts, timer,
                                      gemm_results);
            } else {
                benchmark_gemm<__half>(handle, dtype, options.sizes, options.layouts, timer,
                                       gemm_results);
            }
        }
        check_rocblas(rocblas_destroy_handle(handle));
    }

    std::cout << render_json(properties.name, gfx, rocm_version, rocm_release, copy_results,
                             gemm_results)
              << "\n";
    return 0;
}
