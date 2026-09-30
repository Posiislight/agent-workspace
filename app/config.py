from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    openrouter_api_key: str = ""
    openrouter_base_url: str = "https://openrouter.ai/api/v1"
    maritime_api_key: str = ""
    maritime_base_url: str = "https://api.maritime.sh"
    maritime_template_id: str = ""
    templates_allowlist: tuple[str, ...] = ("codex", "dsh")
    github_pat: str = ""
    github_webhook_secret: str = ""
    merge_pr_when_ready: bool = False
    vm_cost_per_hour: float = 0.0

    database_url: str = "postgresql://aw:aw@localhost:5433/agent_workspace"
    redis_url: str = "redis://localhost:6380/0"

    sandbox_run_timeout_seconds: int = 1800
    sandbox_exec_timeout_seconds: int = 110
    sandbox_poll_interval_seconds: float = 2.0
    test_command: str = "python -m pytest -q"  # -m puts repo root on sys.path


@lru_cache
def get_settings() -> Settings:
    return Settings()