from tests.fakes import StubAgent, make_services

from app.graph.nodes.researcher import researcher_node

FIRST = "URL: https://docs.example.com/api\nURL: https://docs.example.com/guide"
NOTES = "### Notes\n- API takes ints (docs.example.com/api)"


def researcher_state(**kw):
    st = {"task_id": "t1", "repo": "org/repo", "plan": "PLAN",
          "agent_id": "agent-1", "template_id": "codex", "error_log": []}
    st.update(kw)
    return st


async def test_researcher_visits_urls_and_writes_notes():
    stub = StubAgent(responses=[FIRST, NOTES])
    s = make_services(agent=stub)
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
    assert len(stub.calls) == 2
    assert stub.calls[0]["conversation_id"] == "aw-t1-researcher"
    assert stub.calls[1]["conversation_id"] == "aw-t1-researcher"
    assert "TOOL RESULTS:" in stub.calls[1]["message"]
