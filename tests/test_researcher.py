from app.graph.nodes.researcher import researcher_node
from tests.fakes import StubLLM, make_services

FIRST = "URL: https://docs.example.com/api\nURL: https://docs.example.com/guide"
NOTES = "### Notes\n- API takes ints (docs.example.com/api)"


def researcher_state():
    return {"task_id": "t1", "repo": "org/repo", "plan": "PLAN", "model_overrides": {},
            "cost_so_far": 0.0, "error_log": []}


async def test_researcher_visits_urls_and_writes_notes():
    llm = StubLLM([FIRST, NOTES])
    s = make_services(llm=llm)
    updates = await researcher_node(researcher_state(), services=s)
    assert updates["computer_id"] == s.computers.computer_id
    assert updates["research_notes"].startswith("### Sources consulted")
    assert "Notes" in updates["research_notes"]
    assert s.computers.opened == ["https://docs.example.com/api", "https://docs.example.com/guide"]
    assert s.computers.viewer_calls == 1
    tool = [e for e in s.publisher.events if e.type == "tool_call"]
    assert any(e.data.get("viewer_url") for e in tool)
    assert sum(1 for e in tool if e.data.get("action") == "browser_open") == 2
    assert updates["status"] == "coding"