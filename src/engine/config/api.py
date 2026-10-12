"""Configuration for the HTTP trigger surface (design section 7.8).

Separate from :mod:`alerts_bi_admin.engine.config.app` because these are properties of *this deployment
of the service* - where it listens, which database it writes, where it puts reports - rather
than of the backing services the pipeline talks to. The pipeline itself runs identically
whether it was started from the command line or from a request.

The surface holds no runtime state: the lock that serializes runs lives with the code that
runs them, in :mod:`alerts_bi_admin.engine.api.service`.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from alerts_bi_shared.config.env import read_int, read_str

from .app import AppConfig, load_config

__all__ = ["DEFAULT_HOST", "DEFAULT_PORT", "ApiSettings", "load_api_settings"]

#: Loopback by default. The surface has no authentication and every request triggers real
#: Elasticsearch reads and SQL writes, so widening the bind address is an explicit act.
DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8000

_LOOPBACK = frozenset({"127.0.0.1", "localhost", "::1"})


@dataclass(frozen=True, slots=True)
class ApiSettings:
    """Where this instance listens and what it writes to."""

    config: AppConfig = field(default_factory=load_config)
    host: str = DEFAULT_HOST
    port: int = DEFAULT_PORT
    #: Registry path; ``None`` means the packaged default, ``config/teams.json``.
    registry_path: str | None = None
    #: Target database; ``None`` means ``SQL_DATABASE``.
    database: str | None = None
    out_root: Path = Path("out")

    @property
    def target_database(self) -> str:
        return self.database or self.config.sql.database

    @property
    def loopback_only(self) -> bool:
        return self.host in _LOOPBACK


def load_api_settings(
    config: AppConfig | None = None,
    host: str | None = None,
    port: int | None = None,
    registry_path: str | None = None,
    database: str | None = None,
    out_root: Path | None = None,
) -> ApiSettings:
    """Read the surface's settings from the environment, with explicit overrides winning.

    Command-line flags are the overrides: a flag beats the environment, and the environment
    beats the default, which is the same precedence the rest of the configuration uses.
    """
    resolved = config or load_config()
    return ApiSettings(
        config=resolved,
        host=host or read_str("API_HOST", DEFAULT_HOST),
        port=port or read_int("API_PORT", DEFAULT_PORT),
        registry_path=registry_path or (read_str("API_REGISTRY_PATH") or None),
        database=database or (read_str("API_DATABASE") or None),
        out_root=out_root or Path(read_str("API_OUT_DIR", "out")),
    )
