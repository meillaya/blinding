"""Run the gateway with `python -m blinding.gateway`.

Command line flags win over the `BLINDING_*` environment variables, and the
environment fills every value the flags leave unset. The parsed settings go
straight to `create_app` and uvicorn binds the configured address.
"""

from __future__ import annotations

import argparse
import sys
from collections.abc import Sequence
from typing import Any

import uvicorn
from pydantic import ValidationError

from blinding.errors import EXIT_OK, EXIT_USAGE
from blinding.gateway.app import create_app
from blinding.gateway.settings import (
    DEFAULT_HOST,
    DEFAULT_PORT,
    DEFAULT_RATE_LIMIT_RPS,
    GatewaySettings,
    parse_api_keys,
)


def build_parser() -> argparse.ArgumentParser:
    """The gateway command line, with every flag optional so env can fill it."""
    parser = argparse.ArgumentParser(
        prog="python -m blinding.gateway",
        description="Serve the Blinding gateway in front of a llama.cpp server.",
    )
    parser.add_argument(
        "--host",
        help=f"listen address (env BLINDING_HOST, default {DEFAULT_HOST})",
    )
    parser.add_argument(
        "--port",
        type=int,
        help=f"listen port (env BLINDING_PORT, default {DEFAULT_PORT})",
    )
    parser.add_argument("--llm-url", help="llama.cpp base URL (env BLINDING_LLM_URL)")
    parser.add_argument(
        "--rate-limit-rps",
        type=float,
        help=(
            "token bucket refill rate per API key "
            f"(env BLINDING_RATE_LIMIT_RPS, default {DEFAULT_RATE_LIMIT_RPS:g})"
        ),
    )
    parser.add_argument(
        "--api-keys",
        help="accepted API keys as CSV or a JSON array (env BLINDING_API_KEYS)",
    )
    return parser


def settings_from_args(args: argparse.Namespace) -> GatewaySettings:
    """Overlay the flags that were given onto the environment settings."""
    overrides: dict[str, Any] = {}
    if args.host is not None:
        overrides["host"] = args.host
    if args.port is not None:
        overrides["port"] = args.port
    if args.llm_url is not None:
        overrides["llm_url"] = args.llm_url
    if args.rate_limit_rps is not None:
        overrides["rate_limit_rps"] = args.rate_limit_rps
    if args.api_keys is not None:
        overrides["api_keys"] = parse_api_keys(args.api_keys)
    return GatewaySettings(**overrides)


def main(argv: Sequence[str] | None = None) -> int:
    """Parse the command line, build the application, and hand it to uvicorn."""
    args = build_parser().parse_args(argv)
    try:
        settings = settings_from_args(args)
        app = create_app(settings)
    except (ValidationError, ValueError) as exc:
        print(f"blinding-gateway: {exc}", file=sys.stderr)
        return EXIT_USAGE

    uvicorn.run(app, host=settings.host, port=settings.port)
    return EXIT_OK


if __name__ == "__main__":
    sys.exit(main())
