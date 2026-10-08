"""Configuration for the operator admin app (design section 7.12).

The admin app writes - it publishes, withdraws and records decisions - so it runs with the
owning SQL credential and must only ever be reached through the login proxy in front of it.
It trusts that proxy's identity header, which is only safe when nothing else can reach the
app: that is why it binds to loopback and refuses any other address. In OpenShift the
oauth-proxy sidecar shares the pod's loopback; nothing outside the pod does.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from pathlib import Path

from alerts_bi_runs.config import ApiSettings
from alerts_bi_runs.config import load_config as load_runtime_config
from alerts_bi_shared.config.env import load_dotenv, read_int, read_str
from alerts_bi_shared.config.sql import SqlConfig, load_sql_config

__all__ = [
    "DEFAULT_ADMIN_PORT",
    "DEFAULT_USER_HEADER",
    "AdminConfig",
    "AdminSettings",
    "load_admin_settings",
    "load_config",
]

DEFAULT_ADMIN_PORT = 8200
#: What OpenShift's oauth-proxy passes upstream for the signed-in user.
DEFAULT_USER_HEADER = "X-Forwarded-User"
_LOOPBACK = frozenset({"127.0.0.1", "localhost", "::1"})
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
    user_header: str = DEFAULT_USER_HEADER
    #: Local development only: act as this user when no proxy header is present.
    dev_user: str | None = None
    registry_path: str | None = None
    out_root: Path = Path("out")

    def api_settings(self) -> ApiSettings:
        """Use the pinned execution engine in-process and preserve the admin SQL override."""
        config = replace(load_runtime_config(), sql=self.config.sql)
        return ApiSettings(
            config,
            host=self.host,
            port=self.port,
            database=self.database,
            registry_path=self.registry_path,
            out_root=self.out_root,
        )

    def __post_init__(self) -> None:
        if self.host not in _LOOPBACK:
            raise ValueError(
                f"the admin app must bind to loopback, not {self.host!r}: it trusts its login "
                "proxy's identity header, so only that proxy may reach it"
            )
        if len(self.secret) < _MIN_SECRET:
            raise ValueError(
                f"ADMIN_SECRET must be at least {_MIN_SECRET} characters; it signs every form"
            )


def load_admin_settings(
    config: AdminConfig | None = None,
    port: int | None = None,
    database: str | None = None,
    dev_user: str | None = None,
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
        user_header=read_str("ADMIN_USER_HEADER", DEFAULT_USER_HEADER),
        dev_user=dev_user,
        registry_path=registry_path or (read_str("ADMIN_REGISTRY_PATH") or None),
        out_root=Path(read_str("ADMIN_OUT_DIR", "out")),
    )
