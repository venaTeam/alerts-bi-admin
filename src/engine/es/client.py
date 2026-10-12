"""Elasticsearch access, built on the official Python client.

TLS note: for an on-prem cluster with a private CA, set ``ES_CA_CERT`` to the bundle path;
the client takes it directly. That is a change from the superseded JavaScript
implementation, where ``fetch`` had no per-request CA option and the setting had to be
supplied through ``NODE_EXTRA_CA_CERTS`` instead.
"""

from __future__ import annotations

import contextlib
from typing import Any

from elasticsearch import ApiError, Elasticsearch, NotFoundError, TransportError

from ..config import EsConfig

__all__ = ["ElasticsearchError", "EsClient", "build_client"]


class ElasticsearchError(Exception):
    """Any failure reaching or reading the cluster."""

    def __init__(self, message: str, status: int | None = None) -> None:
        super().__init__(message)
        self.status = status


def build_client(config: EsConfig) -> Elasticsearch:
    """Construct the official client from configuration."""
    kwargs: dict[str, Any] = {
        "hosts": [config.url],
        "request_timeout": config.request_timeout_ms / 1000,
        # The pipeline owns retry policy where it matters; a silent client-side retry of a
        # paging request could duplicate rows.
        "max_retries": 0,
        "retry_on_timeout": False,
        "http_compress": True,
    }
    if config.username:
        kwargs["basic_auth"] = (config.username, config.password)
    if config.ca_cert:
        kwargs["ca_certs"] = config.ca_cert
    return Elasticsearch(**kwargs)


class EsClient:
    """Thin wrapper exposing exactly the three operations the pipeline performs."""

    def __init__(self, config: EsConfig, client: Elasticsearch | None = None) -> None:
        self.config = config
        self.client = client if client is not None else build_client(config)

    def index_exists(self, index: str) -> bool:
        try:
            return bool(self.client.indices.exists(index=index))
        except (ApiError, TransportError):
            return False

    def open_pit(self, index: str, keep_alive: str = "2m") -> str:
        """Open a point-in-time so pagination sees one consistent view of the index.

        Without a PIT, ``from``/``size`` paging over a live index can skip or repeat
        documents as segments merge - which would make ``alerts`` and ``distinct_alerts``
        depend on how busy the cluster was during the run.
        """
        try:
            response = self.client.open_point_in_time(index=index, keep_alive=keep_alive)
        except NotFoundError as exc:
            raise ElasticsearchError(f"index {index} does not exist", 404) from exc
        except (ApiError, TransportError) as exc:
            raise ElasticsearchError(f"could not open a point-in-time on {index}: {exc}") from exc
        pit_id = response.get("id")
        if not pit_id:
            raise ElasticsearchError(f"could not open a point-in-time on {index}")
        return str(pit_id)

    def close_pit(self, pit_id: str) -> None:
        """Close a point-in-time, tolerating failure.

        A leaked PIT expires on its own; failing the run over cleanup would discard a
        complete, correct result.
        """
        with contextlib.suppress(ApiError, TransportError):
            self.client.close_point_in_time(id=pit_id)

    def search(self, body: dict[str, Any]) -> dict[str, Any]:
        try:
            return dict(self.client.search(**body))
        except (ApiError, TransportError) as exc:
            status = getattr(exc, "status_code", None)
            raise ElasticsearchError(f"search failed: {exc}", status) from exc

    def close(self) -> None:
        self.client.close()
