"""Elasticsearch connection settings.

Elasticsearch is the sole alert source (design section 3.1) and is read-only to this
pipeline: nothing here can write to a cluster.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from alerts_bi_shared.config.env import read_int, read_str

__all__ = ["EsConfig", "load_es_config"]


@dataclass(frozen=True, slots=True)
class EsConfig:
    url: str
    username: str
    password: str
    ca_cert: str
    request_timeout_ms: int
    page_size: int
    v1_index: str = "appchi-v1"
    v2_index: str = "appchi-v2"
    pit_keep_alive: str = "2m"

    def __post_init__(self) -> None:
        for name, value in (("ES_V1_INDEX", self.v1_index), ("ES_V2_INDEX", self.v2_index)):
            if not value.strip() or any(char.isspace() for char in value):
                raise ValueError(
                    f"{name} must be a nonempty index name or expression without whitespace"
                )
        if not re.fullmatch(r"[1-9][0-9]*(?:ms|s|m|h|d)", self.pit_keep_alive):
            raise ValueError("ES_PIT_KEEP_ALIVE must be a positive duration such as 2m or 120s")


def load_es_config() -> EsConfig:
    return EsConfig(
        url=read_str("ES_URL", "http://localhost:9200").rstrip("/"),
        username=read_str("ES_USERNAME"),
        password=read_str("ES_PASSWORD"),
        # Path to a private CA bundle; the Elasticsearch client takes it directly.
        ca_cert=read_str("ES_CA_CERT"),
        request_timeout_ms=read_int("ES_REQUEST_TIMEOUT_MS", 60000),
        page_size=read_int("ES_PAGE_SIZE", 1000),
        v1_index=read_str("ES_V1_INDEX", "appchi-v1"),
        v2_index=read_str("ES_V2_INDEX", "appchi-v2"),
        pit_keep_alive=read_str("ES_PIT_KEEP_ALIVE", "2m"),
    )
