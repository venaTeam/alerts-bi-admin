"""Configuration for the service.

Split by what each block configures rather than kept in one file, so a setting is added
next to the thing that needs it:

* :mod:`~alerts_bi_shared.config.env` - reading the environment and a ``.env`` file
* :mod:`~alerts_bi_admin.engine.config.elasticsearch`, :mod:`~alerts_bi_shared.config.sql`,
  :mod:`~alerts_bi_admin.engine.config.llm` - the three backing services, each validating its own values
* :mod:`~alerts_bi_admin.engine.config.app` - the pipeline's configuration, composing those three
* :mod:`~alerts_bi_admin.engine.config.api` - the HTTP surface: where it listens, what it writes

Everything is re-exported here, so ``from alerts_bi_admin.engine.config import load_config`` is still the
one import a caller needs.
"""

from __future__ import annotations

from alerts_bi_shared.config.env import load_dotenv, read_bool, read_int, read_str
from alerts_bi_shared.config.sql import SqlConfig, load_sql_config

from .api import (
    DEFAULT_HOST,
    DEFAULT_PORT,
    ApiSettings,
    load_api_settings,
)
from .app import AppConfig, load_config
from .elasticsearch import EsConfig, load_es_config
from .llm import MAX_BATCH_SIZE_CEILING, LlmConfig, load_llm_config

__all__ = [
    "DEFAULT_HOST",
    "DEFAULT_PORT",
    "MAX_BATCH_SIZE_CEILING",
    "ApiSettings",
    "AppConfig",
    "EsConfig",
    "LlmConfig",
    "SqlConfig",
    "load_api_settings",
    "load_config",
    "load_dotenv",
    "load_es_config",
    "load_llm_config",
    "load_sql_config",
    "read_bool",
    "read_int",
    "read_str",
]
