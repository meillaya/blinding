"""Command line for Blinding.

Each measurement command writes a run directory before it prints a summary. The
serve command writes one too, because the startup time and offload count are
facts worth keeping next to the benchmark that follows.
"""

from __future__ import annotations

import argparse
import asyncio
import csv
import json
import os
import signal
import sys
import time
from dataclasses import replace
from pathlib import Path
from types import FrameType
from typing import Any

import httpx

from blinding import __version__, bench, client, doctor, download, manifest, proc, report, server
from blinding.config import Device, ServerConfig, default_config, load_config
from blinding.errors import BlindingError, MissingDependency, UsageError
from blinding.report import markdown_table

ROOT = report.repo_root()
DEFAULT_MANIFEST = "models.toml"
DEFAULT_RESULTS = "results"
DEFAULT_CONFIG = "configs/local-cpu.toml"
DEFAULT_ROCM_CONFIG = "configs/local-rocm.toml"
GATEWAY_WAIT_S = 30.0
GATEWAY_POLL_S = 0.2
SERVE_POLL_S = 0.5


_PROGRESS_BUCKET: dict[str, int] = {}


def _progress(filename: str, written: int, total: int) -> None:
    fraction = written / total if total > 0 else 0.0
    bucket = 100 if written >= total else int(fraction * 20) * 5
    if bucket == _PROGRESS_BUCKET.get(filename) and written < total:
        return
    _PROGRESS_BUCKET[filename] = bucket
    message = f"\r{filename} {fraction:6.1%} ({written / 1e6:.1f}/{total / 1e6:.1f} MB)"
    print(message, end="", file=sys.stderr, flush=True)
    if written >= total:
        print(file=sys.stderr)


def _paths(args: argparse.Namespace) -> tuple[Path, Path, Path]:
    manifest_path = Path(args.manifest).expanduser() if args.manifest else ROOT / DEFAULT_MANIFEST
    cache = Path(args.cache).expanduser() if args.cache else download.default_cache()
    results = Path(args.results).expanduser() if args.results else ROOT / DEFAULT_RESULTS
    return (manifest_path, cache, results)


def _tools(args: argparse.Namespace) -> proc.LlamaTools:
    directory = Path(args.llama_bin).expanduser() if args.llama_bin else None
    return proc.discover(directory)


def _resolve(
    args: argparse.Namespace, spec: str
) -> tuple[manifest.Model, str, Path, Path, manifest.Manifest]:
    manifest_path, cache, results = _paths(args)
    loaded = manifest.load_manifest(manifest_path)
    model, quant = loaded.resolve(spec)
    return (model, quant, cache, results, loaded)


def _model_meta(model: manifest.Model, quant: str, paths: tuple[Path, ...]) -> dict[str, object]:
    return {
        "id": model.id,
        "quant": quant,
        "context": model.context,
        "revision": model.revision,
        "files": [
            {"file": item.file, "sha256": item.sha256, "path": str(path)}
            for item, path in zip(model.files_for(quant), paths, strict=True)
        ],
    }


def _config(args: argparse.Namespace) -> ServerConfig:
    if args.config:
        config = load_config(Path(args.config).expanduser())
    elif getattr(args, "device", None) == Device.HIP.value:
        config = default_config(Device.HIP)
    else:
        config = default_config()
    if getattr(args, "device", None):
        config = config.with_device(Device(args.device))
    overrides: dict[str, Any] = {}
    for field in ("threads", "ctx_size", "gpu_layers", "port", "parallel"):
        value = getattr(args, field, None)
        if value is not None:
            overrides[field] = value
    return replace(config, **overrides) if overrides else config


def _ensure(model: manifest.Model, quant: str, cache: Path) -> tuple[Path, ...]:
    return download.ensure_model(model, quant, cache, progress=_progress)


def _write_report(
    run_dir: Path,
    record: dict[str, object],
    title: str,
    sections: list[tuple[str, str]],
) -> None:
    parts = [f"# {title}", ""]
    for heading, body in sections:
        if heading:
            parts.extend([f"## {heading}", ""])
        parts.extend([body.rstrip(), ""])
    report.write_run(run_dir, record, "\n".join(parts))


def _print_table(headers: list[str], rows: list[list[str]]) -> None:
    if not rows:
        print("no rows")
        return
    widths = [len(header) for header in headers]
    for row in rows:
        for index, cell in enumerate(row):
            widths[index] = max(widths[index], len(cell))
    print("  ".join(header.ljust(widths[index]) for index, header in enumerate(headers)))
    print("  ".join("-" * width for width in widths))
    for row in rows:
        print("  ".join(cell.ljust(widths[index]) for index, cell in enumerate(row)))


def _wait_gateway(port: int, process: Any) -> None:
    url = f"http://127.0.0.1:{port}/healthz"
    deadline = time.monotonic() + GATEWAY_WAIT_S
    while time.monotonic() < deadline:
        if process.poll() is not None:
            raise MissingDependency("gateway exited before it became healthy")
        try:
            response = httpx.get(url, timeout=2.0)
        except httpx.HTTPError:
            response = None
        if response is not None and response.status_code == 200:
            return
        time.sleep(GATEWAY_POLL_S)
    raise MissingDependency(f"gateway was not healthy within {GATEWAY_WAIT_S:g}s")


def _start_gateway(args: argparse.Namespace, running: server.RunningServer) -> Any:
    import subprocess

    keys = args.api_key or os.environ.get("BLINDING_API_KEYS", "")
    if not keys:
        raise UsageError("serving through the gateway needs BLINDING_API_KEYS or --api-key")
    env = os.environ.copy()
    env.update(
        {
            "BLINDING_API_KEYS": keys,
            "BLINDING_LLM_URL": running.base_url(),
            "BLINDING_HOST": "127.0.0.1",
            "BLINDING_PORT": str(args.gateway_port),
        }
    )
    if getattr(args, "rate_limit", None) is not None:
        env["BLINDING_RATE_LIMIT_RPS"] = str(args.rate_limit)
    process = subprocess.Popen(
        [sys.executable, "-m", "blinding.gateway"],
        env=env,
        stdin=subprocess.DEVNULL,
        start_new_session=True,
    )
    try:
        _wait_gateway(args.gateway_port, process)
    except BaseException:
        process.terminate()
        raise
    return process


def _stop_gateway(process: Any) -> None:
    if process.poll() is not None:
        return
    try:
        os.killpg(os.getpgid(process.pid), signal.SIGTERM)
    except (OSError, ProcessLookupError):
        process.terminate()
    try:
        process.wait(timeout=10.0)
    except Exception:
        process.kill()


def cmd_doctor(args: argparse.Namespace) -> int:
    tools = _tools(args)
    _, cache, _ = _paths(args)
    data = doctor.collect(tools, cache)
    if args.json:
        print(json.dumps(data, indent=2, sort_keys=True))
    else:
        print(doctor.format_text(data))
    return 0


def cmd_models_list(args: argparse.Namespace) -> int:
    manifest_path, _, _ = _paths(args)
    loaded = manifest.load_manifest(manifest_path)
    rows = [
        [model.id, ",".join(model.quants()), str(model.context), model.revision[:12]]
        for model in loaded.models
    ]
    if args.json:
        payload = [
            {
                "id": model.id,
                "repo": model.repo,
                "revision": model.revision,
                "context": model.context,
                "default_quant": model.default_quant,
                "quants": list(model.quants()),
            }
            for model in loaded.models
        ]
        print(json.dumps(payload, indent=2))
    else:
        _print_table(["model", "quants", "context", "revision"], rows)
    return 0


def cmd_models_pull(args: argparse.Namespace) -> int:
    model, quant, cache, _, _ = _resolve(args, args.model)
    paths = _ensure(model, quant, cache)
    for path in paths:
        print(path)
    return 0


def cmd_models_verify(args: argparse.Namespace) -> int:
    model, quant, cache, _, _ = _resolve(args, args.model)
    bad: list[str] = []
    for item in model.files_for(quant):
        path = download.cache_path(cache, item)
        if not download.verify_file(path, item):
            bad.append(f"{item.file}: missing or hash mismatch at {path}")
    if bad:
        raise MissingDependency("\n".join(bad))
    print(f"{model.id}:{quant} verified")
    return 0


def cmd_serve(args: argparse.Namespace) -> int:
    model, quant, cache, results, _ = _resolve(args, args.model)
    config = _config(args)
    paths = _ensure(model, quant, cache)
    run_dir = report.new_run_dir(results, "serve", f"{model.id}-{quant}")
    started_at = report.utc_iso()
    started = time.perf_counter()
    gateway_process: Any = None
    exit_code = 0
    startup_s = 0.0
    try:
        with server.LlamaServer(
            _tools(args),
            config,
            paths[0],
            args.alias or model.id,
            run_dir,
            port=args.port,
        ) as running:
            startup_s = time.perf_counter() - started
            offload = running.offload()
            print(f"model     {model.id}:{quant}")
            print(f"server    {running.base_url()} ({config.device})")
            print(f"startup   {startup_s:.2f}s")
            if offload is not None:
                print(f"offload   {offload[0]}/{offload[1]} layers to GPU")
            if args.gateway:
                gateway_process = _start_gateway(args, running)
                print(f"gateway   http://127.0.0.1:{args.gateway_port}")
            print("press Ctrl-C to stop")
            stop_requested = False

            def request_stop(_signum: int, _frame: FrameType | None) -> None:
                nonlocal stop_requested
                stop_requested = True

            previous_int = signal.signal(signal.SIGINT, request_stop)
            previous_term = signal.signal(signal.SIGTERM, request_stop)
            try:
                while running.process.poll() is None and not stop_requested:
                    time.sleep(SERVE_POLL_S)
            finally:
                signal.signal(signal.SIGINT, previous_int)
                signal.signal(signal.SIGTERM, previous_term)
    except KeyboardInterrupt:
        pass
    finally:
        if gateway_process is not None:
            _stop_gateway(gateway_process)
        duration = time.perf_counter() - started
        record = report.build_record(
            command="serve",
            argv=sys.argv,
            root=ROOT,
            tools=_tools(args),
            model=_model_meta(model, quant, paths),
            config=config.as_dict(),
            workload={"gateway": bool(args.gateway), "gateway_port": args.gateway_port},
            metrics={"startup_s": round(startup_s, 3)},
            validity={"valid": True, "reasons": []},
            artifacts={"server_log": "server.log"},
            started_at=started_at,
            duration_s=duration,
        )
        _write_report(
            run_dir,
            record,
            f"Serve {model.id}:{quant}",
            [
                (
                    "Result",
                    markdown_table(
                        ["field", "value"],
                        [
                            ["device", str(config.device)],
                            ["startup", f"{duration:.2f}s"],
                        ],
                    ),
                )
            ],
        )
        print(f"run       {run_dir}")
    return exit_code


def cmd_gateway(args: argparse.Namespace) -> int:
    import uvicorn

    from blinding.gateway.app import create_app
    from blinding.gateway.settings import GatewaySettings

    env_map = {
        "llm_url": args.llm_url,
        "api_keys": args.api_keys,
        "rate_limit_rps": args.rate_limit,
        "host": args.host,
        "port": args.port,
    }
    env_names = {
        "llm_url": "BLINDING_LLM_URL",
        "api_keys": "BLINDING_API_KEYS",
        "rate_limit_rps": "BLINDING_RATE_LIMIT_RPS",
        "host": "BLINDING_HOST",
        "port": "BLINDING_PORT",
    }
    for field, value in env_map.items():
        if value is not None:
            os.environ[env_names[field]] = str(value)
    llm_url = os.environ.get("BLINDING_LLM_URL")
    if not llm_url:
        raise UsageError("gateway needs --llm-url or BLINDING_LLM_URL")
    settings = GatewaySettings(llm_url=llm_url)
    app = create_app(settings)
    uvicorn.run(app, host=settings.host, port=settings.port, log_level="info")
    return 0


def _start_test_server(
    args: argparse.Namespace,
    config: ServerConfig,
    model: manifest.Model,
    paths: tuple[Path, ...],
    run_dir: Path,
) -> server.LlamaServer:
    return server.LlamaServer(
        _tools(args),
        config,
        paths[0],
        args.alias or model.id,
        run_dir,
        port=args.port or proc.free_port(),
    )


def cmd_bench(args: argparse.Namespace) -> int:
    model, quant, cache, results, _ = _resolve(args, args.model)
    run_dir = report.new_run_dir(results, "bench", f"{model.id}-{quant}")
    started_at = report.utc_iso()
    paths: tuple[Path, ...] = ()
    config = _config(args)
    if args.url:
        return _run_bench_inside(
            args,
            model,
            quant,
            paths,
            config,
            args.url.rstrip("/"),
            args.alias or model.id,
            run_dir,
            started_at,
        )
    paths = _ensure(model, quant, cache)
    serve_started = time.perf_counter()
    exit_code = 3
    with _start_test_server(args, config, model, paths, run_dir) as running:
        exit_code = _run_bench_inside(
            args,
            model,
            quant,
            paths,
            config,
            running.base_url(),
            args.alias or model.id,
            run_dir,
            started_at,
            offload=running.offload(),
            startup_s=time.perf_counter() - serve_started,
        )
    return exit_code


def _run_bench_inside(
    args: argparse.Namespace,
    model: manifest.Model,
    quant: str,
    paths: tuple[Path, ...],
    config: ServerConfig,
    base_url: str,
    model_label: str,
    run_dir: Path,
    started_at: str,
    *,
    offload: tuple[int, int] | None = None,
    startup_s: float = 0.0,
) -> int:
    started = time.perf_counter()
    work = bench.BenchConfig(
        model=model_label,
        base_url=base_url,
        api_key=args.api_key,
        concurrency=tuple(args.concurrency),
        requests_per_level=args.requests,
        warmup=args.warmup,
        prompt_tokens=args.prompt_tokens,
        max_tokens=args.max_tokens,
        temperature=args.temperature,
        timeout_s=args.timeout,
    )
    result = asyncio.run(bench.run_bench(work))
    duration = time.perf_counter() - started
    rows = bench.summary_rows(result)
    headers = ["conc", "ok", "errors", "ttft p50", "ttft p95", "itl p50", "tok/s", "latency p95"]
    _print_table(headers, rows)
    with (run_dir / "requests.csv").open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(
            handle, fieldnames=list(result.samples[0].keys()) if result.samples else []
        )
        writer.writeheader()
        writer.writerows(result.samples)
    record = report.build_record(
        command="bench",
        argv=sys.argv,
        root=ROOT,
        tools=_tools(args),
        model=_model_meta(model, quant, paths) if paths else {"id": model.id, "quant": quant},
        config=config.as_dict(),
        workload={
            "base_url": base_url,
            "concurrency": list(work.concurrency),
            "requests_per_level": work.requests_per_level,
            "prompt_tokens": result.prompt_tokens,
            "max_tokens": work.max_tokens,
            "temperature": work.temperature,
        },
        metrics={
            "levels": result.levels,
            "startup_s": round(startup_s, 3),
            "offload": list(offload) if offload is not None else None,
        },
        validity={"valid": result.valid, "reasons": result.reasons},
        artifacts={"report": "report.md", "requests": "requests.csv"},
        started_at=started_at,
        duration_s=duration,
    )
    sections: list[tuple[str, str]] = [
        ("Results", markdown_table(headers, rows)),
        (
            "Validity",
            "valid" if result.valid else "\n".join(f"- {reason}" for reason in result.reasons),
        ),
    ]
    _write_report(run_dir, record, f"Bench {model.id}:{quant}", sections)
    print(f"run       {run_dir}")
    if not result.valid:
        for reason in result.reasons:
            print(f"invalid: {reason}", file=sys.stderr)
        return 3
    return 0


def cmd_eval(args: argparse.Namespace) -> int:
    from blinding import evals

    model, quant, cache, results, _ = _resolve(args, args.model)
    config = _config(args)
    suite = evals.load_suite(Path(args.suite))
    run_dir = report.new_run_dir(results, "eval", f"{model.id}-{quant}")
    started_at = report.utc_iso()
    started = time.perf_counter()
    paths: tuple[Path, ...] = ()
    base_url = args.url.rstrip("/") if args.url else ""
    result: dict[str, Any] = {}

    async def chat(prompt: str, max_tokens: int) -> tuple[bool, str, str]:
        ok, _status, text, error = await client.chat_once(
            base_url=base_url,
            model=args.alias or model.id,
            messages=[{"role": "user", "content": prompt}],
            max_tokens=max_tokens,
            temperature=0.0,
            api_key=args.api_key,
        )
        return (ok, text, error)

    if args.url:
        result = asyncio.run(evals.run_eval(suite, chat))
    else:
        paths = _ensure(model, quant, cache)
        with _start_test_server(args, config, model, paths, run_dir) as running:
            base_url = running.base_url()
            result = asyncio.run(evals.run_eval(suite, chat))
    duration = time.perf_counter() - started
    rows = evals.summary_rows(result)
    _print_table(["id", "ok", "text"], rows)
    record = report.build_record(
        command="eval",
        argv=sys.argv,
        root=ROOT,
        tools=_tools(args),
        model=_model_meta(model, quant, paths) if paths else {"id": model.id, "quant": quant},
        config=config.as_dict(),
        workload={"suite": str(args.suite), "cases": len(suite)},
        metrics=result,
        validity={"valid": True, "reasons": []},
        artifacts={"report": "report.md"},
        started_at=started_at,
        duration_s=duration,
    )
    sections = [
        ("Cases", markdown_table(["id", "ok", "text"], rows)),
        (
            "Score",
            f"passed {result['passed']} of {result['trials']} "
            f"({result['rate']:.1%}, Wilson {result['wilson_low']:.1%} to "
            f"{result['wilson_high']:.1%})",
        ),
    ]
    _write_report(run_dir, record, f"Eval {model.id}:{quant}", sections)
    print(
        f"score     {result['passed']}/{result['trials']} ({result['rate']:.1%}) "
        f"wilson {result['wilson_low']:.1%}..{result['wilson_high']:.1%}"
    )
    print(f"run       {run_dir}")
    return 0


def cmd_tune(args: argparse.Namespace) -> int:
    from blinding import tune

    model, quant, cache, results, _ = _resolve(args, args.model)
    del quant
    tools = _tools(args)
    bench_binary = tools.require("llama-bench")
    quants = tuple(args.quants) if args.quants else model.quants()
    device = Device(args.device)
    threads = tuple(args.threads) if args.threads else ((8, 16) if device is Device.CPU else (8,))
    gpu_layers = (99,) if device is Device.HIP else (0,)
    candidates = tune.expand_grid(quants, threads, gpu_layers)
    run_dir = report.new_run_dir(results, "tune", f"{model.id}")
    started_at = report.utc_iso()
    started = time.perf_counter()
    rows: list[Any] = []
    for candidate in candidates:
        files = _ensure(model, candidate.quant, cache)
        print(f"bench     {candidate.quant} threads={candidate.threads} ngl={candidate.gpu_layers}")
        rows.append(
            tune.run_candidate(
                bench_binary,
                files[0],
                candidate,
                pp=args.pp,
                tg=args.tg,
                reps=args.reps,
                timeout_s=args.timeout,
            )
        )
    duration = time.perf_counter() - started
    recommendation = tune.recommend(rows)
    table_headers = ["quant", "threads", "ngl", "pp tok/s", "tg tok/s", "GiB"]
    table_rows = tune.summary_rows(rows)
    _print_table(table_headers, table_rows)
    record = report.build_record(
        command="tune",
        argv=sys.argv,
        root=ROOT,
        tools=tools,
        model={"id": model.id, "quants": list(quants)},
        config={"device": device.value},
        workload={"candidates": len(candidates), "pp": args.pp, "tg": args.tg, "reps": args.reps},
        metrics={"rows": [row.as_dict() for row in rows], "recommendation": recommendation},
        validity={"valid": bool(rows), "reasons": [] if rows else ["no candidate completed"]},
        artifacts={"report": "report.md"},
        started_at=started_at,
        duration_s=duration,
    )
    sections = [
        ("Rows", markdown_table(table_headers, table_rows)),
        ("Recommendation", tune.render_text(rows, recommendation)),
    ]
    _write_report(run_dir, record, f"Tune {model.id}", sections)
    print(tune.render_text(rows, recommendation))
    print(f"run       {run_dir}")
    return 0 if rows else 3


def cmd_train(args: argparse.Namespace) -> int:
    from blinding import train

    model, quant, cache, results, _ = _resolve(args, args.model)
    if quant != "f32":
        raise UsageError("llama-finetune needs an F32 GGUF; pick the f32 quant")
    paths = _ensure(model, quant, cache)
    run_dir = report.new_run_dir(results, "train", f"{model.id}-{quant}")
    output = (
        Path(args.out).expanduser()
        if args.out
        else cache / "train" / model.revision[:12] / "finetuned.gguf"
    )
    output.parent.mkdir(parents=True, exist_ok=True)
    started_at = report.utc_iso()
    started = time.perf_counter()
    config = train.TrainConfig(
        model_path=paths[0],
        corpus=Path(args.corpus),
        output_path=output,
        work_dir=run_dir,
        epochs=args.epochs,
        ctx_size=args.ctx_size,
        batch_size=args.ctx_size,
        ubatch_size=args.ctx_size,
        gpu_layers=args.gpu_layers,
        timeout_s=args.timeout,
    )
    result = train.finetune(_tools(args), config)
    duration = time.perf_counter() - started
    record = report.build_record(
        command="train",
        argv=sys.argv,
        root=ROOT,
        tools=_tools(args),
        model=_model_meta(model, quant, paths),
        config={
            "epochs": args.epochs,
            "ctx_size": args.ctx_size,
            "gpu_layers": args.gpu_layers,
        },
        workload={"corpus": str(args.corpus)},
        metrics=result.as_dict(),
        validity={"valid": result.ok, "reasons": [] if result.ok else [result.error]},
        artifacts={"train_log": "finetune.log", "report": "report.md"},
        started_at=started_at,
        duration_s=duration,
    )
    sections = [
        (
            "Result",
            markdown_table(
                ["field", "value"],
                [
                    ["before ppl", f"{result.before_ppl:.3f}" if result.before_ppl else "-"],
                    ["after ppl", f"{result.after_ppl:.3f}" if result.after_ppl else "-"],
                    ["last loss", f"{result.last_loss:.4f}" if result.last_loss else "-"],
                    ["duration", f"{result.duration_s:.1f}s"],
                    ["output", str(result.output_path)],
                ],
            ),
        )
    ]
    _write_report(run_dir, record, f"Train {model.id}:{quant}", sections)
    if result.ok:
        print(
            f"ppl       {result.before_ppl:.3f} -> {result.after_ppl:.3f} "
            f"in {result.duration_s:.1f}s"
        )
        print(f"model     {result.output_path}")
        print(f"run       {run_dir}")
        return 0
    print(f"failed: {result.error}", file=sys.stderr)
    return 3


def cmd_kernel(args: argparse.Namespace) -> int:
    from blinding import kernel

    tools = _tools(args)
    _, _, results = _paths(args)
    run_dir = report.new_run_dir(results, "kernel", "hip")
    started_at = report.utc_iso()
    started = time.perf_counter()
    config = kernel.KernelConfig(
        binary=args.binary,
        blocks=tuple(args.blocks),
        sizes=tuple(args.sizes),
        dtypes=tuple(args.dtypes),
        layouts=tuple(args.layouts),
        timeout_s=args.timeout,
    )
    data = kernel.run_kernel(config)
    duration = time.perf_counter() - started
    rows = kernel.summary_rows(data)
    _print_table(["kernel", "detail", "value"], rows)
    record = report.build_record(
        command="kernel",
        argv=sys.argv,
        root=ROOT,
        tools=tools,
        model=None,
        config={"blocks": list(config.blocks), "sizes": list(config.sizes)},
        workload={"dtypes": list(config.dtypes), "layouts": list(config.layouts)},
        metrics=data,
        validity={"valid": True, "reasons": []},
        artifacts={"report": "report.md"},
        started_at=started_at,
        duration_s=duration,
    )
    _write_report(
        run_dir,
        record,
        "HIP kernel benchmark",
        [("Rows", markdown_table(["kernel", "detail", "value"], rows))],
    )
    print(f"run       {run_dir}")
    return 0


def cmd_results(args: argparse.Namespace) -> int:
    _, _, results = _paths(args)
    if args.action == "list":
        runs = report.list_runs(results)
        rows = [
            [
                Path(str(run["run_dir"])).name,
                str(run.get("command", "")),
                str(run.get("started_at", "")),
                str(bool(run.get("validity", {}).get("valid", False))),
            ]
            for run in runs
        ]
        _print_table(["run", "command", "started", "valid"], rows)
        return 0
    if args.action == "show":
        run = report.find_run(results, args.selector)
        run_dir = Path(str(run["run_dir"]))
        report_path = run_dir / "report.md"
        print(
            report_path.read_text(encoding="utf-8")
            if report_path.is_file()
            else json.dumps(run, indent=2)
        )
        return 0
    left = report.find_run(results, args.left)
    right = report.find_run(results, args.right)
    print(json.dumps({"left": left.get("metrics"), "right": right.get("metrics")}, indent=2))
    return 0


def _add_common(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--manifest", help="model manifest (default models.toml)")
    parser.add_argument("--cache", help="model cache (default BLINDING_CACHE or ~/.cache/blinding)")
    parser.add_argument("--results", help="results directory (default results)")
    parser.add_argument("--llama-bin", help="directory holding the llama.cpp binaries")


def _add_server_args(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--config", help="server TOML config")
    parser.add_argument("--device", choices=[item.value for item in Device], help="cpu or hip")
    parser.add_argument("--port", type=int, help="server port")
    parser.add_argument("--threads", type=int, help="CPU thread count")
    parser.add_argument("--ctx-size", dest="ctx_size", type=int, help="context size")
    parser.add_argument("--parallel", type=int, help="server slots")
    parser.add_argument("--gpu-layers", dest="gpu_layers", type=int, help="layers to offload")
    parser.add_argument("--alias", help="model alias shown by the API")
    parser.add_argument("--api-key", help="API key sent to the gateway")


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="blinding", description="Serve, measure, and tune llama.cpp."
    )
    parser.add_argument("--version", action="version", version=f"blinding {__version__}")
    sub = parser.add_subparsers(dest="command", required=True)

    doctor_parser = sub.add_parser("doctor", help="print host, tool, and device facts")
    _add_common(doctor_parser)
    doctor_parser.add_argument("--json", action="store_true")
    doctor_parser.set_defaults(handler=cmd_doctor)

    models_parser = sub.add_parser("models", help="list, pull, and verify pinned models")
    _add_common(models_parser)
    models_sub = models_parser.add_subparsers(dest="action", required=True)
    list_parser = models_sub.add_parser("list")
    _add_common(list_parser)
    list_parser.add_argument("--json", action="store_true")
    list_parser.set_defaults(handler=cmd_models_list)
    pull_parser = models_sub.add_parser("pull")
    _add_common(pull_parser)
    pull_parser.add_argument("model")
    pull_parser.set_defaults(handler=cmd_models_pull)
    verify_parser = models_sub.add_parser("verify")
    _add_common(verify_parser)
    verify_parser.add_argument("model")
    verify_parser.set_defaults(handler=cmd_models_verify)

    serve_parser = sub.add_parser("serve", help="run llama-server in the foreground")
    _add_common(serve_parser)
    _add_server_args(serve_parser)
    serve_parser.add_argument("model")
    serve_parser.add_argument("--gateway", action="store_true")
    serve_parser.add_argument("--gateway-port", type=int, default=8080)
    serve_parser.add_argument("--rate-limit", type=float, help="gateway requests per second")
    serve_parser.set_defaults(handler=cmd_serve)

    gateway_parser = sub.add_parser("gateway", help="run the API gateway against a llama-server")
    _add_common(gateway_parser)
    gateway_parser.add_argument("--llm-url")
    gateway_parser.add_argument("--api-keys")
    gateway_parser.add_argument("--rate-limit", type=float, default=100.0)
    gateway_parser.add_argument("--host", default="127.0.0.1")
    gateway_parser.add_argument("--port", type=int, default=8080)
    gateway_parser.set_defaults(handler=cmd_gateway)

    bench_parser = sub.add_parser("bench", help="measure TTFT, ITL, and throughput")
    _add_common(bench_parser)
    _add_server_args(bench_parser)
    bench_parser.add_argument("model")
    bench_parser.add_argument("--url", help="benchmark an already running OpenAI-compatible server")
    bench_parser.add_argument("--concurrency", type=int, nargs="+", default=[1, 2, 4])
    bench_parser.add_argument("--requests", type=int, default=8)
    bench_parser.add_argument("--warmup", type=int, default=2)
    bench_parser.add_argument("--prompt-tokens", dest="prompt_tokens", type=int, default=256)
    bench_parser.add_argument("--max-tokens", dest="max_tokens", type=int, default=128)
    bench_parser.add_argument("--temperature", type=float, default=0.0)
    bench_parser.add_argument("--timeout", type=float, default=180.0)
    bench_parser.set_defaults(handler=cmd_bench)

    tune_parser = sub.add_parser("tune", help="sweep llama-bench configurations")
    _add_common(tune_parser)
    tune_parser.add_argument("model")
    tune_parser.add_argument(
        "--device", choices=[item.value for item in Device], default=Device.CPU.value
    )
    tune_parser.add_argument("--quants", nargs="+")
    tune_parser.add_argument("--threads", type=int, nargs="+")
    tune_parser.add_argument("--pp", type=int, default=512)
    tune_parser.add_argument("--tg", type=int, default=128)
    tune_parser.add_argument("--reps", type=int, default=3)
    tune_parser.add_argument("--timeout", type=float, default=300.0)
    tune_parser.set_defaults(handler=cmd_tune)

    eval_parser = sub.add_parser("eval", help="score the prompt suite")
    _add_common(eval_parser)
    _add_server_args(eval_parser)
    eval_parser.add_argument("model")
    eval_parser.add_argument("--suite", default=str(ROOT / "prompts" / "core.jsonl"))
    eval_parser.add_argument("--url")
    eval_parser.set_defaults(handler=cmd_eval)

    train_parser = sub.add_parser("train", help="fine-tune an F32 GGUF on a text corpus")
    _add_common(train_parser)
    train_parser.add_argument("model")
    train_parser.add_argument("--corpus", default=str(ROOT / "data" / "corpus.txt"))
    train_parser.add_argument("--epochs", type=int, default=1)
    train_parser.add_argument("--ctx-size", dest="ctx_size", type=int, default=512)
    train_parser.add_argument("--gpu-layers", dest="gpu_layers", type=int, default=0)
    train_parser.add_argument("--out")
    train_parser.add_argument("--timeout", type=float, default=1800.0)
    train_parser.set_defaults(handler=cmd_train)

    kernel_parser = sub.add_parser("kernel", help="run the HIP copy and GEMM benchmark")
    _add_common(kernel_parser)
    kernel_parser.add_argument("--binary")
    kernel_parser.add_argument("--blocks", type=int, nargs="+", default=[128, 256, 512, 1024])
    kernel_parser.add_argument("--sizes", type=int, nargs="+", default=[1024, 2048, 4096])
    kernel_parser.add_argument("--dtypes", nargs="+", default=["fp16", "fp32"])
    kernel_parser.add_argument("--layouts", nargs="+", default=["nn", "nt", "tn"])
    kernel_parser.add_argument("--timeout", type=float, default=600.0)
    kernel_parser.set_defaults(handler=cmd_kernel)

    results_parser = sub.add_parser("results", help="list, show, and compare recorded runs")
    _add_common(results_parser)
    results_sub = results_parser.add_subparsers(dest="action", required=True)
    results_list = results_sub.add_parser("list")
    _add_common(results_list)
    results_list.set_defaults(handler=cmd_results)
    results_show = results_sub.add_parser("show")
    _add_common(results_show)
    results_show.add_argument("selector")
    results_show.set_defaults(handler=cmd_results)
    results_compare = results_sub.add_parser("compare")
    _add_common(results_compare)
    results_compare.add_argument("left")
    results_compare.add_argument("right")
    results_compare.set_defaults(handler=cmd_results)

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)
    try:
        return int(args.handler(args))
    except BlindingError as exc:
        print(f"blinding: {exc.message}", file=sys.stderr)
        return exc.exit_code
    except KeyboardInterrupt:
        return 130
