"""Elasticsearch connection settings.

Elasticsearch is the sole alert source (design section 3.1) and is read-only to this
pipeline: nothing here can write to a cluster.
"""

from __future__ import annotations

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


def load_es_config() -> EsConfig:
    return EsConfig(
        url=read_str("ES_URL", "http://localhost:9200").rstrip("/"),
        username=read_str("ES_USERNAME"),
        password=read_str("ES_PASSWORD"),
        # Path to a private CA bundle; the Elasticsearch client takes it directly.
        ca_cert=read_str("ES_CA_CERT"),
        request_timeout_ms=read_int("ES_REQUEST_TIMEOUT_MS", 60000),
        page_size=read_int("ES_PAGE_SIZE", 1000),
    )
