from app.config import Settings


def test_settings_defaults():
    s = Settings(openrouter_api_key="k", database_url="postgresql://x")
    assert s.sandbox_run_timeout_seconds == 1800
    assert s.sandbox_exec_timeout_seconds == 110
    assert s.model_coding_agent == "anthropic/claude-sonnet-4.5"
    assert s.test_command == "pytest -q"