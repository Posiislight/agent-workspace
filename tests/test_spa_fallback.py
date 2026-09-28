from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from app.main import FRONTEND_DIST, create_app

pytestmark = pytest.mark.skipif(not (FRONTEND_DIST / "index.html").is_file(),
                                reason="frontend not built")

BROWSER = {"Accept": "text/html,application/xhtml+xml,*/*;q=0.8"}


class StubRunManager:
    async def get_state(self, task_id):
        return None

    async def artifacts(self, task_id):
        return {}


def make_client():
    app = create_app(services=SimpleNamespace(
        settings=SimpleNamespace(github_pat="k", templates_allowlist=("codex", "dsh")),
        redis=None))
    app.state.run_manager = StubRunManager()
    return TestClient(app)


@pytest.mark.parametrize("path", ["/tasks/abc123", "/new", "/tasks/abc123/"])
def test_browser_navigation_to_ui_route_serves_app(path):
    r = make_client().get(path, headers=BROWSER)
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("text/html")
    assert '<div id="root">' in r.text


def test_api_fetch_to_same_path_still_gets_json():
    # fetch() sends Accept: */* -> must reach the API route, not the SPA
    r = make_client().get("/tasks/abc123", headers={"Accept": "*/*"})
    assert r.headers["content-type"].startswith("application/json")


def test_navigate_mode_wins_even_with_generic_accept():
    r = make_client().get("/tasks/abc123",
                          headers={"Accept": "*/*", "Sec-Fetch-Mode": "navigate"})
    assert r.headers["content-type"].startswith("text/html")


@pytest.mark.parametrize("headers", [BROWSER, {"Accept": "*/*"}])
def test_shared_path_responses_are_not_cached_across_representations(headers):
    # Without this the browser reused the cached HTML for the app's JSON fetch.
    r = make_client().get("/tasks/abc123", headers=headers)
    assert "Accept" in r.headers.get("vary", "")
    assert "no-store" in r.headers.get("cache-control", "")


def test_event_stream_and_subroutes_are_not_hijacked():
    c = make_client()
    r = c.get("/tasks/abc123/artifacts", headers=BROWSER)
    assert not r.headers["content-type"].startswith("text/html")
