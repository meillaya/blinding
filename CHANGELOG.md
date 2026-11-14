# Changelog

All notable changes to this project are documented here. The format follows
Keep a Changelog and the project uses semantic versioning.

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
