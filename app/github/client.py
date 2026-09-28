import httpx


class GitHubClient:
    """Minimal GitHub REST client for PR lifecycle (PAT auth)."""

    def __init__(self, token: str, base_url: str = "https://api.github.com"):
        self._token = token
        self._client = httpx.AsyncClient(
            base_url=base_url,
            headers={
                "Authorization": f"Bearer {token}",
                "Accept": "application/vnd.github+json",
                "X-GitHub-Api-Version": "2022-11-28",
            },
            timeout=30.0,
        )

    async def _request(self, method: str, path: str, **kwargs) -> dict:
        headers = {
            "Authorization": f"Bearer {self._token}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
        }
        resp = await self._client.request(method, path, headers=headers, **kwargs)
        resp.raise_for_status()
        return resp.json()

    async def _request_idempotent_final(self, method: str, path: str, **kwargs) -> dict:
        try:
            return await self._request(method, path, **kwargs)
        except httpx.HTTPStatusError as exc:
            if exc.response.status_code in (405, 422):
                return {"already_finalized": True}
            raise

    async def list_repos(self) -> list[dict]:
        """List repos accessible to the PAT (paginated)."""
        repos: list[dict] = []
        page = 1
        while True:
            batch = await self._request(
                "GET", "/user/repos",
                params={"per_page": 100, "page": page, "sort": "updated",
                        "affiliation": "owner,collaborator,organization_member"})
            if not batch:
                break
            repos.extend(batch)
            if len(batch) < 100:
                break
            page += 1
        return [
            {"full_name": r["full_name"],
             "private": r.get("private", False),
             "default_branch": r.get("default_branch") or "main"}
            for r in repos
        ]

    async def find_open_pr(self, repo: str, head: str) -> dict | None:
        pulls = await self._request(
            "GET", f"/repos/{repo}/pulls",
            params={"state": "open", "head": head})
        return pulls[0] if pulls else None

    async def create_pr(self, repo: str, *, title: str, body: str, head: str,
                        base: str, draft: bool = True) -> dict:
        return await self._request(
            "POST", f"/repos/{repo}/pulls",
        json={"title": title, "body": body,
              "head": head.split(":", 1)[-1],
              "base": base, "draft": draft})

    async def mark_ready(self, repo: str, number: int) -> dict:
        return await self._request_idempotent_final(
            "PATCH", f"/repos/{repo}/pulls/{number}", json={"draft": False})

    async def update_pr_body(self, repo: str, number: int, body: str) -> dict:
        return await self._request("PATCH", f"/repos/{repo}/pulls/{number}",
                                   json={"body": body})

    async def add_comment(self, repo: str, number: int, body: str) -> dict:
        return await self._request(
            "POST", f"/repos/{repo}/issues/{number}/comments", json={"body": body})

    async def list_comments(self, repo: str, number: int) -> list[dict]:
        return await self._request(
            "GET", f"/repos/{repo}/issues/{number}/comments")

    async def merge_pr(self, repo: str, number: int) -> dict:
        return await self._request_idempotent_final(
            "PUT", f"/repos/{repo}/pulls/{number}/merge",
            json={"merge_method": "squash"})

    async def aclose(self) -> None:
        await self._client.aclose()
