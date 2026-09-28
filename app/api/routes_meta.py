from fastapi import APIRouter, HTTPException, Request

router = APIRouter()

TEMPLATE_META = {
    "codex": {"name": "Codex", "description": "OpenAI Codex CLI in a persistent workspace.",
              "tags": ["Coding", "OpenAI"]},
    "dsh": {"name": "DeepSeek Harness", "description": "DeepSeek open-source agent harness.",
            "tags": ["Coding", "DeepSeek"]},
}


@router.get("/templates")
async def templates(request: Request):
    allow = getattr(request.app.state.services.settings, "templates_allowlist",
                    ("codex", "dsh"))
    return {"templates": [{"id": t, **TEMPLATE_META.get(t, {"name": t, "description": "", "tags": []})}
                          for t in allow]}


@router.get("/github/repos")
async def github_repos(request: Request):
    from app.github.client import GitHubClient

    settings = request.app.state.services.settings
    if not settings.github_pat:
        raise HTTPException(503, "GitHub PAT not configured")
    gh = GitHubClient(settings.github_pat)
    try:
        return {"repos": await gh.list_repos()}
    finally:
        await gh.aclose()


@router.get("/models")
async def models(request: Request):
    llm = request.app.state.services.llm
    try:
        raw = await llm.models()
    except Exception as e:  # noqa: BLE001
        raise HTTPException(502, f"failed to fetch models: {e}") from e
    return {
        "models": [
            {"id": m["id"],
             "name": m.get("name") or m["id"],
             "context_length": m.get("context_length"),
             "pricing": m.get("pricing") or {}}
            for m in raw
        ]
    }
