"""Configuration for the operator admin app (design section 7.12).

The admin app writes - it runs analyses, publishes, withdraws, records decisions and deletes
runs - with the owning SQL credential. It has no login (design "Admin console without login",
2026-10-19): anyone who can reach its address may use it, so where it listens is the only access
boundary. It binds to loopback by default; a deployment sets ADMIN_HOST=0.0.0.0 so its Service
and Route can reach it.
"""

from __future__ import annotations

from dataclasses import dataclass, replace

from alerts_bi_shared.config.env import load_dotenv, read_int, read_str
from alerts_bi_shared.config.sql import SqlConfig, load_sql_config

from .engine.config import ApiSettings
from .engine.config import load_config as load_runtime_config

__all__ = [
    "DEFAULT_ADMIN_PORT",
    "AdminConfig",
    "AdminSettings",
    "load_admin_settings",
    "load_config",
]

DEFAULT_ADMIN_PORT = 8200
_MIN_SECRET = 32


@dataclass(frozen=True, slots=True)
class AdminConfig:
    """The operator application needs only the shared SQL connection."""

    sql: SqlConfig


def load_config() -> AdminConfig:
    load_dotenv()
    return AdminConfig(sql=load_sql_config())


@dataclass(frozen=True, slots=True)
class AdminSettings:
    config: AdminConfig
    database: str
    #: Signs the anti-forgery token on every form. From ``ADMIN_SECRET``.
    secret: str
    host: str = "127.0.0.1"
    port: int = DEFAULT_ADMIN_PORT
    registry_path: str | None = None

    def api_settings(self) -> ApiSettings:
        """Use the local execution engine in-process and preserve the admin SQL override."""
        config = replace(load_runtime_config(), sql=self.config.sql)
        return ApiSettings(
            config,
            host=self.host,
            port=self.port,
            database=self.database,
            registry_path=self.registry_path,
        )

    def __post_init__(self) -> None:
        if len(self.secret) < _MIN_SECRET:
            raise ValueError(
                f"ADMIN_SECRET must be at least {_MIN_SECRET} characters; it signs every form"
            )


def load_admin_settings(
    config: AdminConfig | None = None,
    port: int | None = None,
    database: str | None = None,
    registry_path: str | None = None,
) -> AdminSettings:
    load_dotenv()
    resolved = config or load_config()
    return AdminSettings(
        config=resolved,
        database=database or read_str("ADMIN_DATABASE") or resolved.sql.database,
        secret=read_str("ADMIN_SECRET"),
        host=read_str("ADMIN_HOST", "127.0.0.1"),
        port=port or read_int("ADMIN_PORT", DEFAULT_ADMIN_PORT),
        registry_path=registry_path or (read_str("ADMIN_REGISTRY_PATH") or None),
    )
