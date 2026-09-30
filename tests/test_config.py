from app.config import Settings


def test_settings_defaults():
    s = Settings(openrouter_api_key="k", database_url="postgresql://x")
    assert s.sandbox_run_timeout_seconds == 1800
    assert s.sandbox_exec_timeout_seconds == 110
    assert s.test_command == "python -m pytest -q"


def test_github_phase2_settings_defaults():
    from app.config import Settings
    s = Settings(github_pat="p", github_webhook_secret="s")
    assert s.github_webhook_secret == "s"
    assert s.merge_pr_when_ready is False


def test_vm_cost_settings_default():
    from app.config import Settings
    s = Settings(github_pat="p")
    assert s.vm_cost_per_hour == 0.0