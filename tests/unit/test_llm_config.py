from pathlib import Path

import pytest
from pydantic import SecretStr, ValidationError

from app.core.config.settings import Settings
from app.shared.errors import AppError
from app.shared.integrations.llm.config import LLMSettings, ProviderType

EXAMPLE = Path(__file__).parents[2] / "config" / "llm.example.yml"

YAML = """
providers:
  openrouter:
    type: openrouter
    api_key_env: OPENROUTER_API_KEY
    timeout_seconds: 30
  local:
    type: openai
    base_url: http://localhost:11434/v1
    api_key_env: LOCAL_KEY
budget_seconds: 90
routes:
  - { provider: openrouter, model: z-ai/glm-5.3-flash, reasoning_effort: low }
  - { provider: local, model: qwen }
"""


def _write(tmp_path: Path, text: str) -> Path:
    path = tmp_path / "llm.yml"
    path.write_text(text, encoding="utf-8")
    return path


def test_yaml_declares_providers_and_ordered_routes(tmp_path: Path) -> None:
    env = {"OPENROUTER_API_KEY": "sk-or"}

    settings = LLMSettings.from_yaml(_write(tmp_path, YAML), read_env=env.get)

    openrouter, local = settings.providers["openrouter"], settings.providers["local"]
    assert openrouter.type is ProviderType.OPENROUTER
    assert openrouter.api_key is not None and openrouter.api_key.get_secret_value() == "sk-or"
    assert openrouter.timeout_seconds == 30
    assert local.api_key is None  # LOCAL_KEY unset: reported when the provider is built
    assert [route.label for route in settings.routes] == [
        "openrouter/z-ai/glm-5.3-flash",
        "local/qwen",
    ]
    assert settings.routes[0].reasoning_effort == "low"
    assert settings.budget_seconds == 90


def test_inline_api_keys_are_rejected(tmp_path: Path) -> None:
    text = YAML.replace("api_key_env: OPENROUTER_API_KEY", "api_key: sk-leaked")

    with pytest.raises(AppError, match="inline api_key"):
        LLMSettings.from_yaml(_write(tmp_path, text), read_env=lambda _: None)


def test_routes_must_use_declared_providers(tmp_path: Path) -> None:
    text = YAML.replace("provider: local", "provider: anthropic")

    with pytest.raises(ValidationError, match="undeclared provider 'anthropic'"):
        LLMSettings.from_yaml(_write(tmp_path, text), read_env=lambda _: None)


@pytest.mark.parametrize(
    "broken",
    [
        YAML.replace("type: openai", "type: gemini"),  # unknown provider type
        YAML.replace("timeout_seconds: 30", "timeout_secs: 30"),  # typo
        YAML.replace("budget_seconds: 90", "budget_seconds: 0"),
    ],
)
def test_invalid_files_fail_at_load_time(tmp_path: Path, broken: str) -> None:
    with pytest.raises(ValidationError):
        LLMSettings.from_yaml(_write(tmp_path, broken), read_env=lambda _: None)


def test_the_example_file_is_valid() -> None:
    settings = LLMSettings.from_yaml(EXAMPLE, read_env=lambda _: None)

    assert settings.routes[0].label == "openrouter/z-ai/glm-5.3-flash"


def test_without_a_file_the_llm_env_vars_form_a_single_route() -> None:
    settings = Settings(
        llm_provider="openai",
        llm_model="gpt-x",
        llm_api_key=SecretStr("sk-test"),
        llm_reasoning_effort="low",
        llm_max_retries=4,
        llm_circuit_breaker_failure_threshold=3,
        _env_file=None,  # type: ignore[call-arg]
    ).llm_settings()

    [provider] = settings.providers.values()
    assert provider.type is ProviderType.OPENAI
    assert provider.max_retries == 4
    assert provider.circuit_breaker.failure_threshold == 3
    [route] = settings.routes
    assert (route.label, route.reasoning_effort) == ("openai/gpt-x", "low")


def test_settings_load_the_file_and_resolve_keys_from_the_environment(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-env")

    settings = Settings(
        llm_config_file=_write(tmp_path, YAML),
        _env_file=None,  # type: ignore[call-arg]
    ).llm_settings()

    key = settings.providers["openrouter"].api_key
    assert key is not None and key.get_secret_value() == "sk-env"
