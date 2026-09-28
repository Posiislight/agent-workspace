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

    async def update_pr(self, repo: str, number: int, **fields) -> dict:
        return await self._request("PATCH", f"/repos/{repo}/pulls/{number}", json=fields)

    async def get_pr(self, repo: str, number: int) -> dict:
        return await self._request("GET", f"/repos/{repo}/pulls/{number}")

    async def job_log_tail(self, repo: str, job_id: int, limit: int = 8000) -> str:
        """Plain-text Actions job log (GitHub answers with a redirect to storage)."""
        resp = await self._client.get(f"/repos/{repo}/actions/jobs/{job_id}/logs",
                                      follow_redirects=True)
        if resp.status_code != 200:
            return f"(could not fetch job log: HTTP {resp.status_code})"
        return resp.text[-limit:]

    async def put_file(self, repo: str, path: str, content_b64: str, *, branch: str,
                       message: str) -> dict:
        """Create/replace a file on `branch` via the Contents API (creates the branch
        from the default branch if missing)."""
        await self._ensure_branch(repo, branch)
        sha = None
        try:
            cur = await self._request("GET", f"/repos/{repo}/contents/{path}",
                                      params={"ref": branch})
            sha = cur.get("sha") if isinstance(cur, dict) else None
        except httpx.HTTPStatusError as exc:
            if exc.response.status_code != 404:
                raise
        body = {"message": message, "content": content_b64, "branch": branch}
        if sha:
            body["sha"] = sha
        return await self._request("PUT", f"/repos/{repo}/contents/{path}", json=body)

    async def _ensure_branch(self, repo: str, branch: str) -> None:
        try:
            await self._request("GET", f"/repos/{repo}/git/ref/heads/{branch}")
            return
        except httpx.HTTPStatusError as exc:
            if exc.response.status_code != 404:
                raise
        info = await self._request("GET", f"/repos/{repo}")
        base = await self._request(
            "GET", f"/repos/{repo}/git/ref/heads/{info['default_branch']}")
        try:
            await self._request("POST", f"/repos/{repo}/git/refs",
                                json={"ref": f"refs/heads/{branch}",
                                      "sha": base["object"]["sha"]})
        except httpx.HTTPStatusError as exc:
            if exc.response.status_code != 422:  # created concurrently
                raise

    async def mark_ready(self, repo: str, number: int) -> dict:
        return await self._request_idempotent_final(
            "PATCH", f"/repos/{repo}/pulls/{number}", json={"draft": False})

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
