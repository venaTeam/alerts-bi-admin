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
#: Must keep its default: it is the header the OpenShift login proxy sends.
_FIXED = {"ADMIN_USER_HEADER"}
#: Legacy trigger listener settings; `alerts-bi-admin serve` does not read them.
_LEGACY = {"API_HOST", "API_PORT", "API_REGISTRY_PATH", "API_DATABASE"}
#: Read by libraries rather than through read_*: the log threshold and the model client's CA file.
_LIBRARY = {"LOG_LEVEL", "SSL_CERT_FILE"}
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
    "alerts-bi-admin-tls",
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
    assert read - documented == _TEST_ONLY | _TEMPLATE_OWNED | _FIXED | _LEGACY


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
        assert set(images) <= {'"${ADMIN_IMAGE}"', '"${OAUTH_PROXY_IMAGE}"'}, name
    objects, _ = _template("admin.yaml")
    # migrate, admin, run-worker and the weekly CronJob.
    assert objects.count('image: "${ADMIN_IMAGE}"') == 4


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
