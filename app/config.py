from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    openrouter_api_key: str = ""
    openrouter_base_url: str = "https://openrouter.ai/api/v1"
    maritime_api_key: str = ""
    maritime_base_url: str = "https://api.maritime.sh"
    maritime_template_id: str = ""
    github_pat: str = ""
    github_webhook_secret: str = ""
    merge_pr_when_ready: bool = False

    database_url: str = "postgresql://aw:aw@localhost:5433/agent_workspace"
    redis_url: str = "redis://localhost:6380/0"

    model_planner: str = "openai/gpt-4.1-mini"
    model_researcher: str = "openai/gpt-4.1-mini"
    model_coding_agent: str = "anthropic/claude-sonnet-4.5"
    model_reviewer: str = "openai/gpt-4.1-mini"

    sandbox_run_timeout_seconds: int = 1800
    sandbox_exec_timeout_seconds: int = 110
    sandbox_poll_interval_seconds: float = 2.0
    test_command: str = "pytest -q"

    backoff_attempts: int = 3
    backoff_base_delay: float = 1.0

    # Phase 4/5 — plan limits: awake VMs and running computers (sleeping VMs are free)
    max_awake_vms: int = 2
    max_running_computers: int = 1

    # Harness layer: template id + command per CLI harness ({brief} = brief file path)
    default_harness: str = "openrouter"
    maritime_template_dsh: str = "dsh"
    maritime_template_codex: str = "codex"
    harness_cmd_dsh: str = 'dsh --profile headless "$(cat {brief})"'
    harness_cmd_codex: str = 'codex exec --full-auto --skip-git-repo-check "$(cat {brief})"'
    harness_env: dict[str, str] = {}

    # Cost / budget (VM billing is nominal: awake-minutes x rate)
    vm_cost_per_minute: float = 0.002
    default_budget_usd: float = 0.0

    # Follow-ups
    ci_autofix_max_attempts: int = 2
    proof_artifacts_branch: str = "aw-artifacts"


@lru_cache
def get_settings() -> Settings:
    return Settings()