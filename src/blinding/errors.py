"""Typed errors and the exit codes the command line promises.

The exit codes are part of the contract. A shell script that runs Blinding can
tell a bad config from a measurement it should not publish, and a missing model
from a backend that went away mid-run.
"""

EXIT_OK = 0
EXIT_USAGE = 2
EXIT_INVALID = 3
EXIT_MISSING = 4
EXIT_UPSTREAM = 5


class BlindingError(Exception):
    """Base error carrying the process exit code for its failure class."""

    exit_code: int = 1

    def __init__(self, message: str) -> None:
        super().__init__(message)
        self.message = message


class UsageError(BlindingError):
    """The command line or a config file is wrong."""

    exit_code = EXIT_USAGE


class ConfigError(UsageError):
    """A server config or manifest field failed validation."""


class ManifestError(UsageError):
    """The model manifest is malformed or names a model that does not exist."""


class InvalidMeasurement(BlindingError):
    """A run produced data that must not be reported as a result."""

    exit_code = EXIT_INVALID


class MissingDependency(BlindingError):
    """A required binary, model file, or device is absent."""

    exit_code = EXIT_MISSING


class UpstreamError(BlindingError):
    """The inference server answered with an error or disappeared."""

    exit_code = EXIT_UPSTREAM
