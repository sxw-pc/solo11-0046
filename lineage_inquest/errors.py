"""Typed user-facing errors for the CLI and loaders."""


class InquestError(Exception):
    """Base class for expected, user-facing failures."""


class ConfigError(InquestError):
    """Configuration, path, or schema validation failure."""


class NotFoundError(InquestError):
    """A requested run, node, or finding does not exist."""


class VerifyError(InquestError):
    """The verification snapshot did not match."""
