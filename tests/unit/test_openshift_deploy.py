"""The OpenShift templates, their value files and the settings the code reads stay in step.

A deploying agent fills only the example files in deploy/openshift, so every template parameter
must be in values.env.example with the template's own default, and every setting the
application reads must be in config.env.example or secrets.env.example.
"""

from __future__ import annotations

import re
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

import alerts_bi_shared.config.sql as sql_config
import alerts_bi_shared.db.connection as connection
import pytest

ROOT = Path(__file__).resolve().parents[2]
DEPLOY = ROOT / "deploy" / "openshift"

# The substitution syntax of `oc process`; `${NAME:-default}` shell expansion is not a parameter.
_STRING_PARAMETER = re.compile(r"\$\{([a-zA-Z0-9_]+?)\}")
_VALUE_PARAMETER = re.compile(r"\$\{\{([a-zA-Z0-9_]+)\}\}")
_SETTING_READ = re.compile(r"""read_(?:str|int|bool)\(\s*["']([A-Z][A-Z0-9_]*)["']""")
#: Read only by tests or by the optional legacy `db grant-reader` utility, never by a deployment.
_TEST_ONLY = {"SQL_TEST_DATABASE", "LLM_LIVE_TEST", "PORTAL_SQL_USER", "PORTAL_SQL_PASSWORD"}
#: Set by the templates themselves, or forbidden in a deployment.
_TEMPLATE_OWNED = {
    "ADMIN_HOST",
    "ADMIN_PORT",
    "ADMIN_SECRET",
    "ADMIN_REGISTRY_PATH",
    "ADMIN_DATABASE",
}
#: Legacy trigger listener settings; `alerts-bi-admin serve` does not read them.
_LEGACY = {"API_HOST", "API_PORT", "API_REGISTRY_PATH", "API_DATABASE"}
#: Read by libraries rather than through read_*: the log settings and the model client's CA file.
_LIBRARY = {"LOG_LEVEL", "LOG_FORMAT", "LOG_FORWARD_URL", "LOG_FORWARD_LEVEL", "SSL_CERT_FILE"}
#: How a template names a ConfigMap or Secret it reads.
_OBJECT_REFERENCE = re.compile(
    r"(?:(?:configMapRef|secretRef|configMap|secretKeyRef): \{name"
    r"|secretName|serving-cert-secret-name): (alerts-bi-[a-z-]+)"
)
_CLUSTER_OBJECTS = {
    "alerts-bi-config",
    "alerts-bi-secrets",
    "alerts-bi-registry",
    "alerts-bi-ca-bundle",
    "alerts-bi-admin-secrets",
}


def _template(name: str) -> tuple[str, dict[str, dict[str, str]]]:
    """The objects text and the declared parameters, with their fields, of one template."""
    text = (DEPLOY / name).read_text("utf-8")
    objects, separator, declared = text.partition("\nparameters:\n")
    assert separator, f"{name} has no parameters section"
    parameters: dict[str, dict[str, str]] = {}
    for block in ("\n" + declared).split("\n  - name: ")[1:]:
        lines = block.splitlines()
        fields = dict(line.strip().split(": ", 1) for line in lines[1:] if line.strip())
        parameters[lines[0].strip()] = fields
    return objects, parameters


def _references(objects: str) -> set[str]:
    return set(_STRING_PARAMETER.findall(objects)) | set(_VALUE_PARAMETER.findall(objects))


def _env_example(name: str, *, include_commented: bool = False) -> dict[str, str]:
    values: dict[str, str] = {}
    for line in (DEPLOY / name).read_text("utf-8").splitlines():
        if include_commented and line.startswith("# ") and re.match(r"# [A-Z][A-Z0-9_]*=", line):
            line = line[2:]
        if not line or line.startswith("#"):
            continue
        key, _, value = line.partition("=")
        values[key] = value
    return values


def _default(fields: dict[str, str]) -> str:
    value = fields.get("value", "")
    return value[1:-1] if len(value) > 1 and value[0] == value[-1] == '"' else value


def _settings_read_by_code() -> set[str]:
    names: set[str] = set()
    for folder in (ROOT / "src", ROOT / "packages"):
        for source in folder.rglob("*.py"):
            names.update(_SETTING_READ.findall(source.read_text("utf-8")))
    return names


def test_templates_declare_exactly_the_parameters_they_use() -> None:
    for name in ("admin.yaml", "admin-job.yaml"):
        objects, parameters = _template(name)
        assert _references(objects) == set(parameters), name


def test_values_example_holds_every_parameter_with_the_template_default() -> None:
    values = _env_example("values.env.example")
    _, admin = _template("admin.yaml")
    _, job = _template("admin-job.yaml")
    assert set(values) == set(admin)
    assert set(job) - set(values) == {"JOB_NAME", "JOB_COMMAND"}
    for parameters in (admin, job):
        for key, fields in parameters.items():
            if key in values:
                expected = "" if fields.get("required") == "true" else _default(fields)
                assert values[key] == expected, key


def test_the_shared_job_parameters_have_the_same_defaults() -> None:
    _, admin = _template("admin.yaml")
    _, job = _template("admin-job.yaml")
    for key in set(admin) & set(job):
        assert _default(admin[key]) == _default(job[key]), key
        assert admin[key].get("required") == job[key].get("required"), key


def test_config_and_secret_examples_cover_exactly_the_runtime_settings() -> None:
    config = _env_example("config.env.example", include_commented=True)
    secrets = _env_example("secrets.env.example")
    assert not set(config) & set(secrets)
    read = _settings_read_by_code()
    documented = set(config) | set(secrets)
    assert documented - read == _LIBRARY
    assert read - documented == _TEST_ONLY | _TEMPLATE_OWNED | _LEGACY


def test_env_files_follow_the_from_env_file_format() -> None:
    # `oc create --from-env-file` keeps quotes, "#" and trailing spaces as part of the value.
    for name in ("config.env.example", "secrets.env.example"):
        for line in (DEPLOY / name).read_text("utf-8").splitlines():
            if not line or line.startswith("#"):
                continue
            assert re.fullmatch(r"[A-Z][A-Z0-9_]*=[^\"'#\s]*", line), line


def test_every_admin_container_runs_the_one_admin_image() -> None:
    for name in ("admin.yaml", "admin-job.yaml"):
        objects, _ = _template(name)
        images = re.findall(r"^\s*image: (.+)$", objects, flags=re.MULTILINE)
        assert set(images) <= {'"${ADMIN_IMAGE}"'}, name
    objects, _ = _template("admin.yaml")
    # migrate, admin, run-worker and the weekly CronJob.
    assert objects.count('image: "${ADMIN_IMAGE}"') == 4


def test_the_console_has_no_login_proxy_or_client_allowlist() -> None:
    # Design "Admin console without login", 2026-10-19.
    objects, _ = _template("admin.yaml")
    for absent in ("oauth-proxy", "openshift-sar", "kind: Role", "ip_allowlist", "ip_whitelist"):
        assert absent not in objects, absent
    assert '{name: ADMIN_HOST, value: "0.0.0.0"}' in objects
    assert "tls: {termination: edge, insecureEdgeTerminationPolicy: Redirect}" in objects


def test_the_init_container_runs_the_integration_tested_setup_command() -> None:
    objects, _ = _template("admin.yaml")
    commands = re.findall(r"- '(exec alerts-bi-admin db setup[^\n]+)'", objects)
    assert commands == [
        'exec alerts-bi-admin db setup --database "${ADMIN_DATABASE:-${SQL_DATABASE:-alerts_bi_dev}}"'
    ]


def test_templates_reference_only_the_documented_cluster_objects() -> None:
    runbook = (DEPLOY / "README.md").read_text("utf-8")
    for name in ("admin.yaml", "admin-job.yaml"):
        objects, _ = _template(name)
        referenced = set(_OBJECT_REFERENCE.findall(objects))
        assert referenced, name
        assert referenced <= _CLUSTER_OBJECTS, name
    for cluster_object in _CLUSTER_OBJECTS:
        assert f"`{cluster_object}`" in runbook, cluster_object


def test_the_drain_check_queries_while_its_connection_is_open(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    # The runbook's `oc exec ... python -c` snippet must hold the connection open around its
    # query; a dropped context manager closes it before the query under CPython.
    runbook = (DEPLOY / "README.md").read_text("utf-8")
    match = re.search(r"-c run-worker -- python -c '\n(.*?)\n'\n```", runbook, flags=re.DOTALL)
    assert match is not None, "the drain check snippet"
    queries: list[str] = []

    class FakeDatabase:
        open = True

        def query(self, sql: str) -> list[dict[str, Any]]:
            assert self.open, "the query ran on a closed connection"
            queries.append(sql)
            return [{"status": "completed", "jobs": 3}]

    @contextmanager
    def fake_connect(config: object, database: str | None = None) -> Iterator[FakeDatabase]:
        db = FakeDatabase()
        try:
            yield db
        finally:
            db.open = False

    monkeypatch.setattr(connection, "connect", fake_connect)
    monkeypatch.setattr(sql_config, "load_sql_config", lambda: object())
    exec(compile(match.group(1), "deploy/openshift/README.md", "exec"), {})
    assert len(queries) == 1 and "FROM run_jobs" in queries[0]
    assert "'completed'" in capsys.readouterr().out


# ---------------------------------------------------------------- the optional log collector

_LOGGING_OBJECTS = {"alerts-bi-logs-config", "alerts-bi-logs-es", "alerts-bi-ca-bundle"}


def _fluent_bit_sections() -> list[dict[str, str]]:
    sections: list[dict[str, str]] = []
    for line in (DEPLOY / "fluent-bit.conf").read_text("utf-8").splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        if stripped.startswith("["):
            sections.append({"_section": stripped.strip("[]")})
        else:
            key, _, value = stripped.partition(" ")
            sections[-1][key] = value.strip()
    return sections


def test_the_logging_template_and_its_values_example_agree() -> None:
    objects, parameters = _template("logging.yaml")
    assert _references(objects) == set(parameters)
    values = _env_example("logging-values.env.example")
    assert set(values) == set(parameters)
    for key, fields in parameters.items():
        expected = "" if fields.get("required") == "true" else _default(fields)
        assert values[key] == expected, key
    assert re.findall(r"^\s*image: (.+)$", objects, flags=re.MULTILINE) == ['"${LOGS_IMAGE}"']


def test_the_collector_reads_only_documented_objects_and_settings() -> None:
    objects, _ = _template("logging.yaml")
    referenced = set(_OBJECT_REFERENCE.findall(objects))
    assert referenced == _LOGGING_OBJECTS
    runbook = (DEPLOY / "README.md").read_text("utf-8")
    for cluster_object in _LOGGING_OBJECTS | {"alerts-bi-logs"}:
        assert f"`{cluster_object}`" in runbook, cluster_object
    # Every ${VAR} in fluent-bit.conf is set by the Template or the Secret, and nothing else.
    used = set(re.findall(r"\$\{([A-Z_]+)\}", (DEPLOY / "fluent-bit.conf").read_text("utf-8")))
    from_template = set(re.findall(r"\{name: (LOGS_[A-Z_]+), value:", objects))
    from_secret = set(_env_example("logs-es.env.example"))
    assert not from_template & from_secret
    assert used == from_template | from_secret


def test_only_the_alerts_bi_pods_may_reach_the_collector() -> None:
    # The HTTP input has no authentication: the NetworkPolicy admits exactly the pods that
    # send records (admin console and worker, weekly CronJob, one-off Jobs, portal).
    objects, _ = _template("logging.yaml")
    policy = objects[objects.index("kind: NetworkPolicy") :]
    assert "podSelector: {matchLabels: {app: alerts-bi-logs}}" in policy
    assert "policyTypes: [Ingress]" in policy
    (allowed,) = re.findall(r"values: \[([^\]]+)\]", policy)
    senders = {
        label
        for name in ("admin.yaml", "admin-job.yaml")
        for label in re.findall(
            r"labels: \{app: (alerts-bi-[a-z-]+),", (DEPLOY / name).read_text("utf-8")
        )
    }
    # The portal's Deployment lives in alerts-bi-portal; its pods are labelled alerts-bi-portal.
    assert {value.strip() for value in allowed.split(",")} == senders | {"alerts-bi-portal"}
    assert re.findall(r"port: (\d+)\}", policy) == ["9880", "2020"]
    runbook = (DEPLOY / "README.md").read_text("utf-8")
    assert (
        "networkpolicy/alerts-bi-logs" in runbook and "NetworkPolicy\n`alerts-bi-logs`" in runbook
    )


def test_the_secret_example_follows_the_from_env_file_format() -> None:
    for line in (DEPLOY / "logs-es.env.example").read_text("utf-8").splitlines():
        if line and not line.startswith("#"):
            assert re.fullmatch(r"[A-Z][A-Z0-9_]*=[^\"'#\s]*", line), line


def test_each_stream_has_its_own_output_and_the_apps_post_to_the_service() -> None:
    from alerts_bi_shared.logging_setup import STREAMS

    sections = _fluent_bit_sections()
    (http_input,) = [s for s in sections if s["_section"] == "INPUT"]
    assert (http_input["Name"], http_input["Port"]) == ("http", "9880")
    outputs = [s for s in sections if s["_section"] == "OUTPUT"]
    assert [s["Match"] for s in outputs] == list(STREAMS)
    for output in outputs:
        assert output["Name"] == "es"
        assert output["Logstash_Prefix"] == "${LOGS_INDEX_PREFIX}-" + output["Match"]
        # The records carry @timestamp; Fluent Bit must not write a second one.
        assert output["Time_Key"] != "@timestamp"
    objects, _ = _template("logging.yaml")
    assert "{name: http, port: 9880, targetPort: 9880}" in objects
    config = _env_example("config.env.example", include_commented=True)
    assert config["LOG_FORWARD_URL"] == "http://alerts-bi-logs:9880"
