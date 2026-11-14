# Changelog

All notable changes to this project are documented here. The format follows
Keep a Changelog and the project uses semantic versioning.

## 0.3.0

### Added

- Kernel records now carry per-iteration samples, and the compare engine can
  gate kernel runs the same way it gates serving runs: a copy or GEMM row is a
  regression only when its bootstrap interval sits beyond the tolerance.
- Source-level copy variants (`--copy-variants vecN-iM-wW`) and
  `--autotune-copy`, which sweeps 18 variant, item, and wave combinations and
  reports the best per block. Every variant is checked against the source
  buffer before it is timed.
- Roofline context: `configs/rocm-peaks.toml` carries the gfx1101 peaks and
  each kernel row reports its percent of peak.
- `blinding profile <model>:<quant>` runs llama-bench inference under
  rocprofv3, records the per-kernel time breakdown, and keeps the trace under
  the git-ignored run trace directory. `blinding kernel --profile` does the
  same for the HIP benchmark.
- The kernel command emits `bench_schema: 2`; schema-1 rows from an older
  binary still validate and compare as point values.

### Changed

- Compare JSON gains `rows` and `skipped_rows` for kernel runs, and the table
  layout depends on the run kind.
- The ROCm image installs libdw, which rocprofiler-sdk needs for kernel
  traces.
- `results/**/trace/` is ignored: profiler traces are local evidence.

## 0.2.0

### Added

- `blinding compare <baseline> <candidate>`: per-level TTFT, inter-token
  latency, decode throughput, and error-rate deltas with percentile bootstrap
  intervals over the recorded request samples, a verdict per metric, and a JSON
  form (`blinding.compare/1`).
- `blinding gate --baseline <run> [--candidate <run>]`: CI entry point with
  exit 0 for a pass, 1 for a regression beyond the tolerance, and 3 for
  unusable evidence. It fails on host or device changes unless the matching
  `--allow-*` flag says the change was intended.
- Endpoint mode for `bench` and `eval`: `--url` plus `--model-name` measures a
  server that is already running, with no manifest entry and no local
  llama.cpp install. Prompt length falls back to a non-streamed usage probe
  when the server has no `/tokenize`.
- `bench --json` prints the run record for scripts, and `BLINDING_API_KEY`
  supplies the key without putting it on the command line.
- Fake-server calibration test: measured TTFT and ITL must stay inside stated
  bounds of the configured delays, which keeps the harness honest.
- devenv and Nix flake entry points. `devenv shell` and `nix develop` both
  evaluate the same `devenv.nix`; `devenv.lock` and `flake.lock` are committed.
- MIT license, package metadata, and a rewritten README that opens with the
  compare loop instead of a results report.

### Changed

- Run records use schema 2. They gain `model.source`, endpoint records gain
  `model.base_url`, `config: null`, and an empty device list, and the workload
  gains `prompt_tokens_source`. Readers accept schema 1 and 2.
- Run records redact API keys and URL credentials from the recorded argv.
- `results compare` was a stub that printed raw metrics; it is replaced by the
  top-level `compare` and `gate` commands.
- The sandbox script passes an explicit storage driver only when it created the
  store with one, and writes the podman policy to the XDG config path podman
  reads.

### Fixed

- Rootless podman builds work again on hosts where podman records an empty
  graph driver when it auto-detects one.
