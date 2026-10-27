from __future__ import annotations

import os
from pathlib import Path

import pytest
from alerts_bi_shared.logging_setup import redact_error
from src.engine.config import MAX_BATCH_SIZE_CEILING, load_config, load_dotenv

ENV_NAMES = [
    "ES_URL",
    "ES_USERNAME",
    "ES_PASSWORD",
    "ES_CA_CERT",
    "ES_PAGE_SIZE",
    "ES_REQUEST_TIMEOUT_MS",
    "ES_V1_INDEX",
    "ES_V2_INDEX",
    "ES_PIT_KEEP_ALIVE",
    "ES_SEARCH_RETRIES",
    "ES_SEARCH_RETRY_BACKOFF_MS",
    "SQL_HOST",
    "SQL_PORT",
    "SQL_USER",
    "SQL_PASSWORD",
    "SQL_DATABASE",
    "SQL_TEST_DATABASE",
    "SQL_ENCRYPT",
    "SQL_TRUST_SERVER_CERTIFICATE",
    "LLM_ENABLED",
    "LLM_BASE_URL",
    "LLM_API_KEY",
    "LLM_MODEL",
    "LLM_MODEL_REVISION",
    "LLM_MAX_COMPLETION_TOKENS",
    "LLM_TIMEOUT_MS",
    "LLM_MAX_BATCH_SIZE",
    "LLM_CONCURRENCY",
    "LLM_LIVE_TEST",
]


@pytest.fixture(autouse=True)
def clean_environment(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Start every test from a known environment and an empty working directory.

    load_config() reads .env from the working directory, so the tests run in a temporary
    directory to keep the developer's real .env out of the results.
    """
    for name in ENV_NAMES:
        monkeypatch.delenv(name, raising=False)
    monkeypatch.chdir(tmp_path)


def test_optional_model_revision_and_completion_cap(monkeypatch: pytest.MonkeyPatch) -> None:
    assert load_config().llm.max_completion_tokens is None
    monkeypatch.setenv("LLM_MODEL_REVISION", "weights-2026-09-24")
    monkeypatch.setenv("LLM_MAX_COMPLETION_TOKENS", "4096")
    assert load_config().llm.model_revision == "weights-2026-09-24"
    assert load_config().llm.max_completion_tokens == 4096
    monkeypatch.setenv("LLM_MAX_COMPLETION_TOKENS", "-1")
    with pytest.raises(ValueError, match="nonnegative"):
        load_config()


def test_defaults_are_sane_without_any_environment() -> None:
    config = load_config()
    assert config.es.url == "http://localhost:9200"
    assert config.es.page_size == 1000
    assert config.es.v1_index == "appchi-v1"
    assert config.es.v2_index == "appchi-v2"
    assert config.es.pit_keep_alive == "2m"
    assert (config.es.search_retries, config.es.search_retry_backoff_ms) == (3, 1000)
    assert config.sql.database == "alerts_bi_dev"
    assert config.sql.test_database == "alerts_bi_test"
    assert config.llm.enabled is False
    assert config.llm.max_batch_size == MAX_BATCH_SIZE_CEILING


def test_environment_overrides_defaults(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ES_URL", "https://eck.internal:9200/")
    monkeypatch.setenv("ES_PAGE_SIZE", "250")
    monkeypatch.setenv("ES_V1_INDEX", "legacy-alerts-*")
    monkeypatch.setenv("ES_V2_INDEX", "current-alerts")
    monkeypatch.setenv("ES_PIT_KEEP_ALIVE", "5m")
    monkeypatch.setenv("SQL_PORT", "1444")
    config = load_config()
    assert config.es.url == "https://eck.internal:9200", "a trailing slash is stripped"
    assert config.es.page_size == 250
    assert config.es.v1_index == "legacy-alerts-*"
    assert config.es.v2_index == "current-alerts"
    assert config.es.pit_keep_alive == "5m"
    assert config.sql.port == 1444


def test_sql_encryption_settings_default_off_and_trusting(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sql = load_config().sql
    assert (sql.encrypt, sql.trust_server_certificate) == (False, True)
    monkeypatch.setenv("SQL_ENCRYPT", "true")
    monkeypatch.setenv("SQL_TRUST_SERVER_CERTIFICATE", "false")
    sql = load_config().sql
    assert (sql.encrypt, sql.trust_server_certificate) == (True, False)


@pytest.mark.parametrize("value", ["require", "enabled", "ture", "2"])
def test_an_unknown_sql_encrypt_value_fails_instead_of_meaning_plaintext(
    monkeypatch: pytest.MonkeyPatch, value: str
) -> None:
    monkeypatch.setenv("SQL_ENCRYPT", value)
    with pytest.raises(ValueError, match="env SQL_ENCRYPT must be one of"):
        load_config()


@pytest.mark.parametrize(("value", "expected"), [("ON", True), ("1", True), ("off", False)])
def test_sql_encrypt_accepts_the_usual_flag_spellings(
    monkeypatch: pytest.MonkeyPatch, value: str, expected: bool
) -> None:
    monkeypatch.setenv("SQL_ENCRYPT", value)
    assert load_config().sql.encrypt is expected


def test_index_settings_load_from_dotenv_with_environment_precedence(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (tmp_path / ".env").write_text("ES_V1_INDEX=file-v1\nES_V2_INDEX=file-v2\n", encoding="utf-8")
    monkeypatch.setenv("ES_V2_INDEX", "env-v2")
    config = load_config()
    assert (config.es.v1_index, config.es.v2_index) == ("file-v1", "env-v2")


@pytest.mark.parametrize("name", ["ES_V1_INDEX", "ES_V2_INDEX"])
@pytest.mark.parametrize("value", [" ", "alerts v1", "alerts\n"])
def test_whitespace_index_settings_fail_at_load(
    monkeypatch: pytest.MonkeyPatch, name: str, value: str
) -> None:
    monkeypatch.setenv(name, value)
    with pytest.raises(ValueError, match=name):
        load_config()


@pytest.mark.parametrize("value", ["0m", "-1m", "forever", "5", " 2m"])
def test_invalid_pit_lifetime_fails_at_load(monkeypatch: pytest.MonkeyPatch, value: str) -> None:
    monkeypatch.setenv("ES_PIT_KEEP_ALIVE", value)
    with pytest.raises(ValueError, match="ES_PIT_KEEP_ALIVE"):
        load_config()


@pytest.mark.parametrize("name", ["ES_SEARCH_RETRIES", "ES_SEARCH_RETRY_BACKOFF_MS"])
def test_negative_search_retry_settings_fail_at_load(
    monkeypatch: pytest.MonkeyPatch, name: str
) -> None:
    monkeypatch.setenv(name, "-1")
    with pytest.raises(ValueError, match=name):
        load_config()


@pytest.mark.parametrize("value", ["1", "true", "TRUE", "yes", "on"])
def test_truthy_flags(monkeypatch: pytest.MonkeyPatch, value: str) -> None:
    monkeypatch.setenv("LLM_ENABLED", value)
    assert load_config().llm.enabled is True


@pytest.mark.parametrize("value", ["0", "false", "no", "off", "anything else"])
def test_falsey_flags(monkeypatch: pytest.MonkeyPatch, value: str) -> None:
    monkeypatch.setenv("LLM_ENABLED", value)
    assert load_config().llm.enabled is False


def test_batch_size_may_be_lowered(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("LLM_MAX_BATCH_SIZE", "50")
    assert load_config().llm.max_batch_size == 50


def test_batch_size_may_never_be_raised_above_the_design_ceiling(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("LLM_MAX_BATCH_SIZE", "201")
    with pytest.raises(ValueError, match="exceeds the design ceiling"):
        load_config()


def test_a_non_positive_batch_size_is_rejected(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("LLM_MAX_BATCH_SIZE", "0")
    with pytest.raises(ValueError, match="positive integer"):
        load_config()


def test_model_requests_go_one_at_a_time_by_default() -> None:
    assert load_config().llm.concurrency == 1


@pytest.mark.parametrize("value", ["1", "4", "8"])
def test_model_request_concurrency_accepts_one_to_eight(
    monkeypatch: pytest.MonkeyPatch, value: str
) -> None:
    monkeypatch.setenv("LLM_CONCURRENCY", value)
    assert load_config().llm.concurrency == int(value)


@pytest.mark.parametrize("value", ["0", "-1", "9"])
def test_model_request_concurrency_outside_one_to_eight_fails_at_load(
    monkeypatch: pytest.MonkeyPatch, value: str
) -> None:
    monkeypatch.setenv("LLM_CONCURRENCY", value)
    with pytest.raises(ValueError, match="LLM_CONCURRENCY must be between 1 and 8"):
        load_config()


def test_a_non_numeric_integer_setting_fails_loudly(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ES_PAGE_SIZE", "lots")
    with pytest.raises(ValueError, match="must be an integer"):
        load_config()


def test_dotenv_does_not_override_the_real_environment(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    env_file = tmp_path / ".env"
    env_file.write_text(
        "\n".join(
            [
                "# a comment",
                "",
                "ES_URL=http://from-file:9200",
                'SQL_DATABASE="quoted_value"',
                "SQL_TEST_DATABASE='single_quoted'",
                "MALFORMED_LINE_WITHOUT_EQUALS",
            ]
        ),
        encoding="utf-8",
    )
    monkeypatch.setenv("ES_URL", "http://from-environment:9200")

    load_dotenv(env_file)

    assert os.environ["ES_URL"] == "http://from-environment:9200", "real environment wins"
    assert os.environ["SQL_DATABASE"] == "quoted_value", "surrounding quotes are stripped"
    assert os.environ["SQL_TEST_DATABASE"] == "single_quoted"
    assert "MALFORMED_LINE_WITHOUT_EQUALS" not in os.environ


def test_a_missing_dotenv_file_is_not_an_error() -> None:
    load_dotenv("definitely-not-a-file.env")


def test_redact_error_is_short_and_names_the_type() -> None:
    summary = redact_error(ValueError("something went wrong " + "x" * 500))
    assert summary.startswith("ValueError: ")
    assert len(summary) <= 320
    assert redact_error(None) == "unknown error"
